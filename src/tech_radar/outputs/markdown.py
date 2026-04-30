"""Markdown daily-digest writer."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tech_radar.schemas import TaggedArticle


def _fmt_optional(value: str | None) -> str:
    return value if value else "N/A"


def render_digest(articles: list[TaggedArticle], profile_name: str, today: date | None = None) -> str:
    today = today or date.today()
    lines: list[str] = []
    lines.append(f"# tech-radar: {profile_name} — {today.isoformat()}")
    lines.append("")
    lines.append(f"_{len(articles)} items after filtering, summarization, and tagging._")
    lines.append("")

    by_source: dict[str, list[TaggedArticle]] = {}
    for a in articles:
        by_source.setdefault(a.source_name, []).append(a)

    for source_name in sorted(by_source.keys()):
        lines.append(f"## {source_name}")
        lines.append("")
        for a in by_source[source_name]:
            star = " 🌟" if a.affiliations.notable_matches else ""
            lines.append(f"### [{a.title}]({a.url}){star}")
            lines.append("")
            authors = ", ".join(a.authors[:3]) + (" et al." if len(a.authors) > 3 else "")
            if authors:
                lines.append(f"*{authors} — {a.published_at.date().isoformat()}*")
                lines.append("")

            lines.append(f"**TL;DR:** {a.summary.tldr}")
            lines.append("")

            lines.append("**Key points:**")
            for kp in a.summary.key_points:
                lines.append(f"- {kp}")
            lines.append("")

            lines.append(f"**Why it matters:** {a.summary.why_it_matters}")
            lines.append("")

            td = a.summary.technical_details
            lines.append("**Technical details:**")
            lines.append(f"- 性能: {_fmt_optional(td.performance)}")
            lines.append(f"- 処理速度: {_fmt_optional(td.speed)}")
            lines.append(f"- 必要GPU: {_fmt_optional(td.gpu_requirements)}")
            lines.append("")

            code_str = (
                f"[{a.tags.code_url}]({a.tags.code_url})" if a.tags.code_url else "N/A"
            )
            lines.append("**Repository:**")
            lines.append(f"- GitHub: {code_str}")
            lines.append(f"- License: {_fmt_optional(a.tags.license)}")
            lines.append("")

            if a.affiliations.institutions:
                notable_set = set(a.affiliations.notable_matches)
                lines.append("**Affiliations:**")
                for inst in a.affiliations.institutions:
                    prefix = "🌟 " if inst in notable_set else ""
                    lines.append(f"- {prefix}{inst}")
                lines.append("")

            tag_str = " · ".join(f"`{t}`" for t in a.tags.topics)
            extras = [f"type: {a.tags.method_type}"]
            if a.tags.datasets:
                extras.append("datasets: " + ", ".join(a.tags.datasets))
            lines.append(f"{tag_str}  \n_{' · '.join(extras)}_")
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def write_digest(
    content: str,
    out_dir: Path,
    today: date | None = None,
    suffix: str = "",
) -> Path:
    today = today or date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"digest-{today.isoformat()}{('-' + suffix) if suffix else ''}.md"
    path = out_dir / name
    path.write_text(content, encoding="utf-8")
    return path
