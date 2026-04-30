# tech-radar

> 🇯🇵 日本語版: [README.md](README.md)

A multi-agent LLM curation pipeline that scans arXiv (and, in Phase 2, Hugging Face + tech blogs), filters items against a configurable interest profile, and produces a daily Markdown digest with per-paper summaries, key points, and tags. A Notion database mirror and Slack daily-success notification are included.

The interest profile in this repo is **3D reconstruction** (NeRF, 3D Gaussian Splatting, MVS, SfM, depth estimation, mesh recovery, feed-forward 3D), but it's just a YAML — swap it for any topic.

**Operating cost: $0/month.** Default routing runs every stage on a local Qwen 3.6-27B (served by llama.cpp) — no paid API calls anywhere in the stack. The pipeline is **multi-provider by design**: stage-to-model routing lives in a single YAML, and any stage can be flipped to a cloud provider (Gemini / OpenAI-compatible / etc.) by changing one line.

## What it produces

A daily Markdown file like `output/digest-2026-04-30.md`, plus the same articles upserted into a Notion database keyed by arXiv id. **A representative excerpt is checked into [`docs/sample-digest.md`](docs/sample-digest.md)**.

Per paper, the Summarizer emits a constrained Pydantic schema:

- **TL;DR** — 1–2 sentence headline
- **Key points** — 3–5 technical bullets (faithful to the abstract; preserves formulas and benchmark names)
- **Why it matters** — single-sentence framing against the interest profile
- **Technical details** — 性能 / 処理速度 / 必要GPU （unknown fields fall back to `N/A` rather than hallucinating）
- **Repository** — GitHub URL + SPDX license, fetched live from the GitHub API at the enrich step
- **Tags + paper type + datasets** — produced by the Tagger stage against a constrained vocabulary

## Why this exists

The point of the project is to demonstrate the building blocks of an LLM curation system end-to-end:

- **Multi-provider, single-config routing** — `config/models.yaml` maps each stage (Filter / Summarizer / Tagger / Ranker) to a provider+model. The same client code works against any OpenAI-compatible backend. Default routing is all-local for cost reasons; flip any `provider:` to re-route a stage to a cloud model.
- **Structured output that doesn't drop articles** — `response_format=json_object` + Pydantic validation + automatic retry + markdown-fence rescue. JSON failures from the local model are recovered, not lost.
- **Stage-level thinking-mode A/B** — `enable_thinking` is per-stage in YAML. A sibling `models-thinking.yaml` is included for side-by-side runs, so promotion decisions are based on real comparisons (see _Engineering decisions_ below).
- **Source layer kept narrow and swappable** — arXiv → HF → RSS, all behind the same `Article` schema.
- **Cumulative digest** — a single failed run never erases past output; `tech-radar render` re-builds the digest from DuckDB without calling any LLM ($0 to regenerate).

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
                                                                      ├→ Notion DB
                                                                      └→ Slack notify
