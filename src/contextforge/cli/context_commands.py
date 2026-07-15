"""Thin CLI adapters for context package creation and inspection."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer
from pydantic import ValidationError

from contextforge.cli.scan_output import OutputWriteError, write_output_atomic
from contextforge.context import (
    MAX_JSON_PACKAGE_BYTES,
    ContextBuildError,
    ContextBuildLimitError,
    ContextBuildOptions,
    ContextInspectionError,
    ContextReaderError,
    ContextRenderError,
    ContextSelection,
    ProjectTreeError,
    SelectionError,
    SelectorNoMatchError,
    build_context_package,
    inspect_context_package_json,
    parse_line_range_request,
    render_context_inspection,
    render_context_package_json,
    render_context_package_markdown,
)
from contextforge.filesystem import FileTooLargeError, StableReadError, read_file_stably
from contextforge.repositories.ignore import IgnoreRulesError


class ContextFormat(StrEnum):
    """Supported context-package output representations."""

    markdown = "markdown"
    json = "json"


context_app = typer.Typer(
    help="Create and inspect portable context packages.",
    no_args_is_help=True,
)


@context_app.command("create")
def create_context(
    path: Annotated[
        Path,
        typer.Argument(help="Repository root to package."),
    ] = Path("."),
    task: Annotated[
        str | None,
        typer.Option("--task", help="Explicit task description for the package."),
    ] = None,
    exact_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            "--file",
            help=(
                "Include one exact relative file; use --directory or --glob for "
                "other selector kinds; repeatable."
            ),
        ),
    ] = None,
    directories: Annotated[
        list[str] | None,
        typer.Option(
            "--directory",
            help="Include one relative directory recursively; repeatable.",
        ),
    ] = None,
    globs: Annotated[
        list[str] | None,
        typer.Option(
            "--glob",
            help="Include one GitWildMatch file pattern; repeatable.",
        ),
    ] = None,
    exclusions: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Exclude one GitWildMatch file pattern; repeatable.",
        ),
    ] = None,
    line_ranges: Annotated[
        list[str] | None,
        typer.Option(
            "--include-lines",
            "--lines",
            help="Include PATH:START-END; repeatable.",
        ),
    ] = None,
    include_tree: Annotated[
        bool,
        typer.Option(
            "--include-tree/--no-include-tree",
            help="Include the portable project tree in the package.",
        ),
    ] = True,
    output_format: Annotated[
        ContextFormat,
        typer.Option(
            "--format",
            help="Package output representation.",
            case_sensitive=False,
        ),
    ] = ContextFormat.markdown,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write output atomically to a file."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Atomically replace an existing output file."),
    ] = False,
    max_files: Annotated[
        int,
        typer.Option(
            "--max-files",
            min=1,
            help="Maximum number of selected files.",
        ),
    ] = 100,
    max_context_bytes: Annotated[
        int,
        typer.Option(
            "--max-context-bytes",
            "--max-total-size",
            min=1,
            help="Maximum included canonical UTF-8 content bytes.",
        ),
    ] = 1_000_000,
) -> None:
    """Build a deterministic portable context package."""

    try:
        selection = ContextSelection(
            exact_paths=tuple(exact_paths or ()),
            directories=tuple(directories or ()),
            globs=tuple(globs or ()),
            exclusions=tuple(exclusions or ()),
            line_ranges=tuple(
                parse_line_range_request(value) for value in line_ranges or ()
            ),
        )
        option_values: dict[str, object] = {
            "selection": selection,
            "include_tree": include_tree,
            "max_files": max_files,
            "max_total_content_bytes": max_context_bytes,
        }
        if task is not None:
            option_values["task_description"] = task
        options = ContextBuildOptions.model_validate(option_values)
    except (ValidationError, SelectionError) as exc:
        _exit_with_error(str(exc), code=2)

    try:
        package = build_context_package(path, options)
    except (FileNotFoundError, NotADirectoryError) as exc:
        _exit_with_error(str(exc), code=2)
    except SelectorNoMatchError as exc:
        _exit_with_error(_selector_no_match_message(exc), code=2)
    except (SelectionError, ContextBuildLimitError) as exc:
        _exit_with_error(str(exc), code=2)
    except (
        ContextReaderError,
        ContextBuildError,
        ProjectTreeError,
        IgnoreRulesError,
        OSError,
    ) as exc:
        _exit_with_error(str(exc), code=1)

    try:
        representation = (
            render_context_package_json(package)
            if output_format is ContextFormat.json
            else render_context_package_markdown(package)
        )
    except ContextRenderError as exc:
        _exit_with_error(str(exc), code=1)

    if output is None:
        typer.echo(representation, nl=False)
        return

    try:
        written_path = write_output_atomic(output, representation, force=force)
    except OutputWriteError as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo(f"Output written to {written_path}")


@context_app.command("inspect")
def inspect_context(
    package: Annotated[
        Path,
        typer.Argument(help="JSON context package to validate and inspect."),
    ],
) -> None:
    """Validate and summarize a JSON package without its repository."""

    try:
        raw = read_file_stably(
            package.expanduser(), max_size_bytes=MAX_JSON_PACKAGE_BYTES
        )
    except FileNotFoundError:
        _exit_with_error(f"context package does not exist: {package}", code=2)
    except (FileTooLargeError, StableReadError, OSError) as exc:
        _exit_with_error(f"unable to read context package: {exc}", code=1)

    try:
        _, inspection = inspect_context_package_json(raw.content)
        representation = render_context_inspection(inspection)
    except ContextInspectionError as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo(representation, nl=False)


def _exit_with_error(message: str, *, code: int) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


def _selector_no_match_message(error: SelectorNoMatchError) -> str:
    if error.selector.kind == "exact_path":
        return (
            f"Exact file {error.selector.value!r} was not found in the repository "
            "snapshot.\nUse --directory for directories or --glob for patterns."
        )
    return str(error)
