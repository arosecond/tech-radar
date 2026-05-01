"""Post-tagging enrichment.

Two enrich passes run after tagging:

  enrich_articles(...)        Code URL discovery + SPDX license (GitHub API)
  enrich_affiliations(...)    Author institutions via OpenAlex

Both pass through the article list and mutate fields in place. They're
separated so each can be skipped, rate-limit-bailed, or retried independently.

Within `enrich_articles`:
  1. arXiv scrape: fetch the abstract page and extract any GitHub link.
  2. GitHub search: if arXiv scrape found nothing, search GitHub by title
     keywords, filtered to repos created on-or-after the paper's publication
     date to suppress false positives.
  3. License lookup: once code_url is known, call the GitHub API for the
     SPDX licence identifier.

Rate limits (unauthenticated):
  - GitHub REST API:    60 req/hr
  - GitHub Search API:  10 req/min
  - OpenAlex polite:   ~100k req/day with mailto in User-Agent
All three are tracked separately and bail gracefully when hit.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Iterable

import httpx
from pydantic import BaseModel, Field, HttpUrl, TypeAdapter

from tech_radar.agents._client import ModelSpec, call_structured
from tech_radar.schemas import Affiliations, TaggedArticle

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


# ---------------------------------------------------------------------------
# OpenAlex affiliation enrichment
# ---------------------------------------------------------------------------

# OpenAlex asks API consumers to identify themselves; doing so puts requests
# in the "polite pool" with much higher per-day quota. Set OPENALEX_MAILTO
# to a contact email to opt in.
_OPENALEX_BASE = "https://api.openalex.org"


def _openalex_user_agent() -> str:
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    repo = "https://github.com/arosecond/tech-radar"
    if mailto:
        return f"tech-radar/0.1 ({repo}; mailto:{mailto})"
    return f"tech-radar/0.1 ({repo})"


def _fetch_openalex_institutions(
    client: httpx.Client,
    arxiv_id: str,
) -> list[str] | None:
    """Return institution display names from OpenAlex authorships, or None on transport error.

    An empty list (vs None) means the API responded but had no affiliation
    data — common for arXiv-only / pre-conference papers.
    """
    # OpenAlex registers arXiv works under the versionless DOI (e.g.
    # 10.48550/arXiv.2604.15941). Versioned DOIs (`...v1`, `...v2`) 404.
    versionless = re.sub(r"v\d+$", "", arxiv_id)
    try:
        r = client.get(
            f"{_OPENALEX_BASE}/works/doi:10.48550/arXiv.{versionless}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.debug("OpenAlex fetch failed for %s: %s", arxiv_id, exc)
        return None

    if r.status_code == 404:
        return []
    if r.status_code == 429:
        logger.warning("OpenAlex rate-limited; skipping remaining lookups")
        raise RuntimeError("openalex_rate_limit")
    if r.status_code != 200:
        logger.debug("OpenAlex %s for %s", r.status_code, arxiv_id)
        return []

    institutions: list[str] = []
    for authorship in r.json().get("authorships", []):
        for inst in authorship.get("institutions", []):
            name = inst.get("display_name")
            if name:
                institutions.append(name)
    return institutions


# ---------------------------------------------------------------------------
# LLM fallback: extract affiliations from the paper's PDF first page
# ---------------------------------------------------------------------------
#
# OpenAlex covers a substantial back-catalogue but has thin or no affiliation
# data for many arXiv-only / pre-conference papers. We download the PDF, pull
# the first page's text, and ask the local LLM to extract the institutions
# directly. The LLM tolerates the typical pdfplumber quirks (no inter-word
# spaces in narrow columns, "TheUniversityofTokyo") far better than regex.

_ARXIV_PDF_BASE = "https://arxiv.org/pdf"
_PAGE1_TEXT_CAP = 3000  # characters; affiliations are always in the header block


class _AffiliationExtraction(BaseModel):
    """LLM response shape for the affiliation extraction stage."""

    institutions: list[str] = Field(
        default_factory=list,
        description="Unique author affiliations (universities, labs, companies). "
        "Normalize obvious word-spacing artifacts like 'TheUniversityofTokyo' to "
        "'The University of Tokyo'. Return [] if no affiliations are visible.",
    )


def _fetch_pdf_page1_text(client: httpx.Client, arxiv_id: str) -> str | None:
    """Download the arXiv PDF and return text from page 1, or None on any failure.

    Stripped of versioned suffix on the URL because arxiv.org serves the latest
    version under the bare id and we don't care about pinning here.
    """
    versionless = re.sub(r"v\d+$", "", arxiv_id)
    url = f"{_ARXIV_PDF_BASE}/{versionless}"
    try:
        r = client.get(url, timeout=30.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.debug("PDF fetch failed for %s: %s", arxiv_id, exc)
        return None
    if r.status_code != 200 or not r.content:
        logger.debug("PDF fetch returned %s for %s", r.status_code, arxiv_id)
        return None

    try:
        import pdfplumber  # local import: keeps import cost off the hot path
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            if not pdf.pages:
                return None
            text = pdf.pages[0].extract_text() or ""
    except Exception as exc:  # noqa: BLE001 — pdfplumber raises a zoo of exceptions
        logger.debug("PDF parse failed for %s: %s", arxiv_id, exc)
        return None

    return text[:_PAGE1_TEXT_CAP] if text else None


_LLM_SYSTEM = """You extract author affiliations from the first page of a research paper.

