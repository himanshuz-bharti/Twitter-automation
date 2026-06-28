from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
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
    topic: str = typer.Option("latest technology news", help="Topic to search for."),
    style: DraftStyle | None = typer.Option(None, help="Drafting style."),
    output_dir: Path = typer.Option(Path("outputs"), help="Directory for draft bundles."),
    post: bool = typer.Option(False, help="Post to X/Twitter. Dry-run is the default."),
) -> None:
    """Run the full news-to-tweet pipeline."""
    load_dotenv()
    settings = get_settings()
    selected_style = style or settings.default_style

    if post and not settings.can_post_to_x:
        raise typer.BadParameter("Posting requested, but X API credentials are incomplete.")

    pipeline = Pipeline(settings)
    result = pipeline.run(topic=topic, style=selected_style, output_dir=output_dir, post=post)

    console.print(
        Panel.fit(
            display_text(result.draft.text),
            title="Tweet Draft" if not result.posted else f"Posted Tweet {result.post_id}",
            border_style="cyan",
        )
    )
    console.print(f"[bold]Source:[/bold] {display_text(result.selected_article.title)}")
    console.print(f"[bold]URL:[/bold] {result.selected_article.url}")
    if result.draft.image_url:
        console.print(f"[bold]Image:[/bold] {result.draft.image_url}")
    if result.draft.image_path:
        console.print(f"[bold]Downloaded:[/bold] {result.draft.image_path}")


@app.command()
def sources(
    topic: str = typer.Option("latest technology news", help="Topic to search for."),
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

    table = Table(title=f"Top sources for: {topic}")
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
