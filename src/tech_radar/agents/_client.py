"""Multi-provider LLM client with structured output via OpenAI-compatible endpoints.

Both backends we use expose an OpenAI-compatible API:
- "mori":    Local LLM at LLM_BASE_URL (default: http://localhost:8080/v1).
             Provider-agnostic: any OpenAI-compatible server works — the home
             rig runs llama.cpp serving Qwen 3-27B GGUF; the company rig is
             expected to run vLLM serving the same model (2-3x throughput on
             the same hardware). The "mori" provider key is the engineering
             persona name we kept once it stuck — the actual backend behind
             it is decided by LLM_BASE_URL.
- "gemini":  Gemini 2.0 Flash via Google's OpenAI-compat endpoint (free tier)

Structured output is achieved cross-provider by:
1. Embedding the JSON schema into the system prompt so the model knows the shape.
2. Setting `response_format={"type": "json_object"}` to force valid JSON.
3. Validating the result with Pydantic; retrying on schema-validation failure.

This is more portable than tool-use, which has different semantics on each backend.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, TypeVar

from openai import OpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

Provider = Literal["mori", "gemini"]
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ModelSpec:
    """Logical model reference: which provider, which model id."""

    provider: Provider
    model: str


# -- Per-provider rate limiting ------------------------------------------------
#
# Gemini's free tier caps gemini-2.5-flash at ~10 requests/minute. Pacing the
# calls proactively avoids the 429 cascade we'd otherwise hit on a 15-article
# batch. mori has no remote limit but we still serialize on its slot.

_MIN_INTERVAL_S: dict[str, float] = {
    "gemini": 6.5,   # 10 RPM with margin
    "mori": 0.0,     # local; llama.cpp self-serializes via --parallel 1, vLLM has its own scheduler
}

_last_call_at: dict[str, float] = {}
_throttle_lock = threading.Lock()


def _throttle(provider: str) -> None:
    interval = _MIN_INTERVAL_S.get(provider, 0.0)
    if interval <= 0:
        return
    with _throttle_lock:
        last = _last_call_at.get(provider, 0.0)
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_call_at[provider] = time.monotonic()


# -- Client construction -------------------------------------------------------


@lru_cache(maxsize=4)
def _client_for(provider: Provider) -> OpenAI:
    if provider == "mori":
        base_url = os.environ.get("LLM_BASE_URL", "http://localhost:8080/v1")
        # llama.cpp's default `--parallel 1` returns 503 when the slot is busy
        # (e.g. an active interactive chat session sharing the same process).
        # vLLM does internal queueing, so 5 retries are wasted budget there but
        # also harmless. Generous timeout because thinking-mode summaries can
        # take 60-90s on a single 24GB GPU.
        return OpenAI(
            base_url=base_url,
            api_key=os.environ.get("LLM_API_KEY", "not-used"),
            max_retries=5,
            timeout=180.0,
        )

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set — get one at https://aistudio.google.com/apikey")
        return OpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=api_key,
            max_retries=3,
            timeout=120.0,
        )

    raise ValueError(f"Unknown provider: {provider}")


# -- Structured output ---------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json(content: str) -> str:
    """Pull a JSON payload out of a model response that may be wrapped in markdown.

    Local Qwen tends to return ```json {...} ``` despite response_format=json_object.
    This is a defensive recovery so we don't burn a retry on a trivially fixable wrap.
    """
    text = content.strip()
    if not text:
        raise json.JSONDecodeError("empty response", text, 0)

    # Fast path: already pure JSON.
    if text.startswith("{") or text.startswith("["):
        return text

    fence = _FENCE_RE.search(text)
    if fence:
        return fence.group(1)

    bare = _BARE_OBJ_RE.search(text)
    if bare:
        return bare.group(1)

    return text  # let json.loads raise with the original


def _system_with_schema(system: str, schema: dict, tool_name: str) -> str:
    """Append the target JSON schema to the system prompt."""
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    return (
        f"{system}\n\n"
        f"OUTPUT FORMAT (strict):\n"
        f"Respond with EXACTLY ONE raw JSON object that validates against the "
        f"`{tool_name}` schema below. Output the JSON as the very first character "
        f"of your reply (`{{`). Do NOT use markdown code fences. Do NOT add any "
        f"prose, comments, or explanation before or after the JSON.\n\n"
        f"Schema:\n{schema_json}"
    )


@retry(
    retry=retry_if_exception_type((ValidationError, json.JSONDecodeError)),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=4),
)
def _call_once(
    *,
    spec: ModelSpec,
    system: str,
    user: str,
    response_model: type[T],
    tool_name: str,
    max_tokens: int,
    temperature: float,
    enable_thinking: bool = False,
) -> T:
    _throttle(spec.provider)

    client = _client_for(spec.provider)
    schema = response_model.model_json_schema()
    full_system = _system_with_schema(system, schema, tool_name)

    # Both backends ship reasoning models that eat into max_tokens by default.
    # Caller decides whether to spend that budget on thinking; default off.
    extra_body: dict = {}
    if spec.provider == "gemini":
        extra_body["reasoning_effort"] = "low" if not enable_thinking else "medium"
    elif spec.provider == "mori":
        # Qwen 3.x exposes an `enable_thinking` flag via its chat template.
        # llama.cpp passes chat_template_kwargs straight through.
        extra_body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

    response: ChatCompletion = client.chat.completions.create(
        model=spec.model,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": full_system},
            {"role": "user", "content": user},
        ],
        extra_body=extra_body or None,
    )

    content = response.choices[0].message.content or ""
    usage = response.usage
    if usage is not None:
        logger.debug(
            "provider=%s model=%s in=%d out=%d",
            spec.provider,
            spec.model,
            usage.prompt_tokens,
            usage.completion_tokens,
        )

    try:
        payload = _extract_json(content)
        raw = json.loads(payload)
    except json.JSONDecodeError:
        finish = response.choices[0].finish_reason
        logger.warning(
            "JSON decode failed for %s/%s (finish=%s). First 300 chars: %r",
            spec.provider, spec.model, finish, content[:300],
        )
        raise
    return response_model.model_validate(raw)


def call_structured(
    *,
    spec: ModelSpec,
    system: str,
    user: str,
    response_model: type[T],
    tool_name: str,
    tool_description: str = "",  # kept for API compatibility; unused in JSON-mode flow
    max_tokens: int = 1024,
    temperature: float = 0.2,
    enable_thinking: bool = False,  # mori (Qwen) only; off by default for predictable latency
    cache_system: bool = False,  # no-op for OpenAI-compat backends
) -> T:
    """One-shot structured call returning a validated Pydantic instance."""
    del tool_description, cache_system  # explicitly unused
    return _call_once(
        spec=spec,
        system=system,
        user=user,
        response_model=response_model,
        tool_name=tool_name,
        max_tokens=max_tokens,
        temperature=temperature,
        enable_thinking=enable_thinking,
    )
