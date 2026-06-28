from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from twitter_automation_agent.config import get_settings
from twitter_automation_agent.models import DraftStyle
from twitter_automation_agent.pipeline import Pipeline

app = typer.Typer(help="Create factual tech-news X/Twitter drafts and optionally post them.")
console = Console()


def display_text(value: object) -> str:
    """Return text that legacy Windows consoles can print reliably."""
    replacements = {
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
    text = str(value)
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("cp1252", errors="replace").decode("cp1252")


@app.command()
def run(
    topic: str | None = typer.Option(None, help="Optional topic bias. Omit for trending tech."),
    style: DraftStyle | None = typer.Option(None, help="Drafting style."),
    count: int = typer.Option(20, min=1, max=50, help="Number of drafts to generate."),
    output_dir: Path = typer.Option(Path("outputs"), help="Directory for draft bundles."),
    include_seen: bool = typer.Option(False, help="Allow articles generated in previous runs."),
    post: bool = typer.Option(False, help="Post to X/Twitter. Dry-run is the default."),
) -> None:
    """Run the full news-to-tweet pipeline."""
    load_dotenv()
    settings = get_settings()
    selected_style = style or settings.default_style

    if post and not settings.can_post_to_x:
        raise typer.BadParameter("Posting requested, but X API credentials are incomplete.")

    pipeline = Pipeline(settings)
    result = pipeline.run(
        topic=topic,
        style=selected_style,
        output_dir=output_dir,
        count=count,
        post=post,
        skip_history=not include_seen,
    )

    table = Table(title=f"{len(result.drafts)} draft(s) for: {result.topic}")
    table.add_column("#", justify="right")
    table.add_column("Source")
    table.add_column("Tweet")
    table.add_column("Image")
    table.add_column("Post")

    for index, item in enumerate(result.drafts, start=1):
        table.add_row(
            str(index),
            display_text(item.article.source),
            display_text(item.draft.text),
            "yes" if item.draft.image_path else "no",
            item.post_id or ("posted" if item.posted else "dry-run"),
        )

    console.print(table)
    console.print(f"[bold]Saved:[/bold] {output_dir}")


@app.command()
def sources(
    topic: str | None = typer.Option(None, help="Optional topic bias. Omit for trending tech."),
    limit: int = typer.Option(10, min=1, max=50, help="Number of sources to show."),
) -> None:
    """Preview ranked source articles without drafting."""
    load_dotenv()
    settings = get_settings()
    articles = Pipeline(settings).news.collect(
        topic=topic,
        lookback_hours=settings.news_lookback_hours,
        limit=settings.max_articles,
    )

    label = topic or "trending tech news"
    table = Table(title=f"Top sources for: {label}")
    table.add_column("Score", justify="right")
    table.add_column("Source")
    table.add_column("Title")
    table.add_column("Published")

    for article in articles[:limit]:
        table.add_row(
            f"{article.score:.1f}",
            display_text(article.source),
            display_text(article.title),
            article.published_at.isoformat() if article.published_at else "unknown",
        )

    console.print(table)


if __name__ == "__main__":
    app()
