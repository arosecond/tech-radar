"""Ranker agent — fine-grained interest-fit scoring after Filter / Tag.

Filter is high-recall (don't drop borderline items); the Ranker assigns each
surviving article a 0.0-1.0 score plus a one-sentence rationale, so the digest
can be sorted within each source group and Notion's `Score` column is filled.

Like every other stage, the model+config comes from `config/models.yaml` —
this stage is keyed `ranker`. If that key is absent the pipeline skips ranking
entirely (every article gets rank=None) and renderers fall back to publish-date
ordering.
"""

from __future__ import annotations

import logging
from typing import Any

from tech_radar.agents._client import ModelSpec, call_structured
from tech_radar.schemas import RankScore, TaggedArticle

logger = logging.getLogger(__name__)


def _topics_block(profile: dict[str, Any], bucket: str) -> str:
    items = profile.get(bucket, [])
    if not items:
        return "(none)"
    return ", ".join(t["name"] for t in items)


def _build_system_prompt(profile: dict[str, Any]) -> str:
    description = profile.get("description", "").strip()
    deprio = "; ".join(profile.get("deprioritize", [])) or "(none)"

    base = f"""You score technical articles for a reader's interest fit.

The reader's profile is "{profile['profile_name']}".
Description:
{description}

Topic buckets (ordered by weight):
- CORE (weight 1.0): {_topics_block(profile, "core_topics")}
- ADJACENT (weight 0.5): {_topics_block(profile, "adjacent_topics")}
- PERIPHERAL (weight 0.2): {_topics_block(profile, "peripheral_topics")}
- NOT INTERESTED IN: {deprio}

Score the article on a 0.0-1.0 scale:
  0.9-1.0  must-read: clearly hits a CORE topic AND brings real novelty
           (a new method, a non-trivial result, or a benchmark on a CORE task).
  0.7-0.9  strong: solid CORE-topic contribution, or an exceptional ADJACENT
           paper with directly transferable ideas.
  0.5-0.7  worth a look: ADJACENT topic with substance, or a CORE topic
           with limited novelty (incremental tweaks, narrow datasets).
  0.3-0.5  optional: PERIPHERAL with an interesting angle, or a CORE-adjacent
           paper that's mostly a survey / position piece.
  0.0-0.3  unlikely to interest the reader; deprioritize-flavored content.

Then write a 1-sentence rationale (≤200 characters) explaining what drove the
score in profile-fit terms — what topic it touches, what makes it (un)novel.
Be concrete. Reference the actual contribution; do not just restate the title.
Avoid hedge words like "potentially", "may be useful"."""

    lang = str(profile.get("output_language", "english")).strip().lower()
    if lang in ("japanese", "ja", "日本語", "jp"):
        base += (
            "\n\nLANGUAGE: Write the rationale in natural Japanese (自然な日本語). "
            "Keep technical terms in their standard English form when conventional "
            "(NeRF, Gaussian Splatting, SfM, MVS, depth estimation, PSNR, FPS, etc.). "
            "JSON field names stay English; only the rationale STRING is Japanese."
        )
    return base


def _build_user_prompt(article: TaggedArticle) -> str:
    kp = "\n".join(f"- {p}" for p in article.summary.key_points) or "- (none)"
    topics = ", ".join(article.tags.topics) or "(none)"
    datasets = ", ".join(article.tags.datasets) or "(none)"
    abstract = (article.abstract or "")[:1500]
    return f"""Title: {article.title}

TL;DR: {article.summary.tldr}

Key points:
{kp}

Tags: topics=[{topics}], method_type={article.tags.method_type}, has_code={article.tags.has_code}, datasets=[{datasets}]

Abstract:
{abstract}
"""


def score_article(
    article: TaggedArticle, profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> TaggedArticle:
    """Run the Ranker on one article. Mutates `article.rank` in place and returns it."""
    rank = call_structured(
        spec=spec,
        system=_build_system_prompt(profile),
        user=_build_user_prompt(article),
        response_model=RankScore,
        tool_name="rank_score",
        **call_kwargs,
    )
    article.rank = rank
    return article


def score_articles(
    articles: list[TaggedArticle], profile: dict[str, Any], spec: ModelSpec, **call_kwargs: Any
) -> list[TaggedArticle]:
    """Score a batch. Articles whose rank is already set are skipped (cache hit)."""
    import time
    from openai import APIConnectionError, APIStatusError

    pending = [a for a in articles if a.rank is None]
    skipped = len(articles) - len(pending)
    if skipped:
        logger.info("Ranker: %d/%d already ranked, scoring %d new", skipped, len(articles), len(pending))

    for i, article in enumerate(pending):
        try:
            score_article(article, profile, spec, **call_kwargs)
        except (APIConnectionError, APIStatusError) as e:
            logger.warning("Ranker transient error on %s: %s; sleeping 10s", article.id, e)
            time.sleep(10)
            try:
                score_article(article, profile, spec, **call_kwargs)
            except Exception as e2:
                logger.warning("Ranker retry failed on %s: %s", article.id, e2)
                continue
        except Exception as e:
            logger.warning("Ranker failed on %s: %s", article.id, e)
            continue
        if (i + 1) % 5 == 0:
            logger.info("Ranked %d/%d", i + 1, len(pending))

    ranked_count = sum(1 for a in articles if a.rank is not None)
    logger.info("Rank pass: %d / %d articles have a score", ranked_count, len(articles))
    return articles