Return a flat, deduplicated list of institution names (universities, research \
labs, companies). Rules:
- Normalize obvious word-spacing artifacts from PDF extraction
  (e.g. "TheUniversityofTokyo" -> "The University of Tokyo").
- Use the canonical full name when both the abbreviation and full form appear.
- Do NOT include department names, lab names, or country/city alone.
- Do NOT include author names, emails, conference names, or copyright lines.
- If no affiliations are visible on the first page, return an empty list."""


def _extract_via_llm(
    page1_text: str,
    spec: ModelSpec,
    **call_kwargs: Any,
) -> list[str]:
    """Run the LLM-based affiliation extraction on a chunk of page-1 text.

    Returns institutions in document order, deduplicated. Empty list on any
    LLM failure (caller treats this the same as "no data").
    """
    user = (
        "Extract affiliations from the first-page text below.\n\n"
        "----- PAGE 1 -----\n"
        f"{page1_text}\n"
        "----- END -----\n"
    )
    try:
        result = call_structured(
            spec=spec,
            system=_LLM_SYSTEM,
            user=user,
            response_model=_AffiliationExtraction,
            tool_name="affiliations",
            **call_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — treat any LLM failure as "no data"
        logger.warning("LLM affiliation extract failed: %s", exc)
        return []
    return list(dict.fromkeys(result.institutions))  # dedup, preserve order


def _match_notable(institutions: list[str], notable_lower: list[str]) -> list[str]:
    """Return institutions whose name contains any notable pattern (case-insensitive). Dedup, preserve order."""
    seen: set[str] = set()
    matches: list[str] = []
    for inst in institutions:
        if inst in seen:
            continue
        if any(pat in inst.lower() for pat in notable_lower):
            matches.append(inst)
            seen.add(inst)
    return matches


def enrich_affiliations(
    articles: Iterable[TaggedArticle],
    notable_patterns: Iterable[str],
    *,
    delay: float = 0.5,
    llm_spec: ModelSpec | None = None,
    llm_kwargs: dict[str, Any] | None = None,
) -> list[TaggedArticle]:
    """Enrich articles with author institutions and a notable-match flag.

    Mutates articles in place. arXiv-sourced articles only — other sources are
    passed through untouched. Empty results are stored as empty `Affiliations`
    rather than failing.

    Lookup strategy per article (when source is arxiv):
        1. OpenAlex DOI lookup (free, ~instant).
        2. If OpenAlex returned no institutions AND llm_spec is provided,
           download the PDF and let the LLM extract affiliations from page 1.
    """
    notable_lower = [p.lower() for p in notable_patterns]
    llm_kwargs = llm_kwargs or {}
    enriched: list[TaggedArticle] = []
    rate_limited = False
    llm_calls = 0

    headers = {"User-Agent": _openalex_user_agent(), "Accept": "application/json"}
    with httpx.Client(headers=headers) as client:
        for article in articles:
            # Cache hit (pipeline pre-populated from previous run): skip lookups.
            if article.affiliations.institutions:
                enriched.append(article)
                continue
            if article.source.value == "arxiv":
                arxiv_id = article.id.removeprefix("arxiv:")
                institutions: list[str] | None = None

                if not rate_limited:
                    try:
                        institutions = _fetch_openalex_institutions(client, arxiv_id)
                    except RuntimeError:
                        rate_limited = True
                        institutions = None
                    time.sleep(delay)

                if not institutions and llm_spec is not None:
                    page1 = _fetch_pdf_page1_text(client, arxiv_id)
                    if page1:
                        institutions = _extract_via_llm(page1, llm_spec, **llm_kwargs)
                        llm_calls += 1

                if institutions:
                    unique = list(dict.fromkeys(institutions))
                    article.affiliations = Affiliations(
                        institutions=unique,
                        notable_matches=_match_notable(unique, notable_lower),
                    )

            enriched.append(article)

    n_inst = sum(1 for a in enriched if a.affiliations.institutions)
    n_notable = sum(1 for a in enriched if a.affiliations.notable_matches)
    logger.info(
        "Affiliations: %d/%d with institution data, %d/%d with notable match (LLM fallback used %d times)",
        n_inst, len(enriched), n_notable, len(enriched), llm_calls,
    )
    return enriched
