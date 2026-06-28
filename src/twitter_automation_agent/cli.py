from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from twitter_automation_agent.config import get_settings
from twitter_automation_agent.models import DraftStyle
from twitter_automation_agent.pipeline import Pipeline

app = typer.Typer(
    help="Create factual news-based X/Twitter drafts and deliver them for manual posting.",
    pretty_exceptions_show_locals=False,
)
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


def image_status(item: object) -> str:
    suggestions = getattr(item.draft, "image_suggestions", [])
    if suggestions:
        return f"{len(suggestions)} image(s)"
    return item.draft.image_path or "missing"


def add_result_rows(table: Table, drafts: list, fallback_status: str) -> None:
    for index, item in enumerate(drafts, start=1):
        table.add_row(
            str(index),
            display_text(item.article.source),
            display_text(item.draft.text),
            image_status(item),
            item.post_id or fallback_status,
        )


@app.command()
def run(
    topic: str | None = typer.Option(None, help="Specific news topic to search. Omit for trending tech."),
    style: DraftStyle | None = typer.Option(None, help="Drafting style."),
    count: int = typer.Option(20, min=1, max=50, help="Number of drafts to generate."),
    output_dir: Path = typer.Option(Path("outputs"), help="Directory for draft bundles."),
    include_seen: bool = typer.Option(False, help="Allow articles generated in previous runs."),
    post: bool = typer.Option(False, help="Post to X/Twitter. Requires paid X API credits."),
) -> None:
    """Run the full news-to-tweet pipeline and save drafts locally."""
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

    add_result_rows(table, result.drafts, "dry-run")
    console.print(table)
    console.print(f"[bold]Saved:[/bold] {output_dir}")


@app.command()
def sources(
    topic: str | None = typer.Option(None, help="Specific news topic to search. Omit for trending tech."),
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


@app.command()
def autopost(
    topic: str | None = typer.Option(None, help="Specific news topic to search. Omit for trending tech."),
    style: DraftStyle | None = typer.Option(None, help="Drafting style."),
    queue_size: int = typer.Option(20, min=1, max=50, help="Draft queue size to build first."),
    posts: int = typer.Option(20, min=1, max=50, help="Number of image-backed posts to publish."),
    interval_minutes: float = typer.Option(90.0, min=0.0, help="Minutes to wait between posts."),
    output_dir: Path = typer.Option(Path("outputs"), help="Directory for queue/history files."),
    include_seen: bool = typer.Option(False, help="Allow articles already posted through this command."),
    dry_run: bool = typer.Option(False, help="Build queue and simulate posting without using X."),
) -> None:
    """Post ranked news tweets to X. Requires paid X API credits."""
    load_dotenv()
    settings = get_settings()
    selected_style = style or settings.default_style

    if not dry_run and not settings.can_post_to_x:
        raise typer.BadParameter("X API credentials are incomplete.")

    result = Pipeline(settings).autopost(
        topic=topic,
        style=selected_style,
        output_dir=output_dir,
        queue_size=queue_size,
        posts=posts,
        interval_minutes=interval_minutes,
        skip_history=not include_seen,
        dry_run=dry_run,
    )

    table = Table(title=f"Autopost {'dry run' if dry_run else 'run'}: {len(result.drafts)} item(s)")
    table.add_column("#", justify="right")
    table.add_column("Source")
    table.add_column("Tweet")
    table.add_column("Image")
    table.add_column("Post ID")

    add_result_rows(table, result.drafts, "not posted")
    console.print(table)
    console.print(f"[bold]Saved:[/bold] {output_dir}")


@app.command("telegram")
def telegram_batch(
    topic: str | None = typer.Option(None, help="Specific news topic to search. Omit for trending tech."),
    style: DraftStyle | None = typer.Option(None, help="Drafting style."),
    count: int = typer.Option(10, min=1, max=50, help="Number of image-backed drafts to send immediately."),
    output_dir: Path = typer.Option(Path("outputs"), help="Directory for batch/history files."),
    include_seen: bool = typer.Option(False, help="Allow articles already sent through this command."),
    dry_run: bool = typer.Option(False, help="Build the batch without sending to Telegram."),
) -> None:
    """Send a batch of ranked tweet drafts plus images to Telegram immediately."""
    load_dotenv()
    settings = get_settings()
    selected_style = style or settings.default_style

    if not dry_run and not settings.can_send_to_telegram:
        raise typer.BadParameter("Telegram credentials are incomplete.")

    result = Pipeline(settings).send_telegram_batch(
        topic=topic,
        style=selected_style,
        output_dir=output_dir,
        count=count,
        skip_history=not include_seen,
        dry_run=dry_run,
    )

    table = Table(title=f"Telegram {'dry run' if dry_run else 'delivery'}: {len(result.drafts)} item(s)")
    table.add_column("#", justify="right")
    table.add_column("Source")
    table.add_column("Tweet")
    table.add_column("Image")
    table.add_column("Message ID")

    add_result_rows(table, result.drafts, "not sent")
    console.print(table)
    console.print(f"[bold]Saved:[/bold] {output_dir}")


@app.command("x-check")
def x_check() -> None:
    """Verify X OAuth credentials without posting."""
    load_dotenv()
    settings = get_settings()
    if not settings.can_post_to_x:
        raise typer.BadParameter("X API credentials are incomplete.")
    screen_name, user_id = Pipeline(settings).publisher.verify_credentials()
    console.print(f"[bold]X credentials OK[/bold]: @{screen_name} ({user_id})")


@app.command("telegram-check")
def telegram_check() -> None:
    """Verify Telegram bot token and chat id without sending a draft."""
    load_dotenv()
    settings = get_settings()
    if not settings.can_send_to_telegram:
        raise typer.BadParameter("Telegram credentials are incomplete.")
    bot_username, chat_id = Pipeline(settings).telegram.verify_credentials()
    console.print(f"[bold]Telegram credentials OK[/bold]: @{bot_username} -> {chat_id}")


if __name__ == "__main__":
    app()