"""Thin CLI adapters for context package creation and inspection."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Never, cast

import typer
from pydantic import ValidationError

from contextforge.application import (
    ArtifactReadError,
    build_discovery_request,
    canonical_json,
    create_automatic_handoff,
    load_task_handoff,
    render_handoff_review,
    suggest_repository_context,
)
from contextforge.cli.progress import CLIProgressRenderer, ProgressMode
from contextforge.cli.scan_output import OutputWriteError, write_output_atomic
from contextforge.context import (
    MAX_JSON_PACKAGE_BYTES,
    ContextBudget,
    ContextBuildError,
    ContextBuildLimitError,
    ContextBuildOptions,
    ContextCapsule,
    ContextCompilerError,
    ContextInspectionError,
    ContextReaderError,
    ContextRenderError,
    ContextSelection,
    ProjectTreeError,
    SelectionError,
    SelectorNoMatchError,
    build_context_package,
    compile_context_capsule,
    inspect_context_package_json,
    parse_line_range_request,
    render_context_inspection,
    render_context_package_json,
    render_context_package_markdown,
)
from contextforge.discovery import (
    DiscoveryError,
    DiscoveryResultFormat,
    render_context_suggestion,
)
from contextforge.filesystem import FileTooLargeError, StableReadError, read_file_stably
from contextforge.git import GitDiffRequest, collect_git_diff
from contextforge.handoff import ContextMaterializationError, PromptCompileError
from contextforge.intelligence import (
    INDEX_SCHEMA_VERSION,
    IndexManifestReadError,
    IndexStorageError,
    RetrievalResult,
    SourceRange,
    load_manifest,
    retrieve_context_candidates,
)
from contextforge.models import ModelProvider, ModelProviderError
from contextforge.project_config import (
    ProjectConfigError,
    configuration_resolution,
    create_model_provider,
    load_project_configuration,
    resolve_provider_configuration,
)
from contextforge.repositories import (
    GeneratedArtifactRegistryError,
    register_generated_artifact,
    scan_repository,
)
from contextforge.repositories.generated import GeneratedArtifactKind
from contextforge.repositories.ignore import IgnoreRulesError


class ContextFormat(StrEnum):
    """Supported context-package output representations."""

    markdown = "markdown"
    json = "json"


class DiscoveryChoice(StrEnum):
    indexed = "indexed"
    fresh = "fresh"
    hybrid = "hybrid"


class SuggestFormat(StrEnum):
    text = "text"
    table = "table"
    markdown = "markdown"
    json = "json"


class GitDiffChoice(StrEnum):
    none = "none"
    working = "working"
    staged = "staged"
    base = "base"


context_app = typer.Typer(
    help="Create and inspect portable context packages.",
    no_args_is_help=True,
)


@context_app.command("suggest")
def suggest_context(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Argument(help="Repository root to investigate without modification."),
    ] = Path("."),
    task: Annotated[
        str,
        typer.Option("--task", help="Task used to select relevant context."),
    ] = "",
    discovery: Annotated[
        DiscoveryChoice,
        typer.Option("--discovery", help="Discovery strategy.", case_sensitive=False),
    ] = DiscoveryChoice.hybrid,
    provider_name: Annotated[
        str | None,
        typer.Option("--provider", help="Provider ID override."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model ID override.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Explicit project configuration TOML."),
    ] = None,
    json_repair_attempts: Annotated[
        int | None,
        typer.Option("--json-repair-attempts", min=0, max=5),
    ] = None,
    includes: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            "--working-file",
            help="Pin one exact snapshot path/Working Set file; repeatable.",
        ),
    ] = None,
    excludes: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Exclude one exact snapshot path; repeatable."),
    ] = None,
    max_files: Annotated[
        int,
        typer.Option("--max-files", min=1, max=1_000),
    ] = 100,
    max_context_bytes: Annotated[
        int,
        typer.Option("--max-context-bytes", min=1, max=10 * 1024 * 1024),
    ] = 1_000_000,
    output_format: Annotated[
        SuggestFormat,
        typer.Option("--format", help="Output representation.", case_sensitive=False),
    ] = SuggestFormat.text,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Include detailed selection provenance."),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Fail when model response repairs are exhausted; disable fallback.",
        ),
    ] = False,
    rerank: Annotated[
        bool,
        typer.Option(
            "--rerank/--no-rerank",
            help="Allow one closed-schema model rerank of supplied candidate IDs.",
        ),
    ] = False,
    legacy_discovery: Annotated[
        bool,
        typer.Option(
            "--legacy-discovery",
            help="Use the deprecated model-led ContextPackage v1 discovery flow.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write output atomically to a file."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Atomically replace an existing output file."),
    ] = False,
    progress: Annotated[
        ProgressMode,
        typer.Option(
            "--progress",
            help="Progress rendering: auto, always when safe, or never.",
            case_sensitive=False,
        ),
    ] = ProgressMode.AUTO,
) -> None:
    """Suggest reviewable task context without modifying repository source."""

    parameter_source = ctx.get_parameter_source("discovery")
    discovery_explicit = (
        parameter_source is not None and parameter_source.name != "DEFAULT"
    )
    legacy_fallback = not rerank and (provider_name is not None or config is not None)
    if not legacy_discovery and not discovery_explicit and not legacy_fallback:
        _suggest_retrieval_context(
            path,
            task=task,
            provider_name=provider_name,
            model=model,
            config=config,
            includes=tuple(includes or ()),
            excludes=tuple(excludes or ()),
            max_files=max_files,
            output_format=output_format,
            explain=explain,
            rerank=rerank,
            output=output,
            force=force,
        )
        return

    active_provider: ModelProvider | None = None
    progress_renderer = CLIProgressRenderer(progress)
    try:
        if not task.strip():
            raise ValueError("--task must be non-empty")
        project = load_project_configuration(path, config_path=config)
        configured_repairs = project.models.structured_response.max_repair_attempts
        repair_source = configuration_resolution(project)["sources"].get(
            "models.structured_response.max_repair_attempts", "built-in default"
        )
        effective_repairs = (
            min(json_repair_attempts, 5)
            if json_repair_attempts is not None
            else 1
            if repair_source == "built-in default"
            else min(configured_repairs, 5)
        )
        provider_configuration = resolve_provider_configuration(
            project,
            provider=provider_name,
            model=model,
            json_repair_attempts=effective_repairs,
        )
        if provider_configuration is None:
            raise ValueError("context suggestion requires a model provider")
        active_provider = create_model_provider(provider_configuration)
        request = build_discovery_request(
            task=task,
            mode=discovery.value,
            includes=tuple(includes or ()),
            excludes=tuple(excludes or ()),
            max_files=max_files,
            max_context_bytes=max_context_bytes,
            strict=strict,
        )
        run = asyncio.run(
            suggest_repository_context(
                path,
                active_provider,
                request,
                progress=progress_renderer,
            )
        )
        selection = run.final_selection
        if selection is None:
            raise RuntimeError("complete discovery returned no final selection")
        render_format = {
            SuggestFormat.text: DiscoveryResultFormat.text,
            SuggestFormat.table: DiscoveryResultFormat.text,
            SuggestFormat.markdown: DiscoveryResultFormat.markdown,
            SuggestFormat.json: DiscoveryResultFormat.json,
        }[output_format]
        representation = render_context_suggestion(
            selection,
            output_format=render_format,
            explain=explain,
        )
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
    except (
        IgnoreRulesError,
        IndexStorageError,
        DiscoveryError,
        ModelProviderError,
        OSError,
    ) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        progress_renderer.close()
        _close_provider(active_provider)

    _publish_or_echo(representation, output=output, force=force)


def _suggest_retrieval_context(
    path: Path,
    *,
    task: str,
    provider_name: str | None,
    model: str | None,
    config: Path | None,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
    max_files: int,
    output_format: SuggestFormat,
    explain: bool,
    rerank: bool,
    output: Path | None,
    force: bool,
) -> None:
    provider: ModelProvider | None = None
    try:
        if not task.strip():
            raise ValueError("--task must be non-empty")
        if (provider_name is not None or model is not None) and not rerank:
            raise ValueError("--provider and --model require --rerank")
        if rerank:
            project = load_project_configuration(path, config_path=config)
            provider_configuration = resolve_provider_configuration(
                project, provider=provider_name, model=model
            )
            if provider_configuration is None:
                raise ValueError("--rerank requires a model provider")
            provider = create_model_provider(provider_configuration)
        result = asyncio.run(
            retrieve_context_candidates(
                path,
                task,
                working_set=includes,
                limit=max_files,
                provider=provider,
                rerank=rerank,
            )
        )
        if excludes:
            excluded = set(excludes)
            result = result.model_copy(
                update={
                    "candidates": tuple(
                        item for item in result.candidates if item.path not in excluded
                    )
                }
            )
        representation = _render_retrieval_result(
            result, output_format=output_format, explain=explain
        )
    except (
        FileNotFoundError,
        NotADirectoryError,
        ProjectConfigError,
        ValidationError,
        ValueError,
    ) as exc:
        _exit_with_error(str(exc), code=2)
    except (IndexStorageError, ModelProviderError, OSError) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        _close_provider(provider)
    _publish_or_echo(representation, output=output, force=force)


def _render_retrieval_result(
    result: RetrievalResult, *, output_format: SuggestFormat, explain: bool
) -> str:
    if output_format is SuggestFormat.json:
        return canonical_json(result.model_dump(mode="json"))
    markdown = output_format is SuggestFormat.markdown
    lines = [
        "# ContextForge retrieval candidates"
        if markdown
        else "ContextForge retrieval candidates",
        f"Task: {result.task}",
        f"Generation: {result.generation_id}",
        f"Provider calls: {result.provider_calls}",
        "Candidates:",
    ]
    for index, candidate in enumerate(result.candidates, start=1):
        prefix = f"{index}." if markdown else f"  {index}."
        lines.append(
            f"{prefix} {candidate.path} | {candidate.exact_group} | "
            f"score={candidate.score:.6f} | {candidate.synopsis}"
        )
        if explain:
            if candidate.matched_symbols:
                lines.append("     symbols: " + ", ".join(candidate.matched_symbols))
            if candidate.matched_concepts:
                lines.append("     concepts: " + ", ".join(candidate.matched_concepts))
            if candidate.evidence_ranges:
                lines.append(
                    "     evidence: "
                    + ", ".join(
                        f"{item.source_range.start_line}-{item.source_range.end_line}"
                        for item in candidate.evidence_ranges
                    )
                )
            if candidate.graph_neighbors:
                lines.append(
                    "     graph: "
                    + ", ".join(item.path for item in candidate.graph_neighbors)
                )
    if result.diagnostics:
        lines.extend(("Diagnostics:", *(f"  {item}" for item in result.diagnostics)))
    return "\n".join(lines) + "\n"


def _has_v3_index(path: Path) -> bool:
    try:
        return load_manifest(path).schema_version == INDEX_SCHEMA_VERSION
    except (IndexStorageError, ValueError, OSError):
        return False


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
    discovery: Annotated[
        DiscoveryChoice | None,
        typer.Option(
            "--discovery",
            help="Enable automatic indexed, fresh, or hybrid discovery.",
            case_sensitive=False,
        ),
    ] = None,
    provider_name: Annotated[
        str | None,
        typer.Option("--provider", help="Provider ID for automatic discovery."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model ID for automatic discovery."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Explicit project configuration TOML."),
    ] = None,
    json_repair_attempts: Annotated[
        int | None,
        typer.Option("--json-repair-attempts", min=0, max=10),
    ] = None,
    refine_task_option: Annotated[
        bool,
        typer.Option("--refine-task", help="Add a labelled optional task refinement."),
    ] = False,
    git_diff: Annotated[
        GitDiffChoice,
        typer.Option("--git-diff", help="Include bounded read-only Git context."),
    ] = GitDiffChoice.none,
    base: Annotated[
        str | None,
        typer.Option("--base", help="Safe Git revision for --git-diff base."),
    ] = None,
    prompt_output: Annotated[
        Path | None,
        typer.Option(
            "--prompt-output", help="Also write the compiled prompt atomically."
        ),
    ] = None,
    progress: Annotated[
        ProgressMode,
        typer.Option(
            "--progress",
            help="Progress rendering for automatic discovery.",
            case_sensitive=False,
        ),
    ] = ProgressMode.AUTO,
    working_files: Annotated[
        list[str] | None,
        typer.Option(
            "--working-file",
            help="Place one exact indexed file in the Capsule v2 Working Set.",
        ),
    ] = None,
    working_lines: Annotated[
        list[str] | None,
        typer.Option(
            "--working-lines",
            help="Place PATH:START-END in the Capsule v2 Working Set; repeatable.",
        ),
    ] = None,
    full_files: Annotated[
        list[str] | None,
        typer.Option(
            "--full-file",
            help="Explicitly permit FULL representation for a Working Set file.",
        ),
    ] = None,
    context_tokens: Annotated[
        int | None,
        typer.Option(
            "--context-tokens",
            min=1,
            help="Model context-window size used by the Capsule v2 budget.",
        ),
    ] = None,
    history_tokens: Annotated[
        int,
        typer.Option("--history-tokens", min=0, help="Caller history token cost."),
    ] = 0,
    response_tokens: Annotated[
        int,
        typer.Option("--response-tokens", min=0, help="Reserved response tokens."),
    ] = 4_096,
    safety_margin_tokens: Annotated[
        int | None,
        typer.Option("--safety-margin-tokens", min=0),
    ] = None,
    rerank: Annotated[
        bool,
        typer.Option("--rerank/--no-rerank", help="Enable bounded candidate rerank."),
    ] = False,
    legacy_handoff: Annotated[
        bool,
        typer.Option(
            "--legacy-handoff",
            help="Use the deprecated TaskHandoff/compile_prompt flow.",
        ),
    ] = False,
) -> None:
    """Build a manual package or an automatic reviewable compiled handoff."""

    if task is not None and not task.strip():
        _exit_with_error("task description must not be empty", code=2)

    if discovery is not None or legacy_handoff:
        _create_automatic_context(
            path,
            task=task,
            exact_paths=exact_paths,
            directories=directories,
            globs=globs,
            exclusions=exclusions,
            line_ranges=line_ranges,
            discovery=discovery or DiscoveryChoice.hybrid,
            provider_name=provider_name,
            model=model,
            config=config,
            json_repair_attempts=json_repair_attempts,
            refine_task_option=refine_task_option,
            git_diff=git_diff,
            base=base,
            max_files=max_files,
            max_context_bytes=max_context_bytes,
            output_format=output_format,
            output=output,
            prompt_output=prompt_output,
            force=force,
            progress_mode=progress,
        )
        return

    manual_selectors = any((exact_paths, directories, globs, line_ranges))
    capsule_options = any(
        (
            working_files,
            working_lines,
            full_files,
            context_tokens is not None,
            history_tokens,
            response_tokens != 4_096,
            safety_margin_tokens is not None,
            rerank,
        )
    )
    if task is not None and (
        capsule_options or (not manual_selectors and _has_v3_index(path))
    ):
        if exclusions:
            _exit_with_error(
                "--exclude is supported only by manual/legacy flows", code=2
            )
        _create_capsule_context(
            path,
            task=task,
            working_files=tuple(working_files or ()),
            working_lines=tuple(working_lines or ()),
            full_files=tuple(full_files or ()),
            provider_name=provider_name,
            model=model,
            config=config,
            git_diff=git_diff,
            base=base,
            context_tokens=context_tokens,
            history_tokens=history_tokens,
            response_tokens=response_tokens,
            safety_margin_tokens=safety_margin_tokens,
            rerank=rerank,
            output_format=output_format,
            output=output,
            prompt_output=prompt_output,
            force=force,
        )
        return

    if any(
        value
        for value in (
            provider_name,
            model,
            config,
            json_repair_attempts,
            refine_task_option,
            git_diff is not GitDiffChoice.none,
            base,
            prompt_output,
            working_files,
            working_lines,
            full_files,
            context_tokens is not None,
            history_tokens,
            response_tokens != 4_096,
            safety_margin_tokens is not None,
            rerank,
            legacy_handoff,
        )
    ):
        _exit_with_error(
            "automatic provider, refinement, Git, and prompt options require "
            "--discovery",
            code=2,
        )

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
        register_generated_artifact(path, written_path, kind="package")
    except (GeneratedArtifactRegistryError, OutputWriteError) as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo(f"Output written to {written_path}")


@context_app.command("inspect")
def inspect_context(
    package: Annotated[
        Path,
        typer.Argument(
            help="JSON context package to validate and inspect.",
            metavar="PACKAGE",
        ),
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
        capsule = _parse_context_capsule(raw.content)
        if capsule is not None:
            representation = _render_capsule_review(capsule)
        else:
            _, inspection = inspect_context_package_json(raw.content)
            representation = render_context_inspection(inspection)
    except (ContextInspectionError, ValidationError, ValueError) as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo(representation, nl=False)


def _create_capsule_context(
    path: Path,
    *,
    task: str,
    working_files: tuple[str, ...],
    working_lines: tuple[str, ...],
    full_files: tuple[str, ...],
    provider_name: str | None,
    model: str | None,
    config: Path | None,
    git_diff: GitDiffChoice,
    base: str | None,
    context_tokens: int | None,
    history_tokens: int,
    response_tokens: int,
    safety_margin_tokens: int | None,
    rerank: bool,
    output_format: ContextFormat,
    output: Path | None,
    prompt_output: Path | None,
    force: bool,
) -> None:
    provider: ModelProvider | None = None
    try:
        if git_diff is GitDiffChoice.base and base is None:
            raise ValueError("--git-diff base requires --base")
        if git_diff is not GitDiffChoice.base and base is not None:
            raise ValueError("--base is accepted only with --git-diff base")
        if (provider_name is not None or model is not None) and not rerank:
            raise ValueError("--provider and --model require --rerank")
        project = load_project_configuration(path, config_path=config)
        provider_configuration = None
        if rerank:
            provider_configuration = resolve_provider_configuration(
                project, provider=provider_name, model=model
            )
            if provider_configuration is None:
                raise ValueError("--rerank requires a model provider")
            provider = create_model_provider(provider_configuration)

        parsed = tuple(parse_line_range_request(value) for value in working_lines)
        working = tuple(
            dict.fromkeys(
                (*working_files, *(item.path for item in parsed), *full_files)
            )
        )
        range_map: dict[str, list[SourceRange]] = {}
        for item in parsed:
            range_map.setdefault(item.path, []).append(
                SourceRange(
                    start_line=item.range.start,
                    start_column=0,
                    end_line=item.range.end,
                    end_column=0,
                )
            )
        canonical_ranges = {
            key: tuple(
                sorted(values, key=lambda item: (item.start_line, item.end_line))
            )
            for key, values in range_map.items()
        }
        snapshot = scan_repository(path)
        diff_context = (
            None
            if git_diff is GitDiffChoice.none
            else collect_git_diff(
                snapshot,
                GitDiffRequest(
                    mode=cast(Literal["working", "staged", "base"], git_diff.value),
                    base_ref=base,
                ),
            )
        )
        retrieval = asyncio.run(
            retrieve_context_candidates(
                path,
                task,
                working_set=working,
                diff_paths=() if diff_context is None else diff_context.touched_paths,
                provider=provider,
                rerank=rerank,
            )
        )
        budget = ContextBudget(
            context_window_tokens=(
                project.models.context_window
                if context_tokens is None
                else context_tokens
            ),
            history_tokens=history_tokens,
            response_tokens=response_tokens,
            safety_margin_tokens=(
                project.models.context_safety_margin
                if safety_margin_tokens is None
                else safety_margin_tokens
            ),
        )
        compiled = compile_context_capsule(
            path,
            task,
            retrieval,
            budget=budget,
            working_files=working,
            working_lines=canonical_ranges,
            pinned_full_files=full_files,
            git_diff=diff_context,
        )
        representation = (
            canonical_json(compiled.capsule.model_dump(mode="json"))
            if output_format is ContextFormat.json
            else compiled.prompt
        )
        if prompt_output is not None:
            written = write_output_atomic(prompt_output, compiled.prompt, force=force)
            register_generated_artifact(path, written, kind="prompt")
            typer.echo(f"Compiled prompt written to {written}", err=True)
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
    except (
        ContextCompilerError,
        IndexStorageError,
        ModelProviderError,
        IgnoreRulesError,
        OutputWriteError,
        OSError,
    ) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        _close_provider(provider)
    _publish_or_echo(
        representation,
        output=output,
        force=force,
        repository_root=path,
        artifact_kind=("capsule" if output_format is ContextFormat.json else "prompt"),
    )


@context_app.command("review")
def review_context(
    package: Annotated[
        Path,
        typer.Argument(help="Portable JSON task handoff to validate and review."),
    ],
) -> None:
    """Inspect a generated handoff without requiring its original repository."""

    try:
        raw = read_file_stably(
            package.expanduser(), max_size_bytes=MAX_JSON_PACKAGE_BYTES
        )
        capsule = _parse_context_capsule(raw.content)
        if capsule is not None:
            representation = _render_capsule_review(capsule)
        else:
            handoff = load_task_handoff(package)
            representation = render_handoff_review(handoff)
    except FileNotFoundError:
        _exit_with_error(f"context handoff does not exist: {package}", code=2)
    except (
        ArtifactReadError,
        FileTooLargeError,
        StableReadError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo(representation, nl=False)


def _parse_context_capsule(raw: bytes) -> ContextCapsule | None:
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 2 or "snapshot" not in payload:
        return None
    return ContextCapsule.model_validate(payload)


def _render_capsule_review(capsule: ContextCapsule) -> str:
    lines = [
        "ContextForge Context Capsule",
        f"Capsule schema: {capsule.schema_version}",
        f"Index schema: {capsule.snapshot.index_schema_version}",
        f"Generation: {capsule.snapshot.generation_id}",
        f"Generation kind: {capsule.snapshot.generation_kind}",
        f"Task: {capsule.task}",
        f"Estimator: {capsule.estimator_id}",
        f"Tokens: {capsule.token_count}",
        "Working Set:",
    ]
    lines.extend(
        f"  {item.path} | {item.representation.value} | {item.token_count} tokens"
        for item in capsule.working_set
    )
    if not capsule.working_set:
        lines.append("  (none)")
    lines.append("Task context:")
    lines.extend(
        f"  {item.path} | {item.representation.value} | {item.token_count} tokens"
        for item in capsule.task_context
    )
    if not capsule.task_context:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _create_automatic_context(
    path: Path,
    *,
    task: str | None,
    exact_paths: list[str] | None,
    directories: list[str] | None,
    globs: list[str] | None,
    exclusions: list[str] | None,
    line_ranges: list[str] | None,
    discovery: DiscoveryChoice,
    provider_name: str | None,
    model: str | None,
    config: Path | None,
    json_repair_attempts: int | None,
    refine_task_option: bool,
    git_diff: GitDiffChoice,
    base: str | None,
    max_files: int,
    max_context_bytes: int,
    output_format: ContextFormat,
    output: Path | None,
    prompt_output: Path | None,
    force: bool,
    progress_mode: ProgressMode,
) -> None:
    provider: ModelProvider | None = None
    progress_renderer = CLIProgressRenderer(progress_mode)
    try:
        if task is None or not task.strip():
            raise ValueError("automatic context creation requires --task")
        if directories or globs or line_ranges:
            raise ValueError(
                "automatic discovery accepts exact --include/--exclude paths; "
                "directory, glob, and line selectors remain manual-only"
            )
        if git_diff is GitDiffChoice.base and base is None:
            raise ValueError("--git-diff base requires --base")
        if git_diff is not GitDiffChoice.base and base is not None:
            raise ValueError("--base is accepted only with --git-diff base")
        project = load_project_configuration(path, config_path=config)
        provider_configuration = resolve_provider_configuration(
            project,
            provider=provider_name,
            model=model,
            json_repair_attempts=json_repair_attempts,
        )
        if provider_configuration is None:
            raise ValueError("automatic context creation requires a model provider")
        provider = create_model_provider(provider_configuration)
        request = build_discovery_request(
            task=task,
            mode=discovery.value,
            includes=tuple(exact_paths or ()),
            excludes=tuple(exclusions or ()),
            max_files=max_files,
            max_context_bytes=max_context_bytes,
        )
        git_request = (
            None
            if git_diff is GitDiffChoice.none
            else GitDiffRequest(
                mode=cast(Literal["working", "staged", "base"], git_diff.value),
                base_ref=base,
            )
        )
        result, compiled = asyncio.run(
            create_automatic_handoff(
                path,
                provider,
                request,
                refine_task=refine_task_option,
                git_diff_request=git_request,
                progress=progress_renderer,
            )
        )
        representation = (
            canonical_json(result.handoff.model_dump(mode="json"))
            if output_format is ContextFormat.json
            else compiled.prompt.body
        )
        if prompt_output is not None:
            written = write_output_atomic(
                prompt_output, compiled.prompt.body, force=force
            )
            register_generated_artifact(path, written, kind="prompt")
            typer.echo(f"Compiled prompt written to {written}", err=True)
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
    except (
        ContextMaterializationError,
        DiscoveryError,
        PromptCompileError,
        IndexManifestReadError,
        IndexStorageError,
        ModelProviderError,
        IgnoreRulesError,
        OutputWriteError,
        OSError,
    ) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        progress_renderer.close()
        _close_provider(provider)

    _publish_or_echo(
        representation,
        output=output,
        force=force,
        repository_root=path,
        artifact_kind=("package" if output_format is ContextFormat.json else "prompt"),
    )


def _publish_or_echo(
    representation: str,
    *,
    output: Path | None,
    force: bool,
    repository_root: Path | None = None,
    artifact_kind: GeneratedArtifactKind | None = None,
) -> None:
    if output is None:
        typer.echo(representation, nl=False)
        return
    try:
        written_path = write_output_atomic(output, representation, force=force)
        if repository_root is not None and artifact_kind is not None:
            register_generated_artifact(
                repository_root, written_path, kind=artifact_kind
            )
    except (GeneratedArtifactRegistryError, OutputWriteError) as exc:
        _exit_with_error(str(exc), code=1)
    typer.echo(f"Output written to {written_path}")


def _close_provider(provider: ModelProvider | None) -> None:
    if provider is None:
        return
    with suppress(ModelProviderError):
        asyncio.run(provider.close())


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
