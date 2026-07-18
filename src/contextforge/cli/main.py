"""ContextForge command-line interface."""

import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer

from contextforge._metadata import APP_NAME, __version__
from contextforge.cli.context_commands import context_app
from contextforge.cli.intelligence_commands import index_app
from contextforge.cli.mcp_commands import mcp_app
from contextforge.cli.scan_output import (
    OutputWriteError,
    ScanReport,
    render_scan_json,
    render_scan_table,
    write_output_atomic,
)
from contextforge.config import get_settings
from contextforge.context import (
    ProjectTreeError,
    build_project_tree,
    render_project_tree,
    render_project_tree_json,
    render_project_tree_markdown,
)
from contextforge.logging import configure_logging
from contextforge.repositories import ScanOptions, scan_repository
from contextforge.repositories.ignore import IgnoreRulesError

DEFAULT_MAX_FILE_SIZE = ScanOptions().max_file_size_bytes


class ScanFormat(StrEnum):
    """Supported repository scan output representations."""

    table = "table"
    json = "json"


class TreeFormat(StrEnum):
    """Supported project-tree output representations."""

    text = "text"
    markdown = "markdown"
    json = "json"


app = typer.Typer(
    name="contextforge",
    help="Build deterministic context packages from local repositories.",
    no_args_is_help=True,
)
app.add_typer(context_app, name="context")
app.add_typer(index_app, name="index")
app.add_typer(mcp_app, name="mcp")


def _show_version(value: bool) -> None:
    """Print application metadata and stop eager option processing."""

    if value:
        typer.echo(f"{APP_NAME} {__version__}")
        raise typer.Exit


@app.callback()
def cli(
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the installed ContextForge version and exit.",
        ),
    ] = False,
) -> None:
    """Build deterministic context packages from local repositories."""


def run() -> None:
    """Run the CLI without Click rewriting native Windows arguments."""

    _configure_utf8_stdio()
    app(windows_expand_args=False)


def _configure_utf8_stdio() -> None:
    """Keep machine-readable and Markdown output Unicode-safe on every console."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")


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


@app.command()
def tree(
    path: Annotated[
        Path,
        typer.Argument(help="Repository root to scan."),
    ] = Path("."),
    depth: Annotated[
        int | None,
        typer.Option(
            "--depth",
            min=0,
            help="Maximum edges below the implicit root; omit for unlimited.",
        ),
    ] = None,
    output_format: Annotated[
        TreeFormat,
        typer.Option(
            "--format",
            help="Output representation.",
            case_sensitive=False,
        ),
    ] = TreeFormat.text,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write output atomically to a file."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Atomically replace an existing output file."),
    ] = False,
) -> None:
    """Render a deterministic project tree from a repository snapshot."""

    try:
        snapshot = scan_repository(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        _exit_with_error(str(exc), code=2)
    except (IgnoreRulesError, OSError) as exc:
        _exit_with_error(str(exc), code=1)

    try:
        project_tree = build_project_tree(snapshot)
        if output_format is TreeFormat.json:
            representation = render_project_tree_json(project_tree, max_depth=depth)
        elif output_format is TreeFormat.markdown:
            representation = render_project_tree_markdown(project_tree, max_depth=depth)
        else:
            representation = render_project_tree(project_tree, max_depth=depth)
    except ProjectTreeError as exc:
        _exit_with_error(str(exc), code=1)

    if output is None:
        typer.echo(representation, nl=False)
    else:
        try:
            written_path = write_output_atomic(output, representation, force=force)
        except OutputWriteError as exc:
            _exit_with_error(str(exc), code=1)
        typer.echo(f"Output written to {written_path}")


def _exit_with_error(message: str, *, code: int) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)
