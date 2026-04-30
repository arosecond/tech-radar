"""Typer CLI entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from tech_radar.pipeline import run_pipeline

app = typer.Typer(help="tech-radar: multi-agent tech curation pipeline")
console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


@app.command()
def run(
    profile: Path = typer.Option(
        Path("config/interest_profile.yaml"), help="Interest profile YAML"
    ),
    sources: Path = typer.Option(Path("config/sources.yaml"), help="Sources config YAML"),
    models: Path = typer.Option(Path("config/models.yaml"), help="Stage→model routing YAML"),
    db: Path = typer.Option(Path("data/tech_radar.duckdb"), help="DuckDB file path"),
    out: Path = typer.Option(Path("output"), help="Directory for digest output"),
    lookback_days: int = typer.Option(2, help="How many days back to fetch"),
    digest_window_days: int = typer.Option(1, help="How many days of tagged articles to include in digest"),
    digest_suffix: str = typer.Option("", help="Filename suffix for the digest (e.g. 'thinking' → digest-YYYY-MM-DD-thinking.md)"),
    dry_run: bool = typer.Option(False, help="Skip writing to DB"),
    notion: bool = typer.Option(True, "--notion/--no-notion", help="Push results to Notion DB (requires NOTION_API_TOKEN)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the full pipeline once and write today's digest."""
    import time
    import traceback as _tb

    from tech_radar.notifier import notify_error, notify_success

    load_dotenv()
    _setup_logging(verbose)
    started = time.time()
    try:
        result = run_pipeline(
            profile_path=profile,
            sources_path=sources,
            models_path=models,
            db_path=db,
            output_dir=out,
            lookback_days=lookback_days,
            digest_window_days=digest_window_days,
            digest_suffix=digest_suffix,
            dry_run=dry_run,
            notion_enabled=notion,
        )
    except Exception as e:
        notify_error(
            stage="pipeline",
            error=f"{type(e).__name__}: {e}",
            traceback_str=_tb.format_exc(),
        )
        raise
    notify_success(
        digest_path=result["digest_path"],
        new_articles=result["new_articles"],
        total_in_digest=len(result["digest_articles"]),
        duration_sec=time.time() - started,
    )
    console.print(f"[green]Wrote[/green] {result['digest_path']}")


@app.command("notify-docker-fail")
def notify_docker_fail(
    log: Path = typer.Option(..., help="Path to the pipeline log file"),
) -> None:
    """Send a Slack notification for Docker startup failure (called from the bat)."""
    from tech_radar.notifier import notify_docker_failure

    load_dotenv()
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        text = f"(failed to read log: {e})"
    ok = notify_docker_failure(text)
    console.print(f"Slack notify_docker_failure → {'sent' if ok else 'skipped/failed'}")


@app.command()
def show_profile(
    profile: Path = typer.Option(Path("config/interest_profile.yaml")),
) -> None:
    """Print the loaded interest profile (sanity check)."""
    import yaml

    data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    console.print_json(data=data)


@app.command()
def render(
    profile: Path = typer.Option(Path("config/interest_profile.yaml")),
    db: Path = typer.Option(Path("data/tech_radar.duckdb")),
    out: Path = typer.Option(Path("output")),
    digest_window_days: int = typer.Option(1, help="How many days of tagged articles to include"),
) -> None:
    """Re-render the digest from already-tagged articles in the DB. No LLM calls."""
    import yaml
    from datetime import datetime, timedelta

    from tech_radar.outputs.markdown import render_digest, write_digest
    from tech_radar.storage import Store

    profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    with Store(db) as store:
        articles = store.list_tagged_since(datetime.now() - timedelta(days=digest_window_days))
    md = render_digest(articles, profile_name=profile_data["profile_name"])
    path = write_digest(md, out)
    console.print(f"[green]Rendered[/green] {len(articles)} articles → {path}")


@app.command()
def show_models(
    models: Path = typer.Option(Path("config/models.yaml")),
) -> None:
    """Print the stage → model routing."""
    import yaml

    data = yaml.safe_load(models.read_text(encoding="utf-8"))
    console.print_json(data=data)


