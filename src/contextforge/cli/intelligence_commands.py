"""Thin Typer adapters for repository-index lifecycle operations."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer

from contextforge.application import (
    ApplicationError,
    IndexBuildReport,
    IndexStatusReport,
    build_repository_index,
    canonical_json,
    clean_repository_index,
    inspect_repository_index,
)
from contextforge.intelligence import (
    GlobalMapAnalysisError,
    IndexStorageError,
    SemanticAnalysisError,
)
from contextforge.models import ModelProvider, ModelProviderError
from contextforge.project_config import (
    ProjectConfigError,
    create_model_provider,
    load_project_configuration,
    resolve_provider_configuration,
)
from contextforge.repositories.ignore import IgnoreRulesError


class IndexFormat(StrEnum):
    table = "table"
    json = "json"


index_app = typer.Typer(
    help="Build, update, inspect, and clean repository intelligence.",
    no_args_is_help=True,
)


def _index_operation(
    path: Path,
    *,
    update_only: bool,
    provider_name: str | None,
    model: str | None,
    config: Path | None,
    concurrency: int,
    fail_on_error: bool,
    force_reanalyze: bool,
    max_files: int | None,
    local_only: bool,
) -> None:
    provider: ModelProvider | None = None
    try:
        project = load_project_configuration(path, config_path=config)
        provider_configuration = resolve_provider_configuration(
            project,
            provider=provider_name,
            model=model,
            concurrency=concurrency,
            local_only=True if local_only else None,
        )
        if provider_configuration is not None:
            provider = create_model_provider(provider_configuration)
        report = asyncio.run(
            build_repository_index(
                path,
                provider=provider,
                provider_configuration=provider_configuration,
                update_only=update_only,
                concurrency=concurrency,
                fail_on_error=fail_on_error,
                force_reanalyze=force_reanalyze,
                max_files=max_files,
            )
        )
    except (FileNotFoundError, NotADirectoryError, ProjectConfigError) as exc:
        _exit_with_error(str(exc), code=2)
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except (
        ApplicationError,
        GlobalMapAnalysisError,
        IndexStorageError,
        IgnoreRulesError,
        ModelProviderError,
        OSError,
        SemanticAnalysisError,
    ) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        if provider is not None:
            with suppress(ModelProviderError):
                asyncio.run(provider.close())

    typer.echo(_render_build_summary(report), nl=False)


@index_app.command("build")
def build_index(
    path: Annotated[Path, typer.Argument(help="Repository root to index.")] = Path("."),
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Provider ID; use 'none' for structural-only."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model ID override.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Explicit project configuration TOML."),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency", min=1, max=8, help="Maximum semantic file concurrency."
        ),
    ] = 2,
    fail_on_error: Annotated[
        bool,
        typer.Option(
            "--fail-on-error",
            help="Keep the prior active generation on any model-analysis failure.",
        ),
    ] = False,
    force_reanalyze: Annotated[
        bool,
        typer.Option(
            "--force-reanalyze",
            help="Bypass valid semantic-analysis reuse for this run.",
        ),
    ] = False,
    max_files: Annotated[
        int | None,
        typer.Option(
            "--max-files", min=1, help="Maximum stale files analyzed semantically."
        ),
    ] = None,
    local_only: Annotated[
        bool,
        typer.Option(
            "--local-only", help="Require a loopback/local provider endpoint."
        ),
    ] = False,
) -> None:
    """Scan and publish deterministic CodeMaps, semantics, and repository maps."""

    _index_operation(
        path,
        update_only=False,
        provider_name=provider,
        model=model,
        config=config,
        concurrency=concurrency,
        fail_on_error=fail_on_error,
        force_reanalyze=force_reanalyze,
        max_files=max_files,
        local_only=local_only,
    )


@index_app.command("update")
def update_index(
    path: Annotated[
        Path, typer.Argument(help="Repository root with an active index.")
    ] = Path("."),
    provider: Annotated[
        str | None, typer.Option("--provider", help="Provider ID override.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model ID override.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Explicit project configuration TOML."),
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, max=8)] = 2,
    fail_on_error: Annotated[bool, typer.Option("--fail-on-error")] = False,
    force_reanalyze: Annotated[bool, typer.Option("--force-reanalyze")] = False,
    max_files: Annotated[int | None, typer.Option("--max-files", min=1)] = None,
    local_only: Annotated[bool, typer.Option("--local-only")] = False,
) -> None:
    """Increment only new, changed, deleted, or stale index records."""

    _index_operation(
        path,
        update_only=True,
        provider_name=provider,
        model=model,
        config=config,
        concurrency=concurrency,
        fail_on_error=fail_on_error,
        force_reanalyze=force_reanalyze,
        max_files=max_files,
        local_only=local_only,
    )


@index_app.command("status")
def index_status(
    path: Annotated[Path, typer.Argument(help="Repository root to inspect.")] = Path(
        "."
    ),
    output_format: Annotated[
        IndexFormat,
        typer.Option("--format", help="Output representation.", case_sensitive=False),
    ] = IndexFormat.table,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Explicit project configuration TOML."),
    ] = None,
) -> None:
    """Report current, stale, failed, deleted, provider, map, and lock state."""

    try:
        project = load_project_configuration(path, config_path=config)
        provider_configuration = resolve_provider_configuration(project)
        report = inspect_repository_index(
            path, provider_configuration=provider_configuration
        )
    except (FileNotFoundError, NotADirectoryError, ProjectConfigError) as exc:
        _exit_with_error(str(exc), code=2)
    except (IndexStorageError, IgnoreRulesError, OSError) as exc:
        _exit_with_error(str(exc), code=1)
    representation = (
        canonical_json(report.to_dict())
        if output_format is IndexFormat.json
        else _render_status(report)
    )
    typer.echo(representation, nl=False)


@index_app.command("clean")
def clean_index(
    path: Annotated[
        Path,
        typer.Argument(
            help="Repository root whose generated index data will be removed."
        ),
    ] = Path("."),
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Skip the interactive generated-data confirmation."
        ),
    ] = False,
) -> None:
    """Delete generated index truth while preserving ``config.toml``."""

    if not force:
        typer.confirm(
            "Delete generated .contextforge/index data (config.toml is preserved)?",
            abort=True,
        )
    try:
        clean_repository_index(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        _exit_with_error(str(exc), code=2)
    except (IndexStorageError, OSError) as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo("Generated repository index data removed; config.toml was preserved.")


def _render_build_summary(report: IndexBuildReport) -> str:
    semantic = report.semantic
    maps = report.maps
    analyzed_count = 0 if semantic is None else len(semantic.analyzed_paths)
    reused_count = 0 if semantic is None else len(semantic.reused_paths)
    failed_count = 0 if semantic is None else len(semantic.failed_paths)
    provider = report.provider_id or "disabled"
    model = report.model_id or "disabled"
    lines = [
        "ContextForge repository index",
        f"Generation: {report.manifest.generation_id}",
        f"Indexed files: {report.manifest.statistics.file_count}",
        f"CodeMaps extracted: {len(report.structural.extracted_paths)}",
        f"CodeMaps reused: {len(report.structural.reused_paths)}",
        f"Semantic analyses completed: {analyzed_count}",
        f"Semantic analyses reused: {reused_count}",
        f"Semantic analyses failed: {failed_count}",
        f"Provider/model: {provider}/{model}",
        "Global maps: "
        + (
            "disabled"
            if maps is None
            else ", ".join(f"{item.map_kind}={item.status}" for item in maps.outcomes)
        ),
        f"Status: {'partial' if report.partial else 'complete'}",
    ]
    return "\n".join(lines) + "\n"


def _render_status(report: IndexStatusReport) -> str:
    provider = report.provider_id or "(none)"
    model = report.model_id or "(none)"
    return (
        "ContextForge index status\n"
        f"Index schema: {report.index_schema}\n"
        f"Repository identity: {report.repository_identity}\n"
        f"Active generation: {report.active_generation_id or '(none)'}\n"
        f"Indexed files: {report.indexed_files}\n"
        f"Stale files: {len(report.stale_files)}\n"
        f"Failed files: {len(report.failed_files)}\n"
        f"Deleted records: {len(report.deleted_records)}\n"
        f"Added files: {len(report.added_files)}\n"
        f"Changed files: {len(report.changed_files)}\n"
        f"Provider/model: {provider}/{model}\n"
        f"Prompt versions: {', '.join(report.prompt_versions) or '(none)'}\n"
        f"Repository overview: {report.overview_status}\n"
        f"Architecture map: {report.architecture_status}\n"
        f"Feature map: {report.feature_status}\n"
        f"Lock status: {report.lock_status}\n"
    )


def _exit_with_error(message: str, *, code: int) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


__all__ = ["IndexFormat", "index_app"]
