"""Pydantic schemas for the curation pipeline.

Articles flow through the pipeline gaining attributes at each stage:
  Article -> FilteredArticle -> SummarizedArticle -> TaggedArticle -> RankedArticle

Each stage's output is the next stage's input.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class SourceType(str, Enum):
    ARXIV = "arxiv"
    HUGGINGFACE = "huggingface"
    RSS = "rss"


class Article(BaseModel):
    """Raw article fetched from a source. Common shape across all sources."""

    id: str = Field(..., description="Stable unique id (arxiv id, HF paper id, URL hash for RSS)")
    source: SourceType
    source_name: str = Field(..., description="Human-readable source label, e.g. 'arXiv cs.CV'")
    title: str
    url: HttpUrl
    abstract: str = Field(default="", description="Abstract or excerpt; may be empty for some RSS items")
    authors: list[str] = Field(default_factory=list)
    published_at: datetime
    raw: dict = Field(default_factory=dict, description="Source-specific raw payload, kept for debugging")


class FilterDecision(BaseModel):
    """Output of the Filter agent (Haiku) — single article cheap screening."""

    keep: bool
    reason: str = Field(..., max_length=200, description="One short sentence explaining the decision")
    relevance_hint: Literal["core", "adjacent", "peripheral", "off-topic"] = "off-topic"


class FilteredArticle(Article):
    decision: FilterDecision


class TechnicalDetails(BaseModel):
    """Optional technical specifics extracted from the abstract.

    All fields are nullable — set to null when the abstract doesn't say.
    Don't make numbers up.
    """

    performance: str | None = Field(
        None,
        description="Quantitative results from the paper (metric + value + benchmark). "
        "Example: 'PSNR 28.5 on Mip-NeRF360 / 92.3% accuracy on KITTI'. null if not stated.",
    )
    speed: str | None = Field(
        None,
        description="Inference / training speed. Example: '60 FPS at 1080p / trains in 30 min'. null if not stated.",
    )
    gpu_requirements: str | None = Field(
        None,
        description="Hardware used or required. Example: 'Single RTX 3090 (24GB VRAM)'. null if not stated.",
    )


class Summary(BaseModel):
    """Output of the Summarizer agent (Sonnet)."""

    tldr: str = Field(..., max_length=300, description="One sentence, plain language")
    key_points: list[str] = Field(..., min_length=2, max_length=5)
    novelty: str = Field(..., max_length=300, description="What's new vs prior art, in one sentence")
    why_it_matters: str = Field(..., max_length=300)
    technical_details: TechnicalDetails = Field(default_factory=TechnicalDetails)


class SummarizedArticle(FilteredArticle):
    summary: Summary


class Tags(BaseModel):
    """Output of the Tagger agent (Haiku)."""

    topics: list[str] = Field(..., min_length=1, max_length=6, description="Topic labels from interest profile vocabulary")
    method_type: Literal[
        "novel_method",
        "improvement",
        "survey",
        "benchmark",
        "tool_or_release",
        "application",
        "other",
    ]
    has_code: bool
    code_url: HttpUrl | None = None
    datasets: list[str] = Field(default_factory=list)
    # `license` is not produced by the LLM; it's filled by the enrichment step
    # using the GitHub API once `code_url` points at a repo.
    license: str | None = None


class Affiliations(BaseModel):
    """Author affiliations enriched from OpenAlex post-tagging.

    Empty `institutions` either means OpenAlex had no data (typical for very
    recent arXiv-only papers) or enrichment hasn't run yet — the digest just
    omits the marker in either case.
    """

    institutions: list[str] = Field(
        default_factory=list,
        description="Unique institution display names returned by OpenAlex authorships",
    )
    notable_matches: list[str] = Field(
        default_factory=list,
        description="Subset of `institutions` that matched a pattern in notable_institutions.yaml",
    )


class RankScore(BaseModel):
    """Output of the Ranker agent: per-article interest-fit score."""

    score: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., max_length=200)


class TaggedArticle(SummarizedArticle):
    tags: Tags
    affiliations: Affiliations = Field(default_factory=Affiliations)
    # Populated by the Ranker stage when enabled in models.yaml; older payloads
    # round-trip through DB with rank=None.
    rank: RankScore | None = None
