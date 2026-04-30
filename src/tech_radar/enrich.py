"""Post-tagging enrichment.

Three steps per article:
  1. arXiv scrape: fetch the abstract page and extract any GitHub link.
  2. GitHub search: if arXiv scrape found nothing, search GitHub by title
     keywords, filtered to repos created on-or-after the paper's publication
     date to suppress false positives.
  3. License lookup: once code_url is known, call the GitHub API for the
     SPDX licence identifier.

Steps 1-2 run for ALL articles that lack a code_url, regardless of whether
the tagger set has_code — papers sometimes publish code without mentioning
it in the abstract.

Rate limits (unauthenticated):
  - GitHub REST API:  60 req/hr
  - GitHub Search API: 10 req/min
Both are tracked separately and bail gracefully when hit.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Iterable

import httpx
from pydantic import HttpUrl, TypeAdapter

from tech_radar.schemas import TaggedArticle

_http_url_ta: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)

logger = logging.getLogger(__name__)

_GITHUB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:[/#?].*)?$",
    re.IGNORECASE,
)

_GITHUB_REPO_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9][A-Za-z0-9_-]*)/([\w.-]+?)(?=[/#?\"'\s<]|$)",
    re.IGNORECASE,
)

_GITHUB_NOISE_OWNERS = frozenset({
    "topics", "explore", "search", "about", "features", "enterprise",
    "pricing", "collections", "trending", "marketplace", "sponsors",
    "organizations", "users", "orgs",
})

# Common words stripped before building a GitHub search query.
_STOP_WORDS = frozenset({
    "a", "an", "the", "for", "of", "in", "on", "with", "via", "and", "or",
    "to", "from", "using", "based", "towards", "toward", "novel", "new",
    "efficient", "effective", "learning", "model", "method", "approach",
    "network", "framework", "system", "its",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _github_owner_repo(url: str) -> tuple[str, str] | None:
    m = _GITHUB_RE.match(url.rstrip("/"))
    if not m:
        return None
    return m.group(1), m.group(2)


def _extract_github_repo_url(html: str) -> str | None:
    """Return the first plausible owner/repo GitHub URL found in raw HTML."""
    for m in _GITHUB_REPO_RE.finditer(html):
        owner, repo = m.group(1), m.group(2).rstrip(".")
        if owner.lower() in _GITHUB_NOISE_OWNERS or not repo:
            continue
        return f"https://github.com/{owner}/{repo}"
    return None


def _scrape_arxiv(client: httpx.Client, article: TaggedArticle) -> str | None:
    """Fetch the arXiv abstract page and extract any GitHub link."""
    if article.source.value != "arxiv":
        return None
    arxiv_id = article.id.removeprefix("arxiv:")
    try:
        r = client.get(f"https://arxiv.org/abs/{arxiv_id}", timeout=10.0)
    except httpx.HTTPError as exc:
        logger.debug("arXiv fetch failed for %s: %s", arxiv_id, exc)
        return None
    if r.status_code != 200:
        return None
    return _extract_github_repo_url(r.text)


def _title_keywords(title: str, n: int = 6) -> list[str]:
    """Extract up to n distinctive words from a paper title."""
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", title).split()
    return [w for w in words if w.lower() not in _STOP_WORDS][:n]


def _search_github(
    client: httpx.Client,
    title: str,
    published_at: datetime,
) -> str | None:
    """Search GitHub for a repo matching the paper title.

    Restricts to repos created on-or-after the paper's publication date so
    unrelated older repos with similar names don't match.
    Requires at least one title keyword to appear in the repo name or
    description as a second-pass filter.
    """
    keywords = _title_keywords(title)
    if not keywords:
        return None

    query = " ".join(keywords)
    date_str = published_at.strftime("%Y-%m-%d")

    try:
        r = client.get(
            "https://api.github.com/search/repositories",
            params={"q": f"{query} created:>={date_str}", "sort": "stars", "per_page": 5},
            timeout=8.0,
        )
    except httpx.HTTPError as exc:
        logger.debug("GitHub search failed for '%s': %s", query, exc)
        return None

    if r.status_code == 403:
        logger.warning("GitHub search rate-limited")
        raise RuntimeError("github_search_rate_limit")
    if r.status_code == 422:
        # Unprocessable: date filter too far in past etc. — skip silently.
        return None
    if r.status_code != 200:
        logger.debug("GitHub search status %s for '%s'", r.status_code, query)
        return None

    items = r.json().get("items", [])
    if not items:
        return None

    kw_set = {k.lower() for k in keywords}
    for item in items:
        repo_text = f"{item.get('name', '')} {item.get('description') or ''}".lower()
        if any(kw in repo_text for kw in kw_set):
            return item["html_url"]

    return None


def _fetch_license(client: httpx.Client, owner: str, repo: str) -> str | None:
    """Returns the SPDX id (e.g. 'MIT', 'Apache-2.0') or None if unknown."""
    try:
        r = client.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=8.0)
    except httpx.HTTPError as e:
        logger.warning("GitHub API error for %s/%s: %s", owner, repo, e)
        return None

    if r.status_code == 404:
        return None
    if r.status_code == 403:
        logger.warning("GitHub rate-limited; skipping remaining license lookups")
        raise RuntimeError("github_rate_limit")
    if r.status_code != 200:
        logger.warning("GitHub API %s for %s/%s", r.status_code, owner, repo)
        return None

    body = r.json()
    lic = body.get("license") or {}
    return lic.get("spdx_id") or lic.get("name")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_articles(
    articles: Iterable[TaggedArticle],
    *,
    discover: bool = True,
    search_github: bool = True,
    arxiv_delay: float = 0.3,
    github_search_delay: float = 1.2,
) -> list[TaggedArticle]:
    """Enrich articles with a code URL (discovery) and a license (GitHub API).

    Discovery runs for every article that lacks a code_url, regardless of the
    tagger's has_code flag — papers often release code without mentioning it
    in the abstract.

    Args:
        articles: Articles to enrich. Mutated in place when data is found.
        discover: Whether to attempt arXiv page scraping.
        search_github: Whether to fall back to GitHub keyword search when
            arXiv scraping finds nothing.
        arxiv_delay: Seconds to sleep between arXiv page requests.
        github_search_delay: Seconds to sleep between GitHub search requests
            (search API allows ~10 req/min unauthenticated).

    Returns:
        All input articles, enriched where data was found.
    """
    enriched: list[TaggedArticle] = []
    license_rate_limited = False
    search_rate_limited = False

    with httpx.Client(headers={"Accept": "application/vnd.github+json"}) as client:
        for article in articles:

            # ------------------------------------------------------------------
            # Step 1: discover code URL for any article still missing one.
            # ------------------------------------------------------------------
            if not article.tags.code_url and not license_rate_limited:
                found_url: str | None = None

                if discover:
                    found_url = _scrape_arxiv(client, article)
                    time.sleep(arxiv_delay)

                if not found_url and search_github and not search_rate_limited:
                    try:
                        found_url = _search_github(client, article.title, article.published_at)
                    except RuntimeError:
                        search_rate_limited = True
                    time.sleep(github_search_delay)

                if found_url:
                    try:
                        article.tags.code_url = _http_url_ta.validate_python(found_url)
                        article.tags.has_code = True
                        logger.debug("Discovered %s → %s", article.id, found_url)
                    except Exception:
                        logger.debug("Invalid URL from discovery for %s: %s", article.id, found_url)

            # ------------------------------------------------------------------
            # Step 2: fetch the licence once we have a GitHub URL.
            # ------------------------------------------------------------------
            if article.tags.code_url and not article.tags.license and not license_rate_limited:
                pair = _github_owner_repo(str(article.tags.code_url))
                if pair is not None:
                    try:
                        license_id = _fetch_license(client, *pair)
                        if license_id and license_id != "NOASSERTION":
                            article.tags.license = license_id
                    except RuntimeError:
                        license_rate_limited = True

            enriched.append(article)

    n_url = sum(1 for a in enriched if a.tags.code_url)
    n_lic = sum(1 for a in enriched if a.tags.license)
    logger.info(
        "Enrich: %d/%d with code URL, %d/%d with license",
        n_url, len(enriched), n_lic, len(enriched),
    )
    return enriched
