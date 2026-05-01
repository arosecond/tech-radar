"""Static HTML output — local browse-friendly view.

Two writers:
    write_digest_html  — single-day archive page (just one calendar date's
                         articles, opened standalone).
    write_index_html   — master page: every tagged article ever, grouped into
                         <details> sections by processed-at date (newest-first,
                         today-only opened by default), with client-side
                         search + per-article bookmarking persisted in
                         localStorage. Self-contained (CSS+JS embedded), so it
                         runs from file:// without any server.

The index page is the primary view for deployments that intentionally disable
Notion (e.g., the company Ubuntu run, where article content stays local).
Bookmarks are scoped to a single browser profile per arXiv ID; users can hit
"Export bookmarks" to download a JSON backup.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from html import escape
from pathlib import Path

from tech_radar.schemas import TaggedArticle


_CSS = """
:root {
  --bg: #fafaf9;
  --fg: #1c1917;
  --muted: #57534e;
  --line: #e7e5e4;
  --card: #ffffff;
  --accent: #0369a1;
  --score-bg: #eef2ff;
  --score-fg: #3730a3;
  --notable: #d97706;
  --bookmark: #eab308;
  --bookmark-hover: #ca8a04;
  --code-bg: #f5f5f4;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Hiragino Kaku Gothic ProN", "ヒラギノ角ゴ ProN W3",
               Meiryo, sans-serif;
  background: var(--bg);
  color: var(--fg);
  margin: 0;
  line-height: 1.6;
  font-size: 15px;
}
.wrap {
  max-width: 880px;
  margin: 0 auto;
  padding: 1.25rem 1.25rem 4rem;
}
h1 {
  font-size: 1.6rem;
  margin: 0 0 .25rem;
  letter-spacing: -.01em;
}
.meta {
  color: var(--muted);
  font-size: .9rem;
  margin-bottom: 1.5rem;
}
.toolbar {
  position: sticky;
  top: 0;
  z-index: 5;
  background: var(--bg);
  padding: .75rem 0;
  border-bottom: 1px solid var(--line);
  margin-bottom: 1rem;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .65rem;
}
.toolbar input[type="search"] {
  flex: 1;
  min-width: 200px;
  font-size: .95rem;
  padding: .45rem .65rem;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--card);
  color: var(--fg);
}
.toolbar input[type="search"]:focus {
  outline: 2px solid var(--accent);
  outline-offset: -1px;
  border-color: var(--accent);
}
.toolbar label {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  font-size: .9rem;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}
.toolbar button {
  font-size: .85rem;
  padding: .4rem .65rem;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--card);
  color: var(--fg);
  cursor: pointer;
}
.toolbar button:hover { background: var(--code-bg); }
.toolbar .count {
  font-size: .85rem;
  color: var(--muted);
  margin-left: auto;
}
details.date-group {
  margin: 1rem 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--card);
}
details.date-group > summary {
  padding: .65rem 1rem;
  cursor: pointer;
  font-weight: 600;
  list-style: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}
