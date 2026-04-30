"""Filter agent — cheap first-pass screening.

Decides whether each fetched article is on-topic for the reader's interest
profile. Aim is high recall (don't drop borderline items) — the Ranker will
do fine-grained ordering later.

By default this stage runs on the local Qwen model (provider=mori) via
llama.cpp's OpenAI-compatible endpoint. See config/models.yaml.
"""

from __future__ import annotations

import logging
from typing import Any

from tech_radar.agents._client import ModelSpec, call_structured
from tech_radar.schemas import Article, FilterDecision, FilteredArticle

logger = logging.getLogger(__name__)


def _build_system_prompt(profile: dict[str, Any]) -> str:
    core = ", ".join(t["name"] for t in profile.get("core_topics", []))
    adjacent = ", ".join(t["name"] for t in profile.get("adjacent_topics", []))
    peripheral = ", ".join(t["name"] for t in profile.get("peripheral_topics", []))
    deprioritize = "; ".join(profile.get("deprioritize", []))

    return f"""You are a screening agent for a technical news curator.

The reader's interest profile is "{profile['profile_name']}".

CORE TOPICS (always keep): {core}
ADJACENT TOPICS (keep if substantive): {adjacent}
PERIPHERAL TOPICS (keep only if novel): {peripheral}
NOT INTERESTED IN: {deprioritize}

For each article, decide whether to keep it. Bias toward keeping borderline
items — a downstream ranker will handle prioritization. Drop only items that
are clearly off-topic.
"""


def _build_user_prompt(article: Article) -> str:
    abstract = article.abstract[:1500] if article.abstract else "(no abstract)"
    return f"""Title: {article.title}

Source: {article.source_name}

Abstract:
{abstract}
"""


def filter_article(
    article: Article, profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> FilteredArticle:
    """Run the Filter agent on a single article."""
    decision = call_structured(
        spec=spec,
        system=_build_system_prompt(profile),
        user=_build_user_prompt(article),
        response_model=FilterDecision,
        tool_name="filter_decision",
        **call_kwargs,
    )
    return FilteredArticle(**article.model_dump(), decision=decision)


def filter_articles(
    articles: list[Article], profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> list[FilteredArticle]:
    """Filter a batch. Sequential for simplicity; switch to threading if volume grows."""
    import time
    from openai import APIConnectionError, APIStatusError

    results: list[FilteredArticle] = []
    for i, article in enumerate(articles):
        try:
            results.append(filter_article(article, profile, spec, **call_kwargs))
        except (APIConnectionError, APIStatusError) as e:
            # mori (llama.cpp) can be transiently unavailable. The OpenAI SDK already
            # retried internally; back off further before moving on.
            logger.warning("Filter transient error on %s: %s; sleeping 10s", article.id, e)
            time.sleep(10)
            try:
                results.append(filter_article(article, profile, spec, **call_kwargs))
            except Exception as e2:
                logger.warning("Filter retry failed on %s: %s", article.id, e2)
                continue
        except Exception as e:
            logger.warning("Filter failed on article %s: %s", article.id, e)
            continue
        if (i + 1) % 10 == 0:
            kept = sum(1 for r in results if r.decision.keep)
            logger.info("Filtered %d/%d, kept %d", i + 1, len(articles), kept)

    kept = [r for r in results if r.decision.keep]
    logger.info("Filter pass: kept %d / %d", len(kept), len(articles))
    return kept
