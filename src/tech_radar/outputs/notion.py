"""Notion DB output for tech-radar.

Pushes each TaggedArticle to a Notion database as a page. The database is
either bootstrapped under a parent page (first run) or reused via
NOTION_DATABASE_ID. Property updates use page_id stored in DuckDB; on first
sight of an article we create the page, on subsequent sights we update
properties only (body is left alone — content rarely changes after tagging,
and rebuilding blocks would require deleting children which is expensive).

Failures are logged but never raised: the markdown digest is the source of
truth, Notion is a best-effort sidecar.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from notion_client import APIResponseError, Client

from tech_radar.schemas import TaggedArticle

logger = logging.getLogger(__name__)

# Notion API rate limit is "average 3 requests per second". Sleep between
# every call to stay well clear; bursts of >3 in <1s get 429s.
THROTTLE_SECONDS = 0.35

# Notion rich_text content cap is 2000 chars per block. Truncate proactively.
RICH_TEXT_LIMIT = 1900


def _rt(text: str | None) -> list[dict[str, Any]]:
    """Build a single-element rich_text array from a plain string."""
    if not text:
        return []
    if len(text) > RICH_TEXT_LIMIT:
        text = text[: RICH_TEXT_LIMIT - 1] + "…"
    return [{"type": "text", "text": {"content": text}}]


def _select(name: str | None) -> dict[str, Any] | None:
    return {"name": name} if name else None


def _multi_select(names: list[str]) -> list[dict[str, Any]]:
    # Notion forbids commas in multi-select option names.
    return [{"name": n.replace(",", " ")} for n in names if n]


METHOD_TYPE_OPTIONS = [
    {"name": "novel_method", "color": "blue"},
    {"name": "improvement", "color": "green"},
    {"name": "survey", "color": "purple"},
    {"name": "benchmark", "color": "yellow"},
    {"name": "tool_or_release", "color": "orange"},
    {"name": "application", "color": "pink"},
    {"name": "other", "color": "default"},
]

RELEVANCE_OPTIONS = [
    {"name": "core", "color": "red"},
    {"name": "adjacent", "color": "yellow"},
    {"name": "peripheral", "color": "gray"},
]


def _build_topic_options(profile: dict[str, Any]) -> list[dict[str, str]]:
    names: list[str] = []
    for bucket in ("core_topics", "adjacent_topics", "peripheral_topics"):
        for topic in profile.get(bucket, []) or []:
            name = topic.get("name") if isinstance(topic, dict) else None
            if name:
                names.append(name)
    seen: set[str] = set()
    options: list[dict[str, str]] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        options.append({"name": n})
    return options


def _build_database_schema(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "Title": {"title": {}},
        "URL": {"url": {}},
        "Source": {"select": {"options": [{"name": "arXiv cs.CV", "color": "blue"}]}},
        "Published": {"date": {}},
        "Topics": {"multi_select": {"options": _build_topic_options(profile)}},
        "Method type": {"select": {"options": METHOD_TYPE_OPTIONS}},
        "Has code": {"checkbox": {}},
        "Code URL": {"url": {}},
        "License": {"select": {"options": []}},
        "Datasets": {"multi_select": {"options": []}},
        "Performance": {"rich_text": {}},
        "Speed": {"rich_text": {}},
        "GPU": {"rich_text": {}},
        "Score": {"number": {"format": "number"}},
        "Relevance": {"select": {"options": RELEVANCE_OPTIONS}},
        "Run date": {"date": {}},
        "arXiv ID": {"rich_text": {}},
    }


def _properties_for(article: TaggedArticle, run_date: str) -> dict[str, Any]:
    td = article.summary.technical_details
    return {
        "Title": {"title": _rt(article.title)},
        "URL": {"url": str(article.url)},
        "Source": {"select": _select(article.source_name)},
        "Published": {"date": {"start": article.published_at.date().isoformat()}},
        "Topics": {"multi_select": _multi_select(article.tags.topics)},
        "Method type": {"select": _select(article.tags.method_type)},
        "Has code": {"checkbox": article.tags.has_code},
        "Code URL": {"url": str(article.tags.code_url) if article.tags.code_url else None},
        "License": {"select": _select(article.tags.license)},
        "Datasets": {"multi_select": _multi_select(article.tags.datasets)},
        "Performance": {"rich_text": _rt(td.performance)},
        "Speed": {"rich_text": _rt(td.speed)},
        "GPU": {"rich_text": _rt(td.gpu_requirements)},
        "Score": {"number": None},  # filled by Phase 2 Ranker
        "Relevance": {"select": _select(article.decision.relevance_hint)},
        "Run date": {"date": {"start": run_date}},
        "arXiv ID": {"rich_text": _rt(article.id)},
    }


def _heading(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def _para(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rt(text)},
    }


def _build_page_blocks(article: TaggedArticle) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    blocks.append(_heading("TL;DR"))
    blocks.append(_para(article.summary.tldr))

    blocks.append(_heading("Key points"))
    for kp in article.summary.key_points:
        blocks.append(_bullet(kp))

    blocks.append(_heading("Why it matters"))
    blocks.append(_para(article.summary.why_it_matters))

    blocks.append(_heading("Novelty"))
    blocks.append(_para(article.summary.novelty))

    td = article.summary.technical_details
    blocks.append(_heading("Technical details"))
    blocks.append(_bullet(f"性能: {td.performance or 'N/A'}"))
    blocks.append(_bullet(f"処理速度: {td.speed or 'N/A'}"))
    blocks.append(_bullet(f"必要GPU: {td.gpu_requirements or 'N/A'}"))

    blocks.append(_heading("Repository"))
    code_str = str(article.tags.code_url) if article.tags.code_url else "N/A"
    license_str = article.tags.license or "N/A"
    blocks.append(_bullet(f"GitHub: {code_str}"))
    blocks.append(_bullet(f"License: {license_str}"))

    return blocks


class NotionPublisher:
    """Best-effort writer to a Notion database. Throttled, never raises.

    Uses Notion API 2025-09 ('data sources'): a database is a container holding
    one or more data sources, and the schema (properties) lives on the data source
    rather than the database. We track both ids; pages are created with a
    data_source_id parent.
    """

    def __init__(
        self,
        token: str,
        database_id: str | None = None,
        parent_page_id: str | None = None,
        profile: dict[str, Any] | None = None,
    ) -> None:
        self.client = Client(auth=token)
        self.database_id = database_id
        self.data_source_id: str | None = None
        self.parent_page_id = parent_page_id
        self.profile = profile or {}
        self._last_call_at: float = 0.0

    # ---- internal ---------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < THROTTLE_SECONDS:
            time.sleep(THROTTLE_SECONDS - elapsed)
        self._last_call_at = time.monotonic()

    # ---- bootstrap --------------------------------------------------------

    def ensure_database(self, profile_name: str) -> str:
        """Return the database id, creating one under parent_page_id if missing.

        Side effect: also resolves and caches self.data_source_id.
        """
        if self.database_id:
            self._throttle()
            db = self.client.databases.retrieve(self.database_id)
            data_sources = db.get("data_sources") or []
            if not data_sources:
                raise RuntimeError(
                    f"Notion DB {self.database_id} has no data sources"
                )
            self.data_source_id = data_sources[0]["id"]
            return self.database_id

        if not self.parent_page_id:
            raise RuntimeError(
                "Notion: neither NOTION_DATABASE_ID nor NOTION_PARENT_PAGE_ID is set"
            )

        self._throttle()
        title = f"tech-radar: {profile_name}"
        result = self.client.databases.create(
            parent={"type": "page_id", "page_id": self.parent_page_id},
            title=[{"type": "text", "text": {"content": title}}],
            initial_data_source={
                "properties": _build_database_schema(self.profile),
            },
        )
        new_id = result["id"]
        data_sources = result.get("data_sources") or []
        if not data_sources:
            raise RuntimeError("Notion: created database has no data_sources field")
        self.data_source_id = data_sources[0]["id"]
        logger.info(
            "Created Notion database: %s (id=%s, data_source_id=%s)",
            title, new_id, self.data_source_id,
        )
        self.database_id = new_id
        return new_id

    # ---- page upsert ------------------------------------------------------

    def create_page(self, article: TaggedArticle, run_date: str) -> str | None:
        """Create a new page in the DB. Returns page_id or None on failure."""
        if not self.data_source_id:
            logger.error("Notion: data_source_id not resolved; skipping create")
            return None
        self._throttle()
        try:
            result = self.client.pages.create(
                parent={"type": "data_source_id", "data_source_id": self.data_source_id},
                properties=_properties_for(article, run_date),
                children=_build_page_blocks(article),
            )
            return result["id"]
        except APIResponseError as e:
            logger.warning("Notion create failed for %s: %s", article.id, e)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("Notion create error for %s: %s", article.id, e)
            return None

    def update_page(self, page_id: str, article: TaggedArticle, run_date: str) -> bool:
        """Update properties on an existing page. Body is not re-rendered."""
        self._throttle()
        try:
            self.client.pages.update(
                page_id=page_id,
                properties=_properties_for(article, run_date),
            )
            return True
        except APIResponseError as e:
            logger.warning("Notion update failed for %s: %s", article.id, e)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("Notion update error for %s: %s", article.id, e)
            return False

    def upsert(
        self, article: TaggedArticle, existing_page_id: str | None, run_date: str
    ) -> str | None:
        """Create or update; returns the page_id (new or unchanged)."""
        if existing_page_id:
            ok = self.update_page(existing_page_id, article, run_date)
            return existing_page_id if ok else None
        return self.create_page(article, run_date)

    def archive_page(self, page_id: str) -> bool:
        """Move a page to the trash. Used to recycle pages whose body needs
        rebuilding (cheaper than deleting and re-appending block-by-block)."""
        self._throttle()
        try:
            self.client.pages.update(page_id=page_id, in_trash=True)
            return True
        except APIResponseError as e:
            logger.warning("Notion archive failed for %s: %s", page_id, e)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("Notion archive error for %s: %s", page_id, e)
            return False