details.date-group > summary::-webkit-details-marker { display: none; }
details.date-group > summary::before {
  content: "▸";
  display: inline-block;
  width: 1em;
  margin-right: .5em;
  transition: transform .15s;
  color: var(--muted);
}
details.date-group[open] > summary::before { transform: rotate(90deg); }
details.date-group > summary .group-count {
  font-weight: 400;
  font-size: .85rem;
  color: var(--muted);
}
.group-body { padding: .25rem 1rem 1rem; }
h2.source {
  font-size: .85rem;
  border-bottom: 1px solid var(--line);
  padding-bottom: .25rem;
  margin: 1.5rem 0 .65rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .04em;
}
h2.source:first-child { margin-top: .5rem; }
.article {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: .85rem 1.1rem;
  margin: .65rem 0;
  background: var(--bg);
}
.article-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.article-title {
  font-size: 1rem;
  font-weight: 600;
  margin: 0;
  flex: 1;
  min-width: 0;
}
.article-title a {
  color: var(--fg);
  text-decoration: none;
}
.article-title a:hover { color: var(--accent); }
.score {
  display: inline-block;
  background: var(--score-bg);
  color: var(--score-fg);
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: .85rem;
  padding: 1px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.star { color: var(--notable); margin-left: .25rem; }
.bookmark-toggle {
  background: none;
  border: none;
  cursor: pointer;
  padding: 0 .15rem;
  font-size: 1.1rem;
  color: var(--line);
  line-height: 1;
}
.bookmark-toggle:hover { color: var(--bookmark-hover); }
.bookmark-toggle.bookmarked { color: var(--bookmark); }
.byline {
  color: var(--muted);
  font-size: .85rem;
  font-style: italic;
  margin: .25rem 0 .65rem;
}
.rationale {
  background: var(--score-bg);
  border-left: 3px solid var(--score-fg);
  padding: .45rem .75rem;
  margin: .35rem 0 .85rem;
  font-size: .9rem;
  color: var(--score-fg);
}
.section-label {
  font-weight: 600;
  font-size: .9rem;
  display: block;
  margin: .65rem 0 .2rem;
}
.kp { margin: .25rem 0 .25rem 1.2rem; padding: 0; }
.kp li { margin: .1rem 0; }
.tech { font-size: .9rem; color: var(--muted); margin: .15rem 0; }
.tech b { color: var(--fg); font-weight: 600; }
.repo { font-size: .9rem; margin: .25rem 0; }
.repo code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px; }
.affiliations { font-size: .9rem; color: var(--muted); margin-top: .4rem; }
.affiliations .star { margin: 0 .15em 0 0; }
.tags {
  margin-top: .8rem;
  padding-top: .55rem;
  border-top: 1px dashed var(--line);
  font-size: .82rem;
  color: var(--muted);
}
.tag {
  display: inline-block;
  background: var(--code-bg);
  border-radius: 4px;
  padding: 1px 7px;
  margin-right: .35em;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  color: var(--fg);
}
a.external { color: var(--accent); }
.empty { padding: 2rem 0; color: var(--muted); text-align: center; }
"""


_JS = r"""
(function() {
  const KEY = 'tech-radar:bookmarks';
  const load = () => new Set(JSON.parse(localStorage.getItem(KEY) || '[]'));
  const save = (set) => localStorage.setItem(KEY, JSON.stringify([...set]));

  let bookmarks = load();
  let bookmarksOnly = false;
  let query = '';

  const articles = Array.from(document.querySelectorAll('.article'));
  const groups = Array.from(document.querySelectorAll('details.date-group'));
  const search = document.getElementById('search');
  const filterToggle = document.getElementById('bookmarks-only');
  const exportBtn = document.getElementById('export-bookmarks');
  const importBtn = document.getElementById('import-bookmarks');
  const importFile = document.getElementById('import-file');
  const countEl = document.getElementById('count');
  const bmCountEl = document.getElementById('bookmark-count');

  function syncStars() {
    articles.forEach(a => {
      const id = a.dataset.arxivId;
      const btn = a.querySelector('.bookmark-toggle');
      if (!btn) return;
      btn.classList.toggle('bookmarked', bookmarks.has(id));
      btn.setAttribute('aria-pressed', bookmarks.has(id) ? 'true' : 'false');
      btn.title = bookmarks.has(id) ? 'Remove bookmark' : 'Bookmark this paper';
      btn.textContent = bookmarks.has(id) ? '★' : '☆';
    });
    bmCountEl.textContent = bookmarks.size;
  }

  function applyFilter() {
    const q = query.trim().toLowerCase();
    let visible = 0;
    articles.forEach(a => {
      const id = a.dataset.arxivId;
      const text = (a.dataset.search || '').toLowerCase();
      const okSearch = !q || text.includes(q);
      const okBookmark = !bookmarksOnly || bookmarks.has(id);
      const show = okSearch && okBookmark;
      a.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    groups.forEach(g => {
      const anyVisible = !!g.querySelector('.article:not([style*="display: none"])');
      g.style.display = anyVisible ? '' : 'none';
      if ((q || bookmarksOnly) && anyVisible) g.open = true;
    });
    if (q || bookmarksOnly) {
      countEl.textContent = `${visible} match${visible === 1 ? '' : 'es'}`;
    } else {
      countEl.textContent = `${articles.length} total`;
    }
  }

  articles.forEach(a => {
    const btn = a.querySelector('.bookmark-toggle');
    if (!btn) return;
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      const id = a.dataset.arxivId;
      if (bookmarks.has(id)) bookmarks.delete(id); else bookmarks.add(id);
      save(bookmarks);
      syncStars();
      if (bookmarksOnly) applyFilter();
    });
  });

  search.addEventListener('input', (e) => { query = e.target.value; applyFilter(); });
  filterToggle.addEventListener('change', (e) => { bookmarksOnly = e.target.checked; applyFilter(); });

  exportBtn.addEventListener('click', () => {
    const data = JSON.stringify([...bookmarks], null, 2);
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const today = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = `tech-radar-bookmarks-${today}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  importBtn.addEventListener('click', () => importFile.click());
  importFile.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const ids = JSON.parse(ev.target.result);
        if (!Array.isArray(ids)) throw new Error('not an array');
        ids.forEach(id => bookmarks.add(id));
        save(bookmarks);
        syncStars();
        if (bookmarksOnly) applyFilter();
        alert(`Imported. Total bookmarks now: ${bookmarks.size}`);
      } catch (err) {
        alert('Import failed: ' + err.message);
      }
      importFile.value = '';
    };
    reader.readAsText(file);
  });

  syncStars();
  applyFilter();
})();
"""


# ---------------------------------------------------------------------------
# Helpers shared between digest + index pages
# ---------------------------------------------------------------------------


def _fmt_optional(value: str | None) -> str:
    return escape(value) if value else "N/A"


def _authors(article: TaggedArticle) -> str:
    if not article.authors:
        return ""
    head = ", ".join(article.authors[:3])
    if len(article.authors) > 3:
        head += " et al."
    return head


def _searchable_text(article: TaggedArticle) -> str:
    """Concatenate every user-visible field into one lowercase blob for `data-search`."""
    parts: list[str] = [
        article.title or "",
        article.summary.tldr or "",
        " ".join(article.summary.key_points or []),
        article.summary.why_it_matters or "",
        article.summary.novelty or "",
        " ".join(article.tags.topics or []),
        article.tags.method_type or "",
        " ".join(article.tags.datasets or []),
        article.tags.license or "",
        " ".join(article.affiliations.institutions or []),
        article.rank.rationale if article.rank else "",
        " ".join(article.authors or []),
        article.id,
    ]
    return " ".join(p for p in parts if p)


def _sort_key(a: TaggedArticle) -> tuple:
    score = a.rank.score if a.rank else -1.0
    return (-score, -a.published_at.timestamp())


def _render_article(article: TaggedArticle, *, show_bookmark: bool) -> str:
    notable = bool(article.affiliations.notable_matches)
    star = '<span class="star" title="notable affiliation">🌟</span>' if notable else ""
    score_badge = (
        f'<span class="score">{article.rank.score:.2f}</span>'
        if article.rank else ""
    )
    rationale_block = (
        f'<div class="rationale">Why this score: {escape(article.rank.rationale)}</div>'
        if article.rank else ""
    )
    authors = _authors(article)
    byline = (
        f'<div class="byline">{escape(authors)} — '
        f'{article.published_at.date().isoformat()}</div>'
        if authors else ""
    )
    kp_items = "".join(f"<li>{escape(p)}</li>" for p in article.summary.key_points)

    td = article.summary.technical_details
    code_url = str(article.tags.code_url) if article.tags.code_url else None
    code_html = (
        f'<a class="external" href="{escape(code_url)}">{escape(code_url)}</a>'
        if code_url else "N/A"
    )

    aff_html = ""
    if article.affiliations.institutions:
        notable_set = set(article.affiliations.notable_matches)
        items = []
        for inst in article.affiliations.institutions:
            prefix = '<span class="star">🌟</span>' if inst in notable_set else ""
            items.append(f"{prefix}{escape(inst)}")
        aff_html = (
            '<div class="affiliations"><b>Affiliations:</b> '
            + " · ".join(items)
            + "</div>"
        )

    tag_chips = " ".join(f'<span class="tag">{escape(t)}</span>' for t in article.tags.topics)
    extras = [f"type: {escape(article.tags.method_type)}"]
    if article.tags.datasets:
        extras.append("datasets: " + escape(", ".join(article.tags.datasets)))

    bookmark_btn = (
        '<button class="bookmark-toggle" type="button" aria-pressed="false" title="Bookmark this paper">☆</button>'
        if show_bookmark else ""
    )

    return f"""    <article class="article" data-arxiv-id="{escape(article.id, quote=True)}" data-search="{escape(_searchable_text(article), quote=True)}">
      <div class="article-head">
        <h3 class="article-title">
          <a href="{escape(str(article.url))}" target="_blank" rel="noopener">{escape(article.title)}</a>{star}
        </h3>
        {score_badge}{bookmark_btn}
      </div>
      {byline}
      {rationale_block}

      <span class="section-label">TL;DR</span>
      <div>{escape(article.summary.tldr)}</div>

      <span class="section-label">Key points</span>
      <ul class="kp">{kp_items}</ul>

      <span class="section-label">Why it matters</span>
      <div>{escape(article.summary.why_it_matters)}</div>

      <span class="section-label">Technical details</span>
      <div class="tech"><b>性能:</b> {_fmt_optional(td.performance)}</div>
      <div class="tech"><b>処理速度:</b> {_fmt_optional(td.speed)}</div>
      <div class="tech"><b>必要GPU:</b> {_fmt_optional(td.gpu_requirements)}</div>

      <div class="repo">
        <b>GitHub:</b> {code_html}
        &nbsp;·&nbsp; <b>License:</b> {_fmt_optional(article.tags.license)}
      </div>
      {aff_html}

      <div class="tags">{tag_chips} <span style="color:var(--muted)">— {' · '.join(extras)}</span></div>
    </article>
"""


def _render_articles_grouped_by_source(articles: list[TaggedArticle], *, show_bookmark: bool) -> str:
    by_source: dict[str, list[TaggedArticle]] = defaultdict(list)
    for a in articles:
        by_source[a.source_name].append(a)
    parts: list[str] = []
    for source_name in sorted(by_source.keys()):
        parts.append(f'<h2 class="source">{escape(source_name)}</h2>')
        for article in sorted(by_source[source_name], key=_sort_key):
            parts.append(_render_article(article, show_bookmark=show_bookmark))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-day digest (single calendar date, no JS)
# ---------------------------------------------------------------------------


def render_digest_html(
    articles: list[TaggedArticle],
    profile_name: str,
    today: date | None = None,
) -> str:
    today = today or date.today()
    body = (
        f'<h1>tech-radar: {escape(profile_name)}</h1>\n'
        f'<div class="meta">{today.isoformat()} — '
        f'{len(articles)} item{"s" if len(articles) != 1 else ""}</div>\n'
        f'<p style="font-size:.85rem;color:var(--muted)"><a href="index.html">← all digests</a></p>\n'
    )
    if articles:
        body += _render_articles_grouped_by_source(articles, show_bookmark=False)
    else:
        body += '<div class="empty">No articles for this date.</div>'

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tech-radar: {escape(profile_name)} — {today.isoformat()}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


def write_digest_html(
    content: str,
    out_dir: Path,
    today: date | None = None,
    suffix: str = "",
) -> Path:
    today = today or date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"digest-{today.isoformat()}{('-' + suffix) if suffix else ''}.html"
    path = out_dir / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Master index (all articles, all dates, search + bookmarks)
# ---------------------------------------------------------------------------


def render_index_html(
    articles_by_date: dict[date, list[TaggedArticle]],
    profile_name: str,
) -> str:
    """Render the master index with every tagged article, grouped by processed_at date.

    Today's section starts open; every older section starts collapsed. JavaScript
    powers per-article bookmarks (localStorage) and a free-text filter that
    searches across title / summary / topics / rationale / affiliations.
    """
    today = date.today()

    sections: list[str] = []
    for d in sorted(articles_by_date.keys(), reverse=True):
        items = articles_by_date[d]
        is_open = " open" if d == today else ""
        body = _render_articles_grouped_by_source(items, show_bookmark=True)
        sections.append(
            f'<details class="date-group"{is_open}>\n'
            f'  <summary>{d.isoformat()}<span class="group-count">'
            f'{len(items)} item{"s" if len(items) != 1 else ""}</span></summary>\n'
            f'  <div class="group-body">\n{body}\n  </div>\n'
            f'</details>'
        )

    body_html = "\n".join(sections) if sections else (
        '<div class="empty">No articles yet — run the pipeline to populate this index.</div>'
    )
    total = sum(len(v) for v in articles_by_date.values())

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tech-radar: {escape(profile_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<h1>tech-radar: {escape(profile_name)}</h1>
<div class="meta">All {total} item{"s" if total != 1 else ""}, grouped by run date — newest first</div>

<div class="toolbar">
  <input id="search" type="search" placeholder="Search title, summary, topics, affiliations..." aria-label="Search articles">
  <label><input id="bookmarks-only" type="checkbox"> ★ only (<span id="bookmark-count">0</span>)</label>
  <button id="export-bookmarks" type="button" title="Download bookmarks as JSON">Export ★</button>
  <button id="import-bookmarks" type="button" title="Merge bookmarks from a JSON file">Import ★</button>
  <input id="import-file" type="file" accept="application/json,.json" style="display:none">
  <span id="count" class="count">{total} total</span>
</div>

{body_html}
</div>
<script>{_JS}</script>
</body>
</html>
"""


def write_index_html(
    out_dir: Path,
    profile_name: str,
    articles_by_date: dict[date, list[TaggedArticle]],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    content = render_index_html(articles_by_date, profile_name=profile_name)
    path = out_dir / "index.html"
    path.write_text(content, encoding="utf-8")
    return path
