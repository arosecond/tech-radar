"""End-to-end pipeline: fetch → dedup → filter → summarize → tag → enrich → render.

After the main flow, a re-crawl step attempts to discover code URLs for
articles that were tagged with has_code=True but no URL at processing time.
Entries stay in the re-crawl queue for up to 30 days after publication.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from tech_radar.agents._client import ModelSpec
from tech_radar.agents.filter_agent import filter_articles
from tech_radar.agents.ranker import score_articles
from tech_radar.agents.summarizer import summarize_articles
from tech_radar.agents.tagger import tag_articles
from tech_radar.enrich import enrich_affiliations, enrich_articles
from tech_radar.outputs.html import render_digest_html, write_digest_html, write_index_html
from tech_radar.outputs.markdown import render_digest, write_digest
from tech_radar.outputs.notion import NotionPublisher
from tech_radar.schemas import Article, TaggedArticle
from tech_radar.sources.arxiv_source import fetch_arxiv
from tech_radar.storage import Store

logger = logging.getLogger(__name__)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_stage(models_cfg: dict[str, Any], stage: str) -> tuple[ModelSpec, dict[str, Any]]:
    """Pull (ModelSpec, call kwargs) for a stage from models.yaml."""
    cfg = models_cfg[stage]
    spec = ModelSpec(provider=cfg["provider"], model=cfg["model"])
    call_kwargs = {k: v for k, v in cfg.items() if k in ("max_tokens", "temperature", "enable_thinking")}
    return spec, call_kwargs


def make_notion_publisher(profile: dict[str, Any]) -> NotionPublisher | None:
    """Build a NotionPublisher from env vars. Returns None if disabled/misconfigured.

    Required: NOTION_API_TOKEN. Either NOTION_DATABASE_ID or NOTION_PARENT_PAGE_ID
    must be set (the former is preferred; the latter triggers DB bootstrap).
    """
    token = os.getenv("NOTION_API_TOKEN")
    if not token:
        logger.info("Notion: NOTION_API_TOKEN not set, skipping Notion output")
        return None
    db_id = os.getenv("NOTION_DATABASE_ID") or None
    parent_id = os.getenv("NOTION_PARENT_PAGE_ID") or None
    if not db_id and not parent_id:
        logger.warning(
            "Notion: NOTION_DATABASE_ID and NOTION_PARENT_PAGE_ID both empty; "
            "set one of them (parent_page_id triggers bootstrap)"
        )
        return None
    publisher = NotionPublisher(
        token=token,
        database_id=db_id,
        parent_page_id=parent_id,
        profile=profile,
    )
    try:
        new_id = publisher.ensure_database(profile.get("profile_name", "tech-radar"))
        if not db_id:
            # We just bootstrapped — let the user save the ID.
            logger.warning(
                "Notion: bootstrapped new database. Add this to your .env to skip "
                "bootstrap on subsequent runs:\n    NOTION_DATABASE_ID=%s",
                new_id,
            )
        return publisher
    except Exception as e:  # noqa: BLE001
        logger.warning("Notion: bootstrap failed: %s", e)
        return None


def push_to_notion(
    publisher: NotionPublisher,
    articles: list[TaggedArticle],
    store: Store,
) -> None:
    """Upsert each article to Notion and persist the page_id back to DuckDB."""
    if not articles:
        return
    run_date = date.today().isoformat()
    created = 0
    updated = 0
    failed = 0
    for article in articles:
        existing = store.get_notion_page_id(article.id)
        page_id = publisher.upsert(article, existing, run_date=run_date)
        if page_id is None:
            failed += 1
            continue
        if existing:
            updated += 1
        else:
            store.set_notion_page_id(article.id, page_id)
            created += 1
    logger.info(
        "Notion sync: created=%d updated=%d failed=%d (of %d)",
        created, updated, failed, len(articles),
    )


def fetch_all(sources_config: dict[str, Any], lookback_days: int) -> list[Article]:
    """Fan-out fetch across enabled sources. Phase 1: arXiv only."""
    articles: list[Article] = []

    arxiv_cfg = sources_config.get("arxiv", {})
    if arxiv_cfg.get("enabled"):
        articles.extend(
            fetch_arxiv(
                categories=arxiv_cfg["categories"],
                keyword_prefilter=arxiv_cfg.get("keyword_prefilter", []),
                max_results_per_category=arxiv_cfg.get("max_results_per_category", 50),
                lookback_days=lookback_days,
            )
        )

    return articles


def _load_notable_patterns(path: Path) -> list[str]:
    """Read config/notable_institutions.yaml. Missing file → empty list (feature disabled)."""
    if not path.exists():
        logger.info("notable_institutions.yaml not found at %s; skipping affiliation marker", path)
        return []
    cfg = load_yaml(path)
    return list(cfg.get("notable", []))


def run_pipeline(
    profile_path: Path,
    sources_path: Path,
    models_path: Path,
    db_path: Path,
    output_dir: Path,
    lookback_days: int = 2,
    digest_window_days: int = 1,
    digest_suffix: str = "",
    dry_run: bool = False,
    notion_enabled: bool = True,
    notable_path: Path | None = None,
) -> dict[str, Any]:
    profile = load_yaml(profile_path)
    sources = load_yaml(sources_path)
    models = load_yaml(models_path)
    notable_patterns = _load_notable_patterns(
        notable_path or (profile_path.parent / "notable_institutions.yaml")
    )

    filter_spec, filter_kwargs = _resolve_stage(models, "filter")
    summarizer_spec, summarizer_kwargs = _resolve_stage(models, "summarizer")
    tagger_spec, tagger_kwargs = _resolve_stage(models, "tagger")
    affiliation_spec: ModelSpec | None = None
    affiliation_kwargs: dict[str, Any] = {}
    if "affiliation_extractor" in models:
        affiliation_spec, affiliation_kwargs = _resolve_stage(models, "affiliation_extractor")
    ranker_spec: ModelSpec | None = None
    ranker_kwargs: dict[str, Any] = {}
    if "ranker" in models:
        ranker_spec, ranker_kwargs = _resolve_stage(models, "ranker")

    logger.info(
        "Routing: filter=%s/%s, summarizer=%s/%s, tagger=%s/%s",
        filter_spec.provider, filter_spec.model,
        summarizer_spec.provider, summarizer_spec.model,
        tagger_spec.provider, tagger_spec.model,
    )

    logger.info("Fetching articles...")
    raw = fetch_all(sources, lookback_days=lookback_days)

    with Store(db_path) as store:
        unseen = store.filter_unseen(raw)

        logger.info("Filter pass over %d articles...", len(unseen))
        kept = filter_articles(unseen, profile, filter_spec, **filter_kwargs)

        logger.info("Summarizing %d articles...", len(kept))
        summarized = summarize_articles(kept, profile, summarizer_spec, **summarizer_kwargs)

        logger.info("Tagging %d articles...", len(summarized))
        tagged: list[TaggedArticle] = tag_articles(summarized, profile, tagger_spec, **tagger_kwargs)

        logger.info("Enriching %d articles (URL discovery + license)...", len(tagged))
        tagged = enrich_articles(tagged)

        if notable_patterns:
            cache_hits = 0
            for article in tagged:
                cached = store.get_cached_affiliations(article.id)
                if cached is not None:
                    article.affiliations = cached
                    cache_hits += 1
            if cache_hits:
                logger.info("Affiliations: %d/%d served from cache", cache_hits, len(tagged))
            logger.info("Looking up author affiliations for %d articles...", len(tagged))
            tagged = enrich_affiliations(
                tagged,
                notable_patterns,
                llm_spec=affiliation_spec,
                llm_kwargs=affiliation_kwargs,
            )

        if ranker_spec is not None and tagged:
            # Pull existing rank back out of cached payloads so re-runs don't re-score.
            rank_cache_hits = 0
            for article in tagged:
                cached = store.get_cached_rank(article.id)
                if cached is not None:
                    article.rank = cached
                    rank_cache_hits += 1
            if rank_cache_hits:
                logger.info("Ranker: %d/%d served from cache", rank_cache_hits, len(tagged))
            logger.info("Ranking %d articles...", len(tagged))
            tagged = score_articles(tagged, profile, ranker_spec, **ranker_kwargs)

        if not dry_run:
            # Only mark articles seen if they made it all the way through. Anything dropped
            # mid-pipeline (rate limit, transient error) gets another shot on the next run.
            tagged_ids = {a.id for a in tagged}
            successfully_processed = [a for a in unseen if a.id in tagged_ids]
            store.mark_seen(successfully_processed)
            store.save_tagged(tagged)

            if notion_enabled and tagged:
                publisher = make_notion_publisher(profile)
                if publisher:
                    push_to_notion(publisher, tagged, store)

            # Re-crawl articles from previous runs that still lack a code URL.
            # Papers often publish code days or weeks after the arXiv submission.
            pending = store.list_code_pending(max_age_days=30)
            if pending:
                logger.info("Re-crawling %d pending code articles...", len(pending))
                re_enriched = enrich_articles(pending)
                found = [a for a in re_enriched if a.tags.code_url]
                if found:
                    logger.info(
                        "Found code URLs for %d previously pending articles", len(found)
                    )
                    for article in found:
                        store.update_tagged_code(article)
                    store.remove_code_pending([a.id for a in found])
                store.cleanup_code_pending(max_age_days=30)

        # Render the digest from DB so it accumulates across runs within the window.
        # Otherwise a single rate-limited run would overwrite a good digest with "0 items".
        since = datetime.now() - timedelta(days=digest_window_days)
        digest_articles = store.list_tagged_since(since)

    logger.info("Rendering digest: %d articles in last %d day(s)", len(digest_articles), digest_window_days)
    digest_md = render_digest(digest_articles, profile_name=profile["profile_name"])
    out_path = write_digest(digest_md, output_dir, suffix=digest_suffix)
    logger.info("Wrote digest: %s", out_path)

    # Per-day HTML archive (only this calendar date's articles) + master index.
    today_date = date.today()
    with Store(db_path) as store:
        per_day_articles = store.list_tagged_on(today_date)
        articles_by_date = store.list_all_tagged_by_processed_date()
    total_indexed = sum(len(v) for v in articles_by_date.values())
    digest_html = render_digest_html(
        per_day_articles, profile_name=profile["profile_name"], today=today_date
    )
    html_path = write_digest_html(digest_html, output_dir, today=today_date, suffix=digest_suffix)
    index_path = write_index_html(
        output_dir, profile_name=profile["profile_name"], articles_by_date=articles_by_date
    )
    logger.info("Wrote HTML: %s (index: %s, %d total)", html_path, index_path, total_indexed)
    return {
        "digest_path": out_path,
        "html_path": html_path,
        "index_path": index_path,
        "new_articles": tagged,
        "digest_articles": digest_articles,
    }
