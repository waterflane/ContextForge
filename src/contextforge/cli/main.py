"""Minimal ContextForge CLI."""

import typer

from contextforge._metadata import APP_NAME, __version__
from contextforge.config import get_settings
from contextforge.logging import configure_logging

app = typer.Typer(
    name="contextforge",
    help="Manage project context for AI models and agents.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Show the installed ContextForge version."""

    typer.echo(f"{APP_NAME} {__version__}")


@app.command()
def doctor() -> None:
    """Check that the package is installed and configured."""

    settings = get_settings()
    configure_logging(settings.log_level)
    typer.echo("ContextForge is installed.")
    typer.echo(f"Environment: {settings.environment}")
    typer.echo(f"Log level: {settings.log_level}")
