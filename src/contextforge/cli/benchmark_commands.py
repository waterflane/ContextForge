"""Thin CLI adapter for application-layer discovery benchmarks."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer
from pydantic import ValidationError

from contextforge.benchmarks import (
    BenchmarkManifest,
    BenchmarkMode,
    BenchmarkTask,
    load_benchmark_manifest,
    run_discovery_benchmark,
)
from contextforge.benchmarks.renderers import (
    render_benchmark_json,
    render_benchmark_markdown,
    render_benchmark_text,
)
from contextforge.cli.progress import CLIProgressRenderer, ProgressMode
from contextforge.cli.scan_output import OutputWriteError, write_output_atomic
from contextforge.models import ModelProvider, ModelProviderError
from contextforge.project_config import (
    ProjectConfigError,
    create_model_provider,
    load_project_configuration,
    resolve_provider_configuration,
)

BENCHMARK_REGRESSION_EXIT_CODE = 3


class BenchmarkFormat(StrEnum):
    """Supported benchmark result representations."""

    text = "text"
    markdown = "markdown"
    json = "json"


benchmark_app = typer.Typer(
    help=(
        "Run repository benchmarks. Exit code 3 means execution completed with "
        "task, expectation, or budget failures."
    ),
    no_args_is_help=True,
)


@benchmark_app.command("discovery")
def benchmark_discovery(
    path: Annotated[
        Path,
        typer.Argument(help="Root containing the manifest task repositories."),
    ],
    tasks: Annotated[
        Path,
        typer.Option(
            "--tasks",
            help="Versioned discovery benchmark task manifest.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    modes: Annotated[
        str | None,
        typer.Option(
            "--modes",
            help="Comma-separated subset of manifest modes: fresh,indexed,hybrid.",
        ),
    ] = None,
    repeat: Annotated[
        int | None,
        typer.Option(
            "--repeat",
            min=1,
            help="Override task and per-mode manifest repeat counts.",
        ),
    ] = None,
    output_format: Annotated[
        BenchmarkFormat,
        typer.Option(
            "--format",
            help="Result representation.",
            case_sensitive=False,
        ),
    ] = BenchmarkFormat.text,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write only the result to a new file."),
    ] = None,
) -> None:
    """Run discovery tasks without mutating repository or index state.

    Exit code 3 means a result was produced with task, expectation, or budget
    failures; the selected output still contains the complete benchmark result.
    """

    provider: ModelProvider | None = None
    progress = CLIProgressRenderer(ProgressMode.AUTO)
    try:
        selected_modes = _parse_modes(modes)
        manifest = _effective_manifest(
            load_benchmark_manifest(tasks),
            modes=selected_modes,
            repeat=repeat,
        )
        project = load_project_configuration(path)
        provider_configuration = resolve_provider_configuration(project)
        if provider_configuration is None:
            raise ValueError("discovery benchmark requires a model provider")
        provider = create_model_provider(provider_configuration)
        result = asyncio.run(
            run_discovery_benchmark(
                manifest,
                path,
                provider,
                progress=progress,
            )
        )
        representation = {
            BenchmarkFormat.text: render_benchmark_text,
            BenchmarkFormat.markdown: render_benchmark_markdown,
            BenchmarkFormat.json: render_benchmark_json,
        }[output_format](result)
    except (
        FileNotFoundError,
        NotADirectoryError,
        ProjectConfigError,
        ValidationError,
        ValueError,
    ) as exc:
        _exit_with_error(str(exc), code=2)
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except (ModelProviderError, OSError) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        progress.close()
        if provider is not None:
            with suppress(ModelProviderError):
                asyncio.run(provider.close())

    if output is None:
        typer.echo(representation, nl=False)
    else:
        try:
            write_output_atomic(output, representation)
        except OutputWriteError as exc:
            _exit_with_error(str(exc), code=1)
    if not result.passed:
        raise typer.Exit(code=BENCHMARK_REGRESSION_EXIT_CODE)


def _parse_modes(value: str | None) -> tuple[BenchmarkMode, ...] | None:
    if value is None:
        return None
    raw = tuple(item.strip().casefold() for item in value.split(","))
    if not raw or any(not item for item in raw):
        raise ValueError("--modes must be a non-empty comma-separated mode list")
    try:
        selected = tuple(BenchmarkMode(item) for item in raw)
    except ValueError as exc:
        raise ValueError(
            "--modes accepts only fresh,indexed,hybrid"
        ) from exc
    order = {mode: index for index, mode in enumerate(BenchmarkMode)}
    if len(selected) != len(set(selected)):
        raise ValueError("--modes must not contain duplicates")
    return tuple(sorted(selected, key=order.__getitem__))


def _effective_manifest(
    manifest: BenchmarkManifest,
    *,
    modes: tuple[BenchmarkMode, ...] | None,
    repeat: int | None,
) -> BenchmarkManifest:
    selected = None if modes is None else set(modes)
    tasks: list[BenchmarkTask] = []
    for task in manifest.tasks:
        enabled = tuple(
            mode for mode in task.modes if selected is None or mode in selected
        )
        if not enabled:
            continue
        payload = task.model_dump(mode="python")
        payload["modes"] = enabled
        overrides: dict[str, object] = {}
        for mode in BenchmarkMode:
            original = getattr(task.mode_overrides, mode.value)
            if mode not in enabled:
                overrides[mode.value] = None
                continue
            values = (
                {}
                if original is None
                else original.model_dump(mode="python", exclude_unset=True)
            )
            if repeat is not None:
                values["repeat_count"] = repeat
            overrides[mode.value] = values or None
        payload["mode_overrides"] = overrides
        if repeat is not None:
            payload["repeat_count"] = repeat
        tasks.append(BenchmarkTask.model_validate(payload))
    if not tasks:
        raise ValueError("selected modes do not enable any manifest task")
    return BenchmarkManifest(
        schema_version=manifest.schema_version,
        suite_name=manifest.suite_name,
        tasks=tuple(tasks),
    )


def _exit_with_error(message: str, *, code: int) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


__all__ = ["BENCHMARK_REGRESSION_EXIT_CODE", "benchmark_app"]