@app.command()
def ping(
    models: Path = typer.Option(Path("config/models.yaml")),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Probe each configured provider with a tiny structured call."""
    import yaml
    from pydantic import BaseModel

    from tech_radar.agents._client import ModelSpec, call_structured

    class Pong(BaseModel):
        ok: bool
        message: str

    load_dotenv()
    _setup_logging(verbose)
    cfg = yaml.safe_load(models.read_text(encoding="utf-8"))
    seen: set[tuple[str, str]] = set()
    for stage, params in cfg.items():
        key = (params["provider"], params["model"])
        if key in seen:
            continue
        seen.add(key)
        spec = ModelSpec(provider=params["provider"], model=params["model"])
        try:
            result = call_structured(
                spec=spec,
                system="You are a connectivity checker.",
                user='Reply with {"ok": true, "message": "pong"}.',
                response_model=Pong,
                tool_name="pong",
                max_tokens=1500,  # leave room for any thinking tokens on reasoning models
            )
            console.print(f"[green]OK[/green] {spec.provider}/{spec.model} → {result.message}")
        except Exception as e:
            console.print(f"[red]FAIL[/red] {spec.provider}/{spec.model} → {e}")


@app.command()
def recrawl(
    profile: Path = typer.Option(Path("config/interest_profile.yaml")),
    db: Path = typer.Option(Path("data/tech_radar.duckdb"), help="DuckDB file path"),
    out: Path = typer.Option(Path("output"), help="Directory for digest output"),
    digest_window_days: int = typer.Option(1, help="Days of articles to include in re-rendered digest"),
    digest_suffix: str = typer.Option("", help="Filename suffix for digest"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-crawl ALL stored articles that have has_code=True but no code_url yet.

    Useful after adding URL-discovery to enrich.py to retroactively fill in
    GitHub links and licenses for previously processed articles.
    """
    import yaml
    from datetime import datetime, timedelta

    from tech_radar.enrich import enrich_articles
    from tech_radar.outputs.markdown import render_digest, write_digest
    from tech_radar.schemas import TaggedArticle
    from tech_radar.storage import Store

    load_dotenv()
    _setup_logging(verbose)

    with Store(db) as store:
        # Pull every article stored in the DB (no date filter — backfill all).
        rows = store.conn.execute(
            "SELECT payload FROM tagged_articles ORDER BY processed_at DESC"
        ).fetchall()
        all_articles: list[TaggedArticle] = [
            TaggedArticle.model_validate_json(row[0]) for row in rows
        ]
        targets = [a for a in all_articles if not a.tags.code_url]
        console.print(f"Found [yellow]{len(targets)}[/yellow] articles without a code URL (out of {len(all_articles)} total)")

        if not targets:
            console.print("[green]Nothing to re-crawl.[/green]")
            return

        enriched = enrich_articles(targets)
        found = [a for a in enriched if a.tags.code_url]

        console.print(f"Discovered URLs for [green]{len(found)}[/green] / {len(targets)} articles")
        for article in found:
            store.update_tagged_code(article)
            store.remove_code_pending([article.id])
            console.print(f"  [green]✓[/green] {article.title[:60]} → {article.tags.code_url}")

        store.cleanup_code_pending(max_age_days=30)

        # Re-render digest so the new URLs show up.
        profile_data = yaml.safe_load(profile.read_text(encoding="utf-8"))
        since = datetime.now() - timedelta(days=digest_window_days)
        digest_articles = store.list_tagged_since(since)

    if digest_articles:
        md = render_digest(digest_articles, profile_name=profile_data["profile_name"])
        path = write_digest(md, out, suffix=digest_suffix)
        console.print(f"[green]Re-rendered digest[/green] ({len(digest_articles)} articles) → {path}")


@app.command("notion-sync")
def notion_sync(
    profile: Path = typer.Option(Path("config/interest_profile.yaml")),
    db: Path = typer.Option(Path("data/tech_radar.duckdb")),
    only_missing: bool = typer.Option(
        True,
        "--only-missing/--all",
        help="Only sync articles without a notion_page_id (default). Use --all to update every article.",
    ),
    recreate: bool = typer.Option(
        False,
        "--recreate",
        help="Archive existing Notion pages and create them fresh. Use after re-summarize so bodies reflect new content.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Push tagged articles from DuckDB to Notion. Used for initial backfill or full re-sync."""
    from datetime import date as _date
    import yaml as _yaml

    from tech_radar.outputs.notion import NotionPublisher
    from tech_radar.pipeline import make_notion_publisher
    from tech_radar.storage import Store

    load_dotenv()
    _setup_logging(verbose)

    profile_data = _yaml.safe_load(profile.read_text(encoding="utf-8"))
    publisher: NotionPublisher | None = make_notion_publisher(profile_data)
    if publisher is None:
        console.print("[red]Notion is not configured. Check NOTION_API_TOKEN and NOTION_DATABASE_ID/NOTION_PARENT_PAGE_ID.[/red]")
        raise typer.Exit(code=1)

    with Store(db) as store:
        rows = store.list_all_with_notion_status()

        if recreate:
            with_pages = [(a, pid) for a, pid in rows if pid]
            console.print(f"Archiving [yellow]{len(with_pages)}[/yellow] existing Notion pages...")
            for article, pid in with_pages:
                ok = publisher.archive_page(pid)
                if ok:
                    store.set_notion_page_id(article.id, None)  # clear so re-create proceeds
                console.print(f"  {'[green]arc[/green]' if ok else '[red]FAIL[/red]'}  {article.title[:60]}")
            # After archiving everything, re-fetch state.
            rows = store.list_all_with_notion_status()

        targets = [(a, pid) for a, pid in rows if (not only_missing or pid is None)]
        console.print(f"Syncing [yellow]{len(targets)}[/yellow] articles (out of {len(rows)} total)")
        if not targets:
            console.print("[green]Nothing to sync.[/green]")
            return

        run_date = _date.today().isoformat()
        created = updated = failed = 0
        for article, existing in targets:
            page_id = publisher.upsert(article, existing, run_date=run_date)
            if page_id is None:
                failed += 1
                console.print(f"  [red]FAIL[/red] {article.title[:60]}")
                continue
            if existing:
                updated += 1
                console.print(f"  [yellow]upd[/yellow]  {article.title[:60]}")
            else:
                store.set_notion_page_id(article.id, page_id)
                created += 1
                console.print(f"  [green]new[/green]  {article.title[:60]}")

    console.print(
        f"[green]Done.[/green] created={created} updated={updated} failed={failed}"
    )


@app.command("re-summarize")
def re_summarize(
    profile: Path = typer.Option(Path("config/interest_profile.yaml")),
    models: Path = typer.Option(Path("config/models.yaml")),
    db: Path = typer.Option(Path("data/tech_radar.duckdb")),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-run summarizer + tagger on every TaggedArticle in the DB using the current models.yaml.

    Useful after changing the summarizer config (e.g. flipping enable_thinking) to backfill
    existing rows. Preserves notion_page_id; pair with `notion-sync --recreate` to push the
    new bodies up to Notion.
    """
    import yaml as _yaml

    from tech_radar.agents.summarizer import summarize_articles
    from tech_radar.agents.tagger import tag_articles
    from tech_radar.pipeline import _resolve_stage
    from tech_radar.schemas import FilteredArticle
    from tech_radar.storage import Store

    load_dotenv()
    _setup_logging(verbose)

    profile_data = _yaml.safe_load(profile.read_text(encoding="utf-8"))
    models_cfg = _yaml.safe_load(models.read_text(encoding="utf-8"))
    summarizer_spec, summarizer_kwargs = _resolve_stage(models_cfg, "summarizer")
    tagger_spec, tagger_kwargs = _resolve_stage(models_cfg, "tagger")

    console.print(
        f"[cyan]Summarizer:[/cyan] {summarizer_spec.provider}/{summarizer_spec.model} kwargs={summarizer_kwargs}"
    )
    console.print(
        f"[cyan]Tagger:[/cyan]     {tagger_spec.provider}/{tagger_spec.model} kwargs={tagger_kwargs}"
    )

    with Store(db) as store:
        rows = store.list_all_with_notion_status()
        # Downcast TaggedArticle -> FilteredArticle: summarize_article does
        # `SummarizedArticle(**article.model_dump(), summary=summary)`, which collides
        # if `summary` is already present on the input. Pydantic ignores extra fields
        # on validate, so this strips summary/tags cleanly.
        filtered = [FilteredArticle.model_validate(a.model_dump()) for a, _ in rows]
        console.print(f"Re-summarizing [yellow]{len(filtered)}[/yellow] articles...")

        summarized = summarize_articles(filtered, profile_data, summarizer_spec, **summarizer_kwargs)
        console.print(f"  summarized: {len(summarized)}")
        tagged = tag_articles(summarized, profile_data, tagger_spec, **tagger_kwargs)
        console.print(f"  tagged:     {len(tagged)}")

        store.save_tagged(tagged)
        console.print(f"[green]Saved[/green] {len(tagged)} articles back to DB (notion_page_id preserved)")


if __name__ == "__main__":
    app()