```

### Default routing (all-local)

| Stage | Provider | Notes |
|---|---|---|
| Filter | local Qwen 3.6-27B via llama.cpp | High-volume binary screening |
| Summarizer | local Qwen 3.6-27B via llama.cpp | `enable_thinking: true` (chosen via A/B; thinking captures formulas and exact benchmark names more faithfully) |
| Tagger | local Qwen 3.6-27B via llama.cpp | Constrained vocabulary + JSON mode |
| Ranker (Phase 2) | TBD | Likely Summarizer-equivalent |

A hybrid mori+Gemini routing was prototyped first; on this account the Gemini free tier capped at ~20 req/day, which was too tight for daily batch runs, so the default routing is all-local. The multi-provider plumbing is intact — flip any `provider:` in `config/models.yaml` to re-route a stage without touching code.

## Engineering decisions

A few trade-offs are worth pulling out — the kind of thing a reviewer would want to know about a real system:

- **Hybrid → all-local.** The original design routed Summarizer to Gemini Flash for higher-quality writing while keeping Filter / Tagger on a local 27B model. The Gemini AI Studio free tier on this account turned out to be ~20 requests/day total (model-independent), which is too tight for batch daily runs. Rather than pay, the project moved to all-local. The multi-provider plumbing — and the rationale for splitting work across providers — survives in the code, so the routing can be flipped back the moment cost becomes acceptable.
- **Thinking ON for Summarizer (A/B-confirmed).** A side-by-side run on 22 papers with `enable_thinking: true` vs `false` showed thinking-on preserved math notation (`$x_\theta$`), named the actual benchmark cells (e.g. _1-NFE_), and avoided "hand-wavy" rephrasing. Thinking-on also blows the default `max_tokens` budget, so it's paired with `max_tokens: 4500`; in the same run, both configurations produced 0 broken JSON outputs. Filter and Tagger stay thinking-off — they don't need it and it adds latency.
- **Cumulative digest.** Early runs lost output when a single stage failed mid-pipeline. The fix: every successfully tagged article is committed to DuckDB before rendering. The digest renderer pulls the last N days from DuckDB and writes a single Markdown file. `tech-radar render` is a $0 command that re-builds the digest from storage without calling any LLM — useful for prompt iteration.
- **Dedup that survives partial failure.** Articles are only written to the `seen` table after the Tagger stage completes for them — not after Filter. So a tagging failure mid-batch doesn't lock that article out forever; the next run picks it back up.
- **Rate-limit handling lives in the client, not the agents.** The OpenAI-compatible client throttles Gemini at 6.5 sec/req and retries on 503 from llama.cpp with a 10-second backoff. Agents stay agnostic to which backend they're hitting.
- **Enrich step on top of the pipeline.** After tagging, repository URLs are passed to the GitHub API to fetch the actual SPDX license string. Avoids the model hallucinating "Apache 2.0" because it sounds plausible — the field is either correct or `N/A`.

## Project status

- **Phase 1 (this repo):** arXiv source · Filter / Summarizer / Tagger · DuckDB dedup · Markdown digest · Notion DB output · Slack notification · daily Windows Task Scheduler auto-run · CLI · multi-provider client
- **Phase 2:** Hugging Face source · RSS source · Ranker agent · author-affiliation highlight (Semantic Scholar) for digest / Notion / Slack
- **Phase 3:** Raspberry Pi cron deploy · Langfuse observability

## Operations

The repo is set up for unattended daily runs. On the development machine:

- `run_pipeline.bat` is registered with **Windows Task Scheduler** (`TechRadarPipeline`, daily 06:05). It:
    1. Starts the Docker service and the Docker Desktop GUI (the daemon won't come up without it).
    2. Polls `docker ps` until the daemon is ready (up to 5 minutes).
    3. Runs `python -m tech_radar.cli run`.
    4. If Docker fails to come up in time, calls `tech-radar notify-docker-fail` to post a Slack alert with the log path.
- The llama.cpp container `llamacpp-mori` runs with `restart: unless-stopped`, so it comes back up automatically the moment Docker is ready.
- **Slack notifications** (Incoming Webhook; URL goes in `.env` as `SLACK_WEBHOOK_URL`):
    - **Success**: number of new papers and the first 20 titles, posted to a configured channel.
    - **Failure**: stage where it broke + log path.
    - **Docker not ready**: the bat-script-level alert above.
    - If `SLACK_WEBHOOK_URL` is unset, all notify calls become silent no-ops.
- Logs land in `data/logs/pipeline_YYYYMMDD.log` (gitignored).

## Setup

```bash
# Install deps (uv handles the venv)
uv sync

# Configure providers
cp .env.example .env
# MORI_BASE_URL defaults to http://localhost:8080/v1 — change only if your llama.cpp lives elsewhere
# GEMINI_API_KEY is optional: only needed if you flip a stage to provider: gemini in config/models.yaml
# NOTION_* and SLACK_WEBHOOK_URL are optional — pipeline runs without them

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

# Re-render the digest from DuckDB without calling any LLM ($0)
uv run tech-radar render
```

The digest lands in `output/digest-YYYY-MM-DD.md`. If Notion is configured, each tagged article is also upserted as a page in the configured database. If Slack is configured, a success summary is posted.

### Notion output

Each run upserts every tagged article into a Notion database, keyed by the arXiv id stored in DuckDB:

- **First run with `NOTION_PARENT_PAGE_ID` set:** bootstraps a new database under that page with the full schema (Title, Topics, Method type, Score for Phase 2 Ranker, …). The new database id is printed; move it to `NOTION_DATABASE_ID` to skip bootstrap on subsequent runs.
- **Subsequent runs with `NOTION_DATABASE_ID` set:** new articles are created as pages, previously-seen articles get their properties updated.

To backfill already-tagged articles (or re-sync everything after editing the schema):

```bash
# Push articles in DuckDB that don't yet have a notion_page_id
uv run tech-radar notion-sync

# Force-update every article (re-pushes properties for all pages)
uv run tech-radar notion-sync --all
```

Pushes are best-effort — a Notion error never aborts the pipeline; the markdown digest is the source of truth.

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
├── enrich.py             # GitHub API → SPDX license per repo
├── notifier.py           # Slack Incoming Webhook (success / error / docker-fail)
├── storage.py            # DuckDB: dedup + tagged-article archive
├── pipeline.py           # Orchestration + stage→model resolution
└── cli.py                # Typer entrypoint (run / render / notion-sync / notify-docker-fail / show-profile / show-models / ping)
```

## License

MIT — see [`LICENSE`](LICENSE).
