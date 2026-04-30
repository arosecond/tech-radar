"""arXiv source: fetch recent papers from configured categories.

Uses the official `arxiv` library which wraps the arXiv export API.
We pre-filter on keywords here to keep the volume manageable before
the LLM agents see anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import arxiv

from tech_radar.schemas import Article, SourceType

logger = logging.getLogger(__name__)


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def fetch_arxiv(
    categories: list[str],
    keyword_prefilter: list[str],
    max_results_per_category: int = 50,
    lookback_days: int = 2,
) -> list[Article]:
    """Fetch recent arXiv papers from the given categories.

    Args:
        categories: arXiv category codes, e.g. ["cs.CV", "cs.GR"].
        keyword_prefilter: OR-match keywords; empty = no prefilter.
        max_results_per_category: API max per category.
        lookback_days: Only return papers published within this window.

    Returns:
        Deduplicated list of Article objects.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    client = arxiv.Client(page_size=max_results_per_category, delay_seconds=3, num_retries=3)

    seen_ids: set[str] = set()
    articles: list[Article] = []

    for category in categories:
        query = f"cat:{category}"
        search = arxiv.Search(
            query=query,
            max_results=max_results_per_category,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )

        try:
            results = list(client.results(search))
        except Exception as e:
            logger.warning("arXiv fetch failed for %s: %s", category, e)
            continue

        for result in results:
            arxiv_id = result.entry_id.rsplit("/", 1)[-1]
            if arxiv_id in seen_ids:
                continue

            published = result.published
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < cutoff:
                continue

            haystack = f"{result.title}\n{result.summary}"
            if keyword_prefilter and not _matches_keywords(haystack, keyword_prefilter):
                continue

            seen_ids.add(arxiv_id)
            articles.append(
                Article(
                    id=f"arxiv:{arxiv_id}",
                    source=SourceType.ARXIV,
                    source_name=f"arXiv {category}",
                    title=result.title.strip(),
                    url=result.entry_id,
                    abstract=result.summary.strip(),
                    authors=[a.name for a in result.authors],
                    published_at=published,
                    raw={
                        "arxiv_id": arxiv_id,
                        "primary_category": result.primary_category,
                        "categories": result.categories,
                        "pdf_url": result.pdf_url,
                    },
                )
            )

    logger.info("Fetched %d arXiv articles after prefilter", len(articles))
    return articles
