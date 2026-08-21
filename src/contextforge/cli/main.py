"""ContextForge command-line interface."""

import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer

from contextforge._metadata import APP_NAME, __version__
from contextforge.cli.benchmark_commands import benchmark_app
from contextforge.cli.context_commands import context_app
from contextforge.cli.diagnostics_commands import diagnostics_app
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
from contextforge.logging import (
    LogFormat,
    LoggingConfiguration,
    LogLevel,
    configure_logging,
    emit,
)
from contextforge.project_config import (
    ProjectConfigError,
    configuration_resolution,
    load_project_configuration,
    resolve_logging_configuration,
)
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
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(index_app, name="index")
app.add_typer(mcp_app, name="mcp")
app.add_typer(diagnostics_app, name="diagnostics")


def _show_version(value: bool) -> None:
    """Print application metadata and stop eager option processing."""

    if value:
        typer.echo(f"{APP_NAME} {__version__}")
        raise typer.Exit


@app.callback()
def cli(
    ctx: typer.Context,
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the installed ContextForge version and exit.",
        ),
    ] = False,
    log_level: Annotated[
        LogLevel | None,
        typer.Option(
            "--log-level",
            help="Diagnostics: quiet, error, warning, info, debug, or trace.",
            case_sensitive=False,
        ),
    ] = None,
    log_format: Annotated[
        LogFormat | None,
        typer.Option(
            "--log-format",
            help="Diagnostic rendering: auto, pretty, or JSON lines.",
            case_sensitive=False,
        ),
    ] = None,
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Write rotating JSON diagnostics to PATH."),
    ] = None,
    log_component: Annotated[
        list[str] | None,
        typer.Option(
            "--log-component",
            help="Restrict diagnostics to COMPONENT; may be repeated.",
        ),
    ] = None,
    no_log_file: Annotated[
        bool,
        typer.Option("--no-log-file", help="Disable configured file logging."),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable color in logs and progress output."),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "-v",
            "--verbose",
            count=True,
            help="Increase diagnostics; repeat as -vv for trace.",
        ),
    ] = 0,
) -> None:
    """Build deterministic context packages from local repositories."""

    del version_requested
    if ctx.invoked_subcommand == "version":
        configure_logging(LoggingConfiguration(level=LogLevel.QUIET))
        return
    root = Path.cwd()
    try:
        project = load_project_configuration(root)
        environment_components = project._value_candidates.get(
            "logging.component_filter", {}
        ).get("environment")
        selected_components = tuple(log_component or ())
        if not selected_components and isinstance(environment_components, tuple):
            selected_components = environment_components
        configuration = resolve_logging_configuration(
            project,
            root,
            level=None if log_level is None else log_level.value,
            log_format=None if log_format is None else log_format.value,
            log_file=log_file,
            component_filter=selected_components,
            no_log_file=no_log_file or ctx.invoked_subcommand == "benchmark",
            no_color=no_color,
            verbosity=verbose,
        )
        configure_logging(configuration)
        resolution = configuration_resolution(project)
        emit(
            "configuration",
            "config.logging_resolved",
            "Resolved the effective diagnostic logging configuration.",
            level=LogLevel.DEBUG,
            data={
                "level": configuration.level.value,
                "format": configuration.format.value,
                "file_enabled": configuration.file_enabled,
                "file": str(configuration.file),
                "component_filter": sorted(configuration.component_filter),
                "sources": resolution["sources"],
                "explicit_log_level": log_level is not None,
                "verbosity": verbose,
            },
        )
    except (OSError, ProjectConfigError, ValueError):
        # The leaf command retains ownership of project-configuration errors.
        # Logging setup must never make an otherwise valid command unusable.
        configure_logging(LoggingConfiguration(level=LogLevel.WARNING))


def run() -> None:
    """Run the CLI without Click rewriting native Windows arguments."""

    _configure_utf8_stdio()
    normalized = _normalized_global_logging_arguments(sys.argv[1:])
    if normalized is None:
        app(windows_expand_args=False)
    else:
        app(args=normalized, windows_expand_args=False)


_GLOBAL_VALUE_OPTIONS = frozenset(
    {"--log-level", "--log-format", "--log-file", "--log-component"}
)
_GLOBAL_FLAG_OPTIONS = frozenset({"--no-log-file", "--no-color"})


def _normalized_global_logging_arguments(arguments: list[str]) -> list[str] | None:
    """Move documented global logging flags before a nested command.

    Click scopes parent options before subcommands. Console users naturally place
    diagnostics after leaf arguments, so the entrypoint performs a narrow,
    value-preserving normalization for only these global flags.
    """

    globals_: list[str] = []
    remaining: list[str] = []
    found = False
    index = 0
    while index < len(arguments):
        value = arguments[index]
        option, separator, attached = value.partition("=")
        if option in _GLOBAL_VALUE_OPTIONS:
            found = True
            globals_.append(value)
            if not separator and index + 1 < len(arguments):
                index += 1
                globals_.append(arguments[index])
        elif value in _GLOBAL_FLAG_OPTIONS or (
            value.startswith("-")
            and not value.startswith("--")
            and value[1:]
            and set(value[1:]) == {"v"}
        ):
            found = True
            globals_.append(value)
        else:
            remaining.append(value)
        index += 1
    return [*globals_, *remaining] if found else None


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
        typer.Argument(help="Repository root to scan.", metavar="PATH"),
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
        typer.Argument(help="Repository root to scan.", metavar="PATH"),
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
