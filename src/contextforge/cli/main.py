"""ContextForge command-line interface."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer

from contextforge._metadata import APP_NAME, __version__
from contextforge.cli.scan_output import (
    OutputWriteError,
    ScanReport,
    render_scan_json,
    render_scan_table,
    write_output_atomic,
)
from contextforge.config import get_settings
from contextforge.logging import configure_logging
from contextforge.repositories import ScanOptions, scan_repository
from contextforge.repositories.ignore import IgnoreRulesError

DEFAULT_MAX_FILE_SIZE = ScanOptions().max_file_size_bytes


class ScanFormat(StrEnum):
    """Supported repository scan output representations."""

    table = "table"
    json = "json"


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


@app.command()
def scan(
    path: Annotated[
        Path,
        typer.Argument(help="Repository root to scan."),
    ] = Path("."),
    output_format: Annotated[
        ScanFormat,
        typer.Option(
            "--format",
            help="Output representation.",
            case_sensitive=False,
        ),
    ] = ScanFormat.table,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write output to a new file."),
    ] = None,
    max_file_size: Annotated[
        int,
        typer.Option(
            "--max-file-size",
            min=1,
            help="Maximum included file size in bytes.",
        ),
    ] = DEFAULT_MAX_FILE_SIZE,
    show_excluded: Annotated[
        bool,
        typer.Option(
            "--show-excluded",
            help="Include excluded paths and reasons in table or JSON output.",
        ),
    ] = False,
    fail_on_error: Annotated[
        bool,
        typer.Option(
            "--fail-on-error",
            help="Exit with code 3 when entries are unreadable.",
        ),
    ] = False,
) -> None:
    """Scan a repository and report its deterministic file inventory."""

    options = ScanOptions(max_file_size_bytes=max_file_size)
    try:
        snapshot = scan_repository(path, options)
    except (FileNotFoundError, NotADirectoryError) as exc:
        _exit_with_error(str(exc), code=2)
    except (IgnoreRulesError, OSError) as exc:
        _exit_with_error(str(exc), code=1)

    report = ScanReport(options=options, snapshot=snapshot)
    if output_format is ScanFormat.json:
        representation = render_scan_json(report, show_excluded=show_excluded)
    else:
        representation = render_scan_table(
            snapshot,
            show_excluded=show_excluded,
        )

    if output is None:
        typer.echo(representation, nl=False)
    else:
        try:
            written_path = write_output_atomic(output, representation)
        except OutputWriteError as exc:
            _exit_with_error(str(exc), code=1)
        typer.echo(f"Output written to {written_path}")

    if fail_on_error and snapshot.summary.failed_count > 0:
        raise typer.Exit(code=3)


def _exit_with_error(message: str, *, code: int) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)
