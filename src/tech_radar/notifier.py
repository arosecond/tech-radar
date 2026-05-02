"""Slack notifications via Incoming Webhook.

All functions no-op silently if SLACK_WEBHOOK_URL is not set, so the pipeline
keeps working in environments without Slack configured (CI, local dev).

When SLACK_WEBHOOK_URL is set but the POST fails, a marker file is written
to the desktop so the operator notices the silent notification gap. The marker
is auto-deleted on the next successful POST.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

from tech_radar.schemas import TaggedArticle

logger = logging.getLogger(__name__)

_MAX_TITLES = 20
_TRACEBACK_LIMIT = 1500
_ERROR_LIMIT = 500
_MARKER_NAME = "TECH_RADAR_SLACK_FAILED.txt"


def _marker_path() -> Path:
    return Path.home() / "Desktop" / _MARKER_NAME


def _write_marker(error: str) -> None:
    try:
        path = _marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "tech-radar Slack通知失敗\n"
            f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Error: {error}\n\n"
            "対処: .env の SLACK_WEBHOOK_URL を更新してください。\n"
            "次回のSlack送信が成功するとこのファイルは自動削除されます。\n",
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Marker file write failed: %s", e)


def _clear_marker() -> None:
    try:
        path = _marker_path()
        if path.exists():
            path.unlink()
    except Exception as e:  # noqa: BLE001
        logger.debug("Marker file clear failed: %s", e)


def _post(blocks: list[dict], fallback_text: str) -> bool:
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        logger.info("Slack: SLACK_WEBHOOK_URL not set, skipping notification")
        return False
    payload = {"text": fallback_text, "blocks": blocks}
    try:
        r = httpx.post(url, json=payload, timeout=10)
        r.raise_for_status()
        _clear_marker()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Slack notification failed: %s", e)
        _write_marker(str(e))
        return False


def notify_success(
    *,
    digest_path: Path,
    new_articles: list[TaggedArticle],
    total_in_digest: int,
    duration_sec: float,
) -> bool:
    """Post a success summary with the list of newly-summarized paper titles."""
    # Rank-scored items go first (high → low); unscored items follow in source
    # order. Slack readers should see the most-relevant items at the top.
    sorted_articles = sorted(
        new_articles,
        key=lambda a: -(a.rank.score if a.rank else -1.0),
    )
    title_lines: list[str] = []
    for a in sorted_articles[:_MAX_TITLES]:
        title = a.title.replace("\n", " ").strip()
        marker = "🌟 " if a.affiliations.notable_matches else ""
        score = f" `{a.rank.score:.2f}`" if a.rank else ""
        title_lines.append(f"• {marker}<{a.url}|{title}>{score}")
    if len(new_articles) > _MAX_TITLES:
        title_lines.append(f"• ... and {len(new_articles) - _MAX_TITLES} more")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "✅ tech-radar 完走"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*新規要約:* {len(new_articles)}件"},
                {"type": "mrkdwn", "text": f"*Digest内合計:* {total_in_digest}件"},
                {"type": "mrkdwn", "text": f"*所要時間:* {int(duration_sec)}秒"},
                {"type": "mrkdwn", "text": f"*Digest:* `{digest_path.name}`"},
            ],
        },
    ]
    if title_lines:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*新規論文:*\n" + "\n".join(title_lines),
                },
            }
        )
    return _post(blocks, fallback_text=f"tech-radar 完走: 新規{len(new_articles)}件")


def notify_error(
    *,
    stage: str,
    error: str,
    traceback_str: str = "",
) -> bool:
    """Post a failure notification with truncated error and traceback."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "❌ tech-radar 失敗"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Stage:* {stage}"},
                {"type": "mrkdwn", "text": f"*Time:* {datetime.now().isoformat(timespec='seconds')}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Error:*\n```{error[:_ERROR_LIMIT]}```",
            },
        },
    ]
    if traceback_str:
        tail = traceback_str[-_TRACEBACK_LIMIT:]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Traceback (tail):*\n```{tail}```"},
            }
        )
    return _post(blocks, fallback_text=f"tech-radar 失敗: {stage}")


def notify_docker_failure(log_tail: str) -> bool:
    """Special-case notifier for Docker startup failure (called from the bat)."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "❌ tech-radar Docker起動失敗"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Stage:* docker-startup"},
                {"type": "mrkdwn", "text": f"*Time:* {datetime.now().isoformat(timespec='seconds')}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Log tail:*\n```{log_tail[-_TRACEBACK_LIMIT:]}```",
            },
        },
    ]
    return _post(blocks, fallback_text="tech-radar Docker起動失敗")
