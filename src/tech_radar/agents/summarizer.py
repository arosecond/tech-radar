"""Summarizer agent — runs on the higher-quality model (default: Gemini Flash)."""

from __future__ import annotations

import logging
from typing import Any

import time

from openai import RateLimitError

from tech_radar.agents._client import ModelSpec, call_structured
from tech_radar.schemas import FilteredArticle, Summary, SummarizedArticle

logger = logging.getLogger(__name__)


def _build_system_prompt(profile: dict[str, Any]) -> str:
    base = f"""You are a technical summarizer for a curated digest on "{profile['profile_name']}".

Your reader is a working engineer. Be concrete and avoid marketing language.

For each article, produce:
- tldr: a single plain-language sentence
- key_points: 2-5 bullet-like statements covering method, results, and any limitations
- novelty: what is genuinely new compared to prior work, in one sentence
- why_it_matters: a one-sentence reason this is worth the reader's attention
- technical_details: extract concrete specs from the abstract:
    * performance: quantitative metrics + benchmark name (e.g. "PSNR 28.5 on Mip-NeRF360")
    * speed: inference FPS, training time, throughput (e.g. "60 FPS at 1080p", "trains in 30min")
    * gpu_requirements: hardware used or required (e.g. "Single RTX 3090 (24GB VRAM)")
  → For ANY field where the abstract does not state the information, set the
    value to null. NEVER invent numbers or hardware specs.

Stay faithful to the abstract. If the abstract is too thin to support a claim,
say so rather than speculating."""

    lang = str(profile.get("output_language", "english")).strip().lower()
    if lang in ("japanese", "ja", "日本語", "jp"):
        base += (
            "\n\nLANGUAGE: Write tldr, key_points, novelty, why_it_matters, "
            "and the technical_details string values in natural Japanese "
            "(自然な日本語). Keep technical terms in their standard English form "
            "when conventional (NeRF, Gaussian Splatting, SfM, MVS, depth "
            "estimation, PSNR, FPS, RTX 3090, etc.). The JSON field names stay "
            "in English — only the string VALUES are Japanese. Numeric values "
            "in technical_details (e.g. '28.5 dB', '60 FPS') are kept as-is."
        )
    return base


def _build_user_prompt(article: FilteredArticle) -> str:
    return f"""Title: {article.title}

Source: {article.source_name}

Abstract:
{article.abstract}
"""


def summarize_article(
    article: FilteredArticle, profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> SummarizedArticle:
    summary = call_structured(
        spec=spec,
        system=_build_system_prompt(profile),
        user=_build_user_prompt(article),
        response_model=Summary,
        tool_name="summary",
        **call_kwargs,
    )
    return SummarizedArticle(**article.model_dump(), summary=summary)


def summarize_articles(
    articles: list[FilteredArticle], profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> list[SummarizedArticle]:
    results: list[SummarizedArticle] = []
    for i, article in enumerate(articles):
        try:
            results.append(summarize_article(article, profile, spec, **call_kwargs))
        except RateLimitError as e:
            # On free tier, the per-minute limit can hit mid-batch. Sleep and retry once;
            # if the daily quota is gone there's nothing to do but stop.
            logger.warning("Rate limited on %s; sleeping 30s and retrying", article.id)
            time.sleep(30)
            try:
                results.append(summarize_article(article, profile, spec, **call_kwargs))
            except Exception as e2:
                logger.error("Summarize aborted on %s after retry: %s", article.id, e2)
                logger.error("Stopping summarize loop early to preserve remaining articles for next run")
                break
        except Exception as e:
            logger.warning("Summarize failed on %s: %s", article.id, e)
            continue
        if (i + 1) % 5 == 0:
            logger.info("Summarized %d/%d", i + 1, len(articles))
    logger.info("Summarize pass: %d / %d", len(results), len(articles))
    return results
