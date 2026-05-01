"""DuckDB-backed storage. Stores seen article ids so daily runs skip duplicates,
tagged output for later inspection / re-ranking, and a queue of articles whose
code URL has not yet been found (for re-crawling over the following 30 days).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from tech_radar.schemas import Affiliations, Article, TaggedArticle

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS tagged_articles (
    id TEXT PRIMARY KEY,
    payload JSON NOT NULL,
    processed_at TIMESTAMP NOT NULL,
    notion_page_id TEXT
);

CREATE TABLE IF NOT EXISTS code_pending (
    id          TEXT PRIMARY KEY,
    published_at TIMESTAMP NOT NULL,
    added_at    TIMESTAMP NOT NULL,
    last_attempt_at TIMESTAMP
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.db_path))
        self.conn.execute(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent column adds for older DBs that pre-date schema additions."""
        cols = self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tagged_articles'"
        ).fetchall()
        col_names = {row[0] for row in cols}
        if "notion_page_id" not in col_names:
            logger.info("Migrating tagged_articles: adding notion_page_id column")
            self.conn.execute("ALTER TABLE tagged_articles ADD COLUMN notion_page_id TEXT")

    # ------------------------------------------------------------------
    # seen_articles
    # ------------------------------------------------------------------

    def filter_unseen(self, articles: list[Article]) -> list[Article]:
        if not articles:
            return []
        ids = [a.id for a in articles]
        placeholders = ",".join(["?"] * len(ids))
        seen_rows = self.conn.execute(
            f"SELECT id FROM seen_articles WHERE id IN ({placeholders})", ids
        ).fetchall()
        seen = {row[0] for row in seen_rows}
        unseen = [a for a in articles if a.id not in seen]
        logger.info("Dedup: %d unseen out of %d", len(unseen), len(articles))
        return unseen

    def mark_seen(self, articles: list[Article]) -> None:
        if not articles:
            return
        now = datetime.now()
        rows = [(a.id, a.source.value, a.title, str(a.url), now) for a in articles]
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_articles VALUES (?, ?, ?, ?, ?)", rows
        )

    # ------------------------------------------------------------------
    # tagged_articles
    # ------------------------------------------------------------------

    def save_tagged(self, articles: list[TaggedArticle]) -> None:
        if not articles:
            return
        now = datetime.now()
        rows = [(a.id, a.model_dump_json(), now) for a in articles]
        # Use ON CONFLICT so existing notion_page_id is preserved on re-save.
        self.conn.executemany(
            """
            INSERT INTO tagged_articles (id, payload, processed_at) VALUES (?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                payload = excluded.payload,
                processed_at = excluded.processed_at
            """,
            rows,
        )
        # Queue articles without a code URL for re-crawl (GitHub search may find them later).
        pending = [a for a in articles if not a.tags.code_url]
        if pending:
            pending_rows = [(a.id, a.published_at, now) for a in pending]
            self.conn.executemany(
                "INSERT OR IGNORE INTO code_pending (id, published_at, added_at) VALUES (?, ?, ?)",
                pending_rows,
            )
            logger.info("Queued %d articles for code URL re-crawl", len(pending))

    def update_tagged_code(self, article: TaggedArticle) -> None:
        """Overwrite a stored article's payload with a newly discovered code URL/license."""
        self.conn.execute(
            "UPDATE tagged_articles SET payload = ? WHERE id = ?",
            [article.model_dump_json(), article.id],
        )

    def list_tagged_since(self, since: datetime) -> list[TaggedArticle]:
        """All tagged articles processed at-or-after `since`, newest first."""
        rows = self.conn.execute(
            "SELECT payload FROM tagged_articles WHERE processed_at >= ? ORDER BY processed_at DESC",
            [since],
        ).fetchall()
        return [TaggedArticle.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------------------------
    # notion_page_id (per-article Notion page tracking)
    # ------------------------------------------------------------------

    def get_notion_page_id(self, article_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT notion_page_id FROM tagged_articles WHERE id = ?", [article_id]
        ).fetchone()
        return row[0] if row else None

    def set_notion_page_id(self, article_id: str, page_id: str | None) -> None:
        self.conn.execute(
            "UPDATE tagged_articles SET notion_page_id = ? WHERE id = ?",
            [page_id, article_id],
        )

    # ------------------------------------------------------------------
    # affiliations cache (read-through against the existing payload column)
    # ------------------------------------------------------------------

    def get_cached_affiliations(self, article_id: str) -> Affiliations | None:
        """Return non-empty cached affiliations for a previously-processed article.

        Pulls the `affiliations` field out of the stored payload. Returns None
        when the article isn't in the table or its cached institutions list is
        empty (so callers can re-attempt enrichment instead of locking in a
        missed lookup).
        """
        row = self.conn.execute(
            "SELECT payload FROM tagged_articles WHERE id = ?", [article_id]
        ).fetchone()
        if not row:
            return None
        try:
            article = TaggedArticle.model_validate_json(row[0])
        except Exception:  # noqa: BLE001 — older payloads may pre-date the field
            return None
        if not article.affiliations.institutions:
            return None
        return article.affiliations

    def list_all_with_notion_status(self) -> list[tuple[TaggedArticle, str | None]]:
        """All tagged articles paired with their notion_page_id (or None)."""
        rows = self.conn.execute(
            "SELECT payload, notion_page_id FROM tagged_articles ORDER BY processed_at DESC"
        ).fetchall()
        return [
            (TaggedArticle.model_validate_json(row[0]), row[1]) for row in rows
        ]

    # ------------------------------------------------------------------
    # code_pending (re-crawl queue)
    # ------------------------------------------------------------------

    def list_code_pending(self, max_age_days: int = 30) -> list[TaggedArticle]:
        """Return queued articles whose paper is younger than max_age_days."""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        rows = self.conn.execute(
            """
            SELECT ta.payload
            FROM tagged_articles ta
            JOIN code_pending cp ON ta.id = cp.id
            WHERE cp.published_at >= ?
            ORDER BY cp.published_at DESC
            """,
            [cutoff],
        ).fetchall()
        return [TaggedArticle.model_validate_json(row[0]) for row in rows]

    def remove_code_pending(self, ids: list[str]) -> None:
        """Remove articles from the re-crawl queue (URL found or no longer needed)."""
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        self.conn.execute(
            f"DELETE FROM code_pending WHERE id IN ({placeholders})", ids
        )

    def cleanup_code_pending(self, max_age_days: int = 30) -> int:
        """Delete stale queue entries older than max_age_days. Returns count removed."""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        before = self.conn.execute("SELECT COUNT(*) FROM code_pending WHERE published_at < ?", [cutoff]).fetchone()[0]
        self.conn.execute("DELETE FROM code_pending WHERE published_at < ?", [cutoff])
        if before:
            logger.info("Cleaned up %d expired code_pending entries", before)
        return before

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
