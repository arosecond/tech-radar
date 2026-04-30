# tech-radar

A multi-agent curation pipeline that scans arXiv (and, in Phase 2, Hugging Face + tech blogs), screens items against a configurable interest profile, and produces a daily Markdown digest with summaries, key points, and tags.

The current profile in this repo is **3D reconstruction** (NeRF, 3D Gaussian Splatting, MVS, SfM, depth estimation, mesh recovery), but the profile is just a YAML file — swap it for any topic.

**Operating cost: $0/month.** Every stage in the default routing runs on a local Qwen 3.6-27B (served by llama.cpp) — no paid API calls anywhere in the stack. The pipeline is **multi-provider by design**: stage-to-model routing lives in a single YAML, and any stage can be flipped to a cloud provider (Gemini/OpenAI/etc.) by changing one line.

## Why this exists

The point of the project is to demonstrate the building blocks of an LLM curation system end-to-end:

- **Multi-provider, single-config routing** — `config/models.yaml` maps each stage (Filter / Summarizer / Tagger / Ranker) to a provider+model. The same client code works against any OpenAI-compatible backend. Default routing is all-local for cost reasons; flip any `provider:` to re-route a stage to a cloud model.
- **Structured output** via `response_format=json_object` + Pydantic validation + retry + markdown-fence rescue, so JSON failures don't drop articles.
- **Source layer kept narrow and swappable** (arXiv → HF → RSS, all behind the same `Article` schema).
- **Thinking-mode A/B**: `enable_thinking` is per-stage in YAML; a sibling `models-thinking.yaml` is included for side-by-side comparison runs.

## Architecture

```
Sources                  Agents                                        Storage           Output
─────────                ────────                                      ─────────         ─────────
arXiv API   ─┐
HF Hub MCP  ─┼── Article ─→ Filter (provider per models.yaml)
RSS feeds   ─┘                  │ keep?
                                ▼
                            Summarizer (provider per models.yaml)
                                │
                                ▼
                            Tagger (provider per models.yaml) ─→ DuckDB ─→ Markdown digest
                                                                      └→ Notion DB
```

### Default routing (all-local)

| Stage | Provider | Notes |
|---|---|---|
| Filter | local Qwen 3.6-27B via llama.cpp | High-volume binary screening |
| Summarizer | local Qwen 3.6-27B via llama.cpp | `enable_thinking: true` (chosen via A/B against thinking-off; thinking captures formulas and exact benchmark names more faithfully) |
| Tagger | local Qwen 3.6-27B via llama.cpp | Constrained vocabulary + JSON mode |
| Ranker (Phase 2) | TBD | Likely Summarizer-equivalent |

A hybrid mori+Gemini routing was prototyped first; on this account the Gemini free tier capped at ~20 req/day, which was too tight for daily batch runs, so the default routing is all-local. The multi-provider plumbing is intact — flip any `provider:` in `config/models.yaml` to re-route a stage without touching code.

## Project status

- **Phase 1 (this repo):** arXiv source · Filter / Summarizer / Tagger · DuckDB dedup · Markdown digest · **Notion DB output** · CLI · multi-provider client
- **Phase 2:** Hugging Face source · RSS source · Ranker agent
- **Phase 3:** Raspberry Pi cron deploy · Langfuse observability

## Setup

```bash
# Install deps (uv handles the venv)
uv sync

# Configure providers
cp .env.example .env
# MORI_BASE_URL defaults to http://localhost:8080/v1 — change only if your llama.cpp lives elsewhere
# GEMINI_API_KEY is optional: only needed if you flip a stage to provider: gemini in config/models.yaml

# Sanity-check loaded configs
uv run tech-radar show-profile
uv run tech-radar show-models

# Probe the configured providers with a tiny structured call
uv run tech-radar ping
```

### Local Qwen requirement

The default routing sends every stage to a llama.cpp `server` running Qwen 3.6-27B (or any compatible model) at `MORI_BASE_URL`. A working `docker-compose` for `unsloth/Qwen3.6-27B-GGUF` (UD-Q4_K_XL on a 24GB GPU) is documented separately. If you don't have a local GPU, edit `config/models.yaml` and flip the stages to `provider: gemini` (and set `GEMINI_API_KEY` in `.env`).

## Run

```bash
# Daily digest, 2-day lookback
uv run tech-radar run

# Wider window, verbose logs, dry run (don't mark items seen)
uv run tech-radar run --lookback-days 7 --dry-run --verbose

# Skip the Notion push for a run
uv run tech-radar run --no-notion
```

The digest lands in `output/digest-YYYY-MM-DD.md`. If Notion is configured, each
tagged article is also upserted as a page in the configured database.

### Notion output

Each run upserts every tagged article into a Notion database, keyed by the
arXiv id stored in DuckDB:

- **First run with `NOTION_PARENT_PAGE_ID` set:** bootstraps a new database
  under that page with the full schema (Title, Topics, Method type, Score for
  Phase 2 Ranker, …). The new database id is printed; move it to
  `NOTION_DATABASE_ID` to skip bootstrap on subsequent runs.
- **Subsequent runs with `NOTION_DATABASE_ID` set:** new articles are created
  as pages, previously-seen articles get their properties updated.

To backfill already-tagged articles (or re-sync everything after editing the
schema), run:

```bash
# Push articles in DuckDB that don't yet have a notion_page_id
uv run tech-radar notion-sync

# Force-update every article (re-pushes properties for all pages)
uv run tech-radar notion-sync --all
```

Pushes are best-effort — a Notion error never aborts the pipeline; the
markdown digest is the source of truth.

## Configuration

- `config/interest_profile.yaml` — topics with weights, plus reader context for the Ranker
- `config/sources.yaml` — which arXiv categories, HF queries, and RSS feeds to fetch
- `config/models.yaml` — which provider/model handles each stage

All three are hot-swappable. Replace the profile YAML to retarget the curator at a different topic; replace `models.yaml` to re-route stages between providers without touching code.

## Layout

```
src/tech_radar/
├── schemas.py            # Pydantic models — the spine of the pipeline
├── sources/
│   └── arxiv_source.py   # arXiv API → Article
├── agents/
│   ├── _client.py        # OpenAI-compatible multi-provider client + Pydantic structured output
│   ├── filter_agent.py   # Stage-agnostic; routed via models.yaml
│   ├── summarizer.py     # Stage-agnostic; routed via models.yaml
│   └── tagger.py         # Stage-agnostic; routed via models.yaml
├── outputs/
│   ├── markdown.py       # Daily digest renderer
│   └── notion.py         # Notion DB upsert (data sources API, throttled)
├── storage.py            # DuckDB: dedup + tagged-article archive
├── pipeline.py           # Orchestration + stage→model resolution
└── cli.py                # Typer entrypoint (run / show-profile / show-models / ping)
```
