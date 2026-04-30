"""Tagger agent — controlled-vocabulary tagging with the cheap local model."""

from __future__ import annotations

import logging
from typing import Any

from tech_radar.agents._client import ModelSpec, call_structured
from tech_radar.schemas import SummarizedArticle, Tags, TaggedArticle

logger = logging.getLogger(__name__)


def _topic_vocabulary(profile: dict[str, Any]) -> list[str]:
    vocab: list[str] = []
    for bucket in ("core_topics", "adjacent_topics", "peripheral_topics"):
        for t in profile.get(bucket, []):
            vocab.append(t["name"])
    return vocab


def _build_system_prompt(profile: dict[str, Any]) -> str:
    vocab = _topic_vocabulary(profile)
    vocab_str = ", ".join(vocab)
    return f"""You label technical articles with topic tags and metadata.

Use ONLY topic labels from this controlled vocabulary:
{vocab_str}

Pick 1-6 labels that genuinely fit (do not pad). Then determine method_type,
whether code is released, the code URL if visible, and any datasets mentioned."""


def _build_user_prompt(article: SummarizedArticle) -> str:
    kp = "\n".join(f"- {p}" for p in article.summary.key_points)
    return f"""Title: {article.title}

TL;DR: {article.summary.tldr}

Key points:
{kp}

Abstract:
{article.abstract[:1500]}
"""


def tag_article(
    article: SummarizedArticle, profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> TaggedArticle:
    tags = call_structured(
        spec=spec,
        system=_build_system_prompt(profile),
        user=_build_user_prompt(article),
        response_model=Tags,
        tool_name="tags",
        **call_kwargs,
    )
    return TaggedArticle(**article.model_dump(), tags=tags)


def tag_articles(
    articles: list[SummarizedArticle], profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> list[TaggedArticle]:
    import time
    from openai import APIConnectionError, APIStatusError

    results: list[TaggedArticle] = []
    for i, article in enumerate(articles):
        try:
            results.append(tag_article(article, profile, spec, **call_kwargs))
        except (APIConnectionError, APIStatusError) as e:
            logger.warning("Tag transient error on %s: %s; sleeping 10s", article.id, e)
            time.sleep(10)
            try:
                results.append(tag_article(article, profile, spec, **call_kwargs))
            except Exception as e2:
                logger.warning("Tag retry failed on %s: %s", article.id, e2)
                continue
        except Exception as e:
            logger.warning("Tag failed on %s: %s", article.id, e)
            continue
        if (i + 1) % 5 == 0:
            logger.info("Tagged %d/%d", i + 1, len(articles))
    logger.info("Tag pass: %d / %d", len(results), len(articles))
    return results
