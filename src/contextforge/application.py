"""Provider-independent application workflows shared by CLI and MCP adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryCancelledError,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoveryRunRecord,
    FinalContextSelection,
    discover_repository,
)
from contextforge.filesystem import FileTooLargeError, StableReadError, read_file_stably
from contextforge.git import GitDiffRequest
from contextforge.handoff import (
    CompiledPrompt,
    ContextSelectionReview,
    DiscoveryHandoffResult,
    HandoffBudgetLimits,
    TaskHandoff,
    compile_prompt,
    discover_context_handoff,
)
from contextforge.intelligence import (
    FALLBACK_ANALYZER,
    GENERIC_SEMANTIC_ANALYZER_ID,
    GENERIC_SEMANTIC_ANALYZER_VERSION,
    GLOBAL_MAP_PROMPT_VERSION,
    INDEX_SCHEMA_VERSION,
    PYTHON_ANALYZER,
    SEMANTIC_ANALYZER_ID,
    SEMANTIC_ANALYZER_VERSION,
    SEMANTIC_PROMPT_VERSION,
    ArchitectureMap,
    GlobalMapAnalysisOptions,
    GlobalMapBuildResult,
    IndexManifest,
    IndexManifestNotFoundError,
    IndexManifestReadError,
    ModelIdentity,
    SemanticAnalysisOptions,
    SemanticIndexBuildResult,
    StructuralIndexBuildResult,
    acquire_index_lock,
    build_repository_maps,
    build_semantic_index,
    build_structural_index,
    calculate_source_snapshot_digest,
    clean_generated_index,
    initialize_index,
    load_architecture_map,
    load_feature_map,
    load_manifest,
    load_repository_overview,
    write_manifest,
)
from contextforge.intelligence.models import AnalyzerIdentity
from contextforge.models import (
    ModelProvider,
    ProviderCancelledError,
    ProviderConfiguration,
)
from contextforge.progress import (
    ProgressActivity,
    ProgressEvent,
    ProgressObserver,
    ProgressReporter,
)
from contextforge.repositories import ProjectSnapshot, scan_repository

MAX_HANDOFF_BYTES = 16 * 1024 * 1024


class ApplicationError(RuntimeError):
    """Base expected workflow failure."""


class MissingIndexError(ApplicationError):
    """Raised when an operation explicitly requires an active index."""


class ArtifactReadError(ApplicationError):
    """Raised when a portable handoff cannot be read or validated."""


@dataclass(frozen=True, slots=True)
class IndexBuildReport:
    """Complete index workflow result with explicit phase accounting."""

    snapshot: ProjectSnapshot
    structural: StructuralIndexBuildResult
    semantic: SemanticIndexBuildResult | None
    maps: GlobalMapBuildResult | None
    provider_id: str | None
    model_id: str | None

    @property
    def manifest(self) -> IndexManifest:
        if self.maps is not None:
            return self.maps.manifest
        if self.semantic is not None:
            return self.semantic.manifest
        return self.structural.manifest

    @property
    def partial(self) -> bool:
        semantic_failed = bool(self.semantic and self.semantic.failed_paths)
        maps_failed = bool(
            self.maps
            and any(
                item.status in {"failed", "recovered"} for item in self.maps.outcomes
            )
        )
        return semantic_failed or maps_failed


@dataclass(frozen=True, slots=True)
class IndexStatusReport:
    """Secret-free read-only repository/index comparison."""

    initialized: bool
    index_schema: int
    repository_identity: str
    active_generation_id: str | None
    indexed_files: int
    stale_files: tuple[str, ...]
    failed_files: tuple[str, ...]
    deleted_records: tuple[str, ...]
    added_files: tuple[str, ...]
    changed_files: tuple[str, ...]
    provider_id: str | None
    model_id: str | None
    prompt_versions: tuple[str, ...]
    overview_status: Literal["current", "missing", "stale"]
    architecture_status: Literal["current", "missing", "stale"]
    feature_status: Literal["current", "missing", "stale"]
    lock_status: Literal["unlocked", "locked", "uninitialized"]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "initialized": self.initialized,
            "index_schema": self.index_schema,
            "repository_identity": self.repository_identity,
            "active_generation_id": self.active_generation_id,
            "indexed_files": self.indexed_files,
            "stale_files": list(self.stale_files),
            "failed_files": list(self.failed_files),
            "deleted_records": list(self.deleted_records),
            "added_files": list(self.added_files),
            "changed_files": list(self.changed_files),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "prompt_versions": list(self.prompt_versions),
            "global_maps": {
                "overview": self.overview_status,
                "architecture": self.architecture_status,
                "features": self.feature_status,
            },
            "lock_status": self.lock_status,
        }


async def build_repository_index(
    repository_root: str | Path,
    *,
    provider: ModelProvider | None,
    provider_configuration: ProviderConfiguration | None,
    update_only: bool = False,
    concurrency: int = 2,
    fail_on_error: bool = False,
    force_reanalyze: bool = False,
    max_files: int | None = None,
    semantic_max_output_tokens: int = 512,
    recover_stale_lock: bool = False,
    confirm_unknown_lock: bool = False,
    progress: ProgressObserver | None = None,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
) -> IndexBuildReport:
    """Build/update all index phases while retaining a prior pointer on failure."""

    reporter = _progress_reporter(
        "repository.index.update" if update_only else "repository.index.build",
        progress,
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
        metadata={
            "provider_id": (
                provider_configuration.provider_id
                if provider_configuration is not None
                else None
            ),
            "model_id": (
                provider_configuration.model_id
                if provider_configuration is not None
                else None
            ),
        },
    )
    reporter.report(
        "initialize",
        "Initializing repository index.",
        percentage=0,
        phase_label="Initialization",
        phase_percent=0,
        phase_weight=2,
        activity=ProgressActivity.ACTIVE,
    )
    try:
        report = await _build_repository_index(
            repository_root,
            provider=provider,
            provider_configuration=provider_configuration,
            update_only=update_only,
            concurrency=concurrency,
            fail_on_error=fail_on_error,
            force_reanalyze=force_reanalyze,
            max_files=max_files,
            semantic_max_output_tokens=semantic_max_output_tokens,
            recover_stale_lock=recover_stale_lock,
            confirm_unknown_lock=confirm_unknown_lock,
            progress=reporter,
        )
    except BaseException as exc:
        _report_terminal_exception(reporter, exc)
        raise
    reporter.complete(
        message="Repository index build completed.",
        metadata={"partial": report.partial},
    )
    return report


async def _build_repository_index(
    repository_root: str | Path,
    *,
    provider: ModelProvider | None,
    provider_configuration: ProviderConfiguration | None,
    update_only: bool,
    concurrency: int,
    fail_on_error: bool,
    force_reanalyze: bool,
    max_files: int | None,
    semantic_max_output_tokens: int,
    recover_stale_lock: bool,
    confirm_unknown_lock: bool,
    progress: ProgressReporter,
) -> IndexBuildReport:
    """Implement index construction under the public progress boundary."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    initialize_index(root)
    previous: IndexManifest | None
    try:
        previous = load_manifest(root)
    except IndexManifestNotFoundError:
        if update_only:
            raise MissingIndexError(
                "no active repository index exists; run 'contextforge index build'"
            ) from None
        previous = None
    model_enabled = provider is not None
    scan_end = 5.0 if model_enabled else 10.0
    compare_end = 8.0 if model_enabled else 15.0
    structural_end = 18.0 if model_enabled else 75.0
    progress.report(
        "scan",
        "Scanning repository files.",
        percentage=1,
        phase_label="Repository scan",
        phase_percent=0,
        phase_weight=scan_end,
        activity=ProgressActivity.ACTIVE,
    )
    snapshot = scan_repository(root)
    progress.report(
        "scan",
        "Repository scan completed.",
        percentage=scan_end,
        phase_label="Repository scan",
        phase_percent=100,
        phase_weight=scan_end,
        completed_units=len(snapshot.files),
        total_units=len(snapshot.files),
        unit_type="files",
        metadata={"files": len(snapshot.files)},
    )
    progress.report(
        "compare",
        "Compared the repository with the previous index generation.",
        percentage=compare_end,
        phase_label="Incremental planning",
        phase_percent=100,
        phase_weight=compare_end - scan_end,
        metadata={
            "previous_generation": previous.generation_id if previous else None,
            "update": update_only,
        },
    )
    run_id = "cli-index-update" if update_only else "cli-index-build"
    semantic: SemanticIndexBuildResult | None = None
    maps: GlobalMapBuildResult | None = None
    with acquire_index_lock(
        root,
        run_id,
        recover_stale=recover_stale_lock,
        confirm_unknown=confirm_unknown_lock,
    ) as lock:
        try:
            progress.report(
                "structural_index",
                "Building structural repository index.",
                percentage=compare_end,
                completed=0,
                total=len(snapshot.files),
                phase_label="Structural extraction",
                phase_percent=0,
                phase_weight=structural_end - compare_end,
                completed_units=0,
                total_units=len(snapshot.files),
                unit_type="files",
                activity=ProgressActivity.ACTIVE,
            )
            structural = build_structural_index(
                snapshot,
                lock,
                previous_manifest=previous,
            )
            progress.report(
                "structural_index",
                "Structural extraction and relationship construction completed.",
                percentage=structural_end,
                completed=len(snapshot.files),
                total=len(snapshot.files),
                phase_label="Structural extraction",
                phase_percent=100,
                phase_weight=structural_end - compare_end,
                completed_units=len(snapshot.files),
                total_units=len(snapshot.files),
                unit_type="files",
                reused_units=len(structural.reused_paths),
                metadata={
                    "extracted": len(structural.extracted_paths),
                    "reused": len(structural.reused_paths),
                },
            )
            if provider is not None:
                semantic_start = 18.0
                semantic_end = 82.0
                semantic_running_end = semantic_start
                model_semantic_observer = progress.scaled_observer(semantic_start, 81.0)
                metadata_semantic_observer = progress.scaled_observer(
                    semantic_start, 22.0
                )
                reused_semantic_observer = progress.scaled_observer(
                    semantic_start, semantic_start
                )

                def observe_semantic(event: ProgressEvent) -> None:
                    nonlocal semantic_running_end
                    route_totals = event.metadata.get("route_totals")
                    model_units = 0
                    metadata_units = 0
                    if isinstance(route_totals, dict):
                        model_units = sum(
                            value
                            for key, value in route_totals.items()
                            if key in {"rich_model_analysis", "generic_model_analysis"}
                            and isinstance(value, int)
                        )
                        metadata_value = route_totals.get(
                            "deterministic_metadata_summary", 0
                        )
                        if isinstance(metadata_value, int):
                            metadata_units = metadata_value
                    if model_units:
                        semantic_running_end = 81.0
                        model_semantic_observer(event)
                    elif metadata_units:
                        semantic_running_end = 22.0
                        metadata_semantic_observer(event)
                    else:
                        reused_semantic_observer(event)

                progress.report(
                    "semantic_index",
                    "Planning semantic repository analysis.",
                    percentage=semantic_start,
                    completed=0,
                    total=0,
                    phase_label="Semantic analysis",
                    phase_percent=0,
                    phase_weight=semantic_end - semantic_start,
                    completed_units=0,
                    total_units=0,
                    unit_type="files",
                    activity=ProgressActivity.ACTIVE,
                )
                semantic = await build_semantic_index(
                    snapshot,
                    lock,
                    provider,
                    options=SemanticAnalysisOptions(
                        max_concurrency=concurrency,
                        max_files=max_files,
                        max_output_tokens=semantic_max_output_tokens,
                        fail_on_error=fail_on_error,
                        force_reanalyze=force_reanalyze,
                        resume=not force_reanalyze,
                        progress=observe_semantic,
                    ),
                    previous_manifest=previous,
                )
                semantic_event = progress.last_event
                if semantic_event is not None:
                    semantic_publish_end = (
                        semantic_end
                        if semantic_running_end == 81.0
                        else 23.0
                        if semantic_running_end == 22.0
                        else semantic_start
                    )
                    progress.report(
                        "semantic_index",
                        "Semantic analysis was validated and published.",
                        percentage=semantic_publish_end,
                        completed=semantic_event.processed_units,
                        total=semantic_event.planned_units,
                        phase_label="Semantic analysis",
                        phase_percent=100,
                        phase_weight=semantic_publish_end - semantic_start,
                        completed_units=semantic_event.processed_units,
                        total_units=semantic_event.planned_units,
                        unit_type="items",
                        last_completed_item=semantic_event.last_completed_item,
                        last_failed_item=semantic_event.last_failed_item,
                        reused_units=semantic_event.reused_units,
                        skipped_units=semantic_event.skipped_units,
                        failed_units=semantic_event.failed_units,
                        planned_units=semantic_event.planned_units,
                        processed_units=semantic_event.processed_units,
                        succeeded_units=semantic_event.succeeded_units,
                        fallback_units=semantic_event.fallback_units,
                        lifecycle_state="published",
                        safe_error_code=semantic_event.safe_error_code,
                        safe_error_message=semantic_event.safe_error_message,
                        analyzer_kind=semantic_event.analyzer_kind,
                        estimated_input_tokens=semantic_event.estimated_input_tokens,
                        output_token_budget=semantic_event.output_token_budget,
                        input_truncated=semantic_event.input_truncated,
                        metadata={
                            "analyzed": len(semantic.analyzed_paths),
                            "reused": len(semantic.reused_paths),
                            "failed": len(semantic.failed_paths),
                        },
                    )
                maps_start = (
                    semantic_end
                    if semantic_running_end == 81.0
                    else 23.0
                    if semantic_running_end == 22.0
                    else semantic_start
                )
                progress.report(
                    "repository_maps",
                    "Building repository maps.",
                    percentage=maps_start,
                    phase_label="Semantic repository maps",
                    phase_percent=0,
                    phase_weight=94 - maps_start,
                    activity=ProgressActivity.WAITING,
                )
                maps = await build_repository_maps(
                    snapshot,
                    lock,
                    provider,
                    options=GlobalMapAnalysisOptions(fail_on_error=fail_on_error),
                )
                progress.report(
                    "repository_maps",
                    "Repository maps were validated and published.",
                    percentage=94,
                    phase_label="Semantic repository maps",
                    phase_percent=100,
                    phase_weight=94 - maps_start,
                    metadata={
                        "outcomes": {
                            item.map_kind: item.status for item in maps.outcomes
                        }
                    },
                )
            else:
                progress.report(
                    "model_analysis",
                    "Model-backed analysis was not requested.",
                    percentage=structural_end,
                    phase_label="Model analysis",
                    phase_percent=100,
                    phase_weight=0,
                    lifecycle_state="skipped",
                    safe_error_code="provider_disabled",
                    safe_error_message="model provider is disabled",
                    metadata={"skipped": True},
                )

            progress.report(
                "finalization",
                "Deterministic relationships and index records finalized.",
                percentage=97 if model_enabled else 90,
                phase_label="Deterministic finalization",
                phase_percent=100,
                phase_weight=3 if model_enabled else 15,
            )
            progress.report(
                "validation",
                "Validating the active index generation.",
                percentage=97 if model_enabled else 90,
                phase_label="Validation and publication",
                phase_percent=0,
                phase_weight=3 if model_enabled else 10,
                activity=ProgressActivity.ACTIVE,
            )
            active = load_manifest(root)
            expected = (
                maps.manifest
                if maps is not None
                else semantic.manifest
                if semantic is not None
                else structural.manifest
            )
            if active != expected:
                raise ApplicationError(
                    "published index generation does not match the completed build"
                )
            progress.report(
                "publication",
                "Atomic index publication completed.",
                percentage=99,
                phase_label="Validation and publication",
                phase_percent=(2 / 3 * 100 if model_enabled else 9 / 10 * 100),
                phase_weight=3 if model_enabled else 10,
                metadata={"generation_id": active.generation_id},
            )
        except BaseException:
            if previous is not None:
                with suppress(Exception):
                    write_manifest(lock, previous)
            raise
    return IndexBuildReport(
        snapshot=snapshot,
        structural=structural,
        semantic=semantic,
        maps=maps,
        provider_id=(
            provider_configuration.provider_id
            if provider_configuration is not None
            else None
        ),
        model_id=(
            provider_configuration.model_id
            if provider_configuration is not None
            else None
        ),
    )


def inspect_repository_index(
    repository_root: str | Path,
    *,
    provider_configuration: ProviderConfiguration | None,
    progress: ProgressObserver | None = None,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
) -> IndexStatusReport:
    """Compare current source with one pinned active generation without mutation."""

    reporter = _progress_reporter(
        "repository.index.inspect",
        progress,
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
    )
    reporter.report("scan", "Scanning repository and index state.", percentage=0)
    try:
        report = _inspect_repository_index(
            repository_root,
            provider_configuration=provider_configuration,
            progress=reporter,
        )
    except BaseException as exc:
        _report_terminal_exception(reporter, exc)
        raise
    reporter.complete(
        message="Repository index inspection completed.",
        metadata={"initialized": report.initialized},
    )
    return report


def _inspect_repository_index(
    repository_root: str | Path,
    *,
    provider_configuration: ProviderConfiguration | None,
    progress: ProgressReporter,
) -> IndexStatusReport:
    """Implement synchronous index inspection with coarse phase reporting."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    snapshot = scan_repository(root)
    progress.report(
        "scan",
        "Repository scan completed.",
        percentage=50,
        metadata={"files": len(snapshot.files)},
    )
    repository_identity = calculate_source_snapshot_digest(snapshot)
    index_root = root / ".contextforge" / "index"
    initialized = index_root.is_dir()
    lock_path = index_root / "lock.json"
    lock_status: Literal["unlocked", "locked", "uninitialized"] = (
        "locked"
        if lock_path.exists()
        else "unlocked"
        if initialized
        else "uninitialized"
    )
    try:
        manifest = load_manifest(root)
    except IndexManifestNotFoundError:
        progress.report(
            "compare",
            "No active repository index was found.",
            percentage=90,
        )
        return IndexStatusReport(
            initialized=initialized,
            index_schema=INDEX_SCHEMA_VERSION,
            repository_identity=repository_identity,
            active_generation_id=None,
            indexed_files=0,
            stale_files=tuple(item.path for item in snapshot.files),
            failed_files=(),
            deleted_records=(),
            added_files=tuple(item.path for item in snapshot.files),
            changed_files=(),
            provider_id=(
                provider_configuration.provider_id
                if provider_configuration is not None
                else None
            ),
            model_id=(
                provider_configuration.model_id
                if provider_configuration is not None
                else None
            ),
            prompt_versions=(SEMANTIC_PROMPT_VERSION, GLOBAL_MAP_PROMPT_VERSION),
            overview_status="missing",
            architecture_status="missing",
            feature_status="missing",
            lock_status=lock_status,
        )

    progress.report("compare", "Comparing repository and index state.", percentage=70)
    current = {item.path: item for item in snapshot.files}
    indexed = {item.path: item for item in manifest.files}
    added = tuple(sorted(set(current) - set(indexed)))
    deleted = tuple(sorted(set(indexed) - set(current)))
    changed = tuple(
        path
        for path in sorted(set(current) & set(indexed))
        if (
            current[path].sha256 != indexed[path].source_sha256
            or current[path].size_bytes != indexed[path].source_size_bytes
            or current[path].language != indexed[path].language
        )
    )
    stale = set(added) | set(changed)
    for path in sorted(set(current) & set(indexed)):
        state = indexed[path]
        structural_expected = (
            PYTHON_ANALYZER if current[path].language == "Python" else FALLBACK_ANALYZER
        )
        if state.analyzer != structural_expected:
            stale.add(path)
        semantic_expected = _semantic_identity(
            provider_configuration, generic=current[path].language != "Python"
        )
        if semantic_expected is not None and (
            state.semantic_status != "complete"
            or semantic_expected not in manifest.semantic_analyzers
        ):
            stale.add(path)
    failed = tuple(
        state.path
        for state in manifest.files
        if state.record_status == "failed" or state.semantic_status == "failed"
    )
    overview_status, architecture_status, feature_status = _global_statuses(
        root, manifest, repository_identity
    )
    provider_id, model_id = _manifest_model_identity(manifest, provider_configuration)
    prompt_versions = tuple(
        sorted(
            {
                *(
                    item.analysis_prompt_version
                    for item in manifest.structural_analyzers
                ),
                *(item.analysis_prompt_version for item in manifest.semantic_analyzers),
                GLOBAL_MAP_PROMPT_VERSION,
            }
        )
    )
    progress.report(
        "compare", "Repository and index comparison completed.", percentage=90
    )
    return IndexStatusReport(
        initialized=initialized,
        index_schema=manifest.schema_versions.index_schema_version,
        repository_identity=repository_identity,
        active_generation_id=manifest.generation_id,
        indexed_files=len(manifest.files),
        stale_files=tuple(sorted(stale)),
        failed_files=tuple(sorted(failed)),
        deleted_records=deleted,
        added_files=added,
        changed_files=changed,
        provider_id=provider_id,
        model_id=model_id,
        prompt_versions=prompt_versions,
        overview_status=overview_status,
        architecture_status=architecture_status,
        feature_status=feature_status,
        lock_status=lock_status,
    )


def clean_repository_index(
    repository_root: str | Path,
    *,
    recover_stale_lock: bool = False,
    confirm_unknown_lock: bool = False,
) -> None:
    """Delete only policy-approved generated index truth."""

    clean_generated_index(
        repository_root,
        recover_stale_lock=recover_stale_lock,
        confirm_unknown_lock=confirm_unknown_lock,
    )


async def suggest_repository_context(
    snapshot: ProjectSnapshot | str | Path,
    provider: ModelProvider,
    request: DiscoveryRequest,
    *,
    progress: ProgressObserver | None = None,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
) -> DiscoveryRunRecord:
    """Run existing bounded discovery without source or index mutation."""

    reporter = _progress_reporter(
        "repository.context.suggest",
        progress,
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
    )
    reporter.report("scan", "Preparing repository snapshot.", percentage=0)
    try:
        active_snapshot = _workflow_snapshot(snapshot, reporter, end_percentage=10)
        discovery_mode = (
            request.mode.value if isinstance(request, DiscoveryRequest) else "hybrid"
        )
        result = await discover_repository(
            active_snapshot,
            provider,
            request,
            progress=reporter.scaled_observer(
                10, 95, phase_prefix=f"{discovery_mode}_discovery"
            ),
            parent_operation_id=(
                reporter.last_event.operation_id
                if reporter.last_event is not None
                else None
            ),
        )
    except BaseException as exc:
        _report_terminal_exception(reporter, exc)
        raise
    reporter.complete(
        message="Repository context discovery completed.",
        metadata={"run_id": result.run_id},
    )
    return result


async def create_automatic_handoff(
    snapshot: ProjectSnapshot | str | Path,
    provider: ModelProvider,
    request: DiscoveryRequest,
    *,
    refine_task: bool = False,
    git_diff_request: GitDiffRequest | None = None,
    progress: ProgressObserver | None = None,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
) -> tuple[DiscoveryHandoffResult, CompiledPrompt]:
    """Discover, review, verify, package, and compile one automatic handoff."""

    reporter = _progress_reporter(
        "repository.handoff.create",
        progress,
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
    )
    reporter.report(
        "repository_knowledge",
        "Loading repository knowledge.",
        percentage=0,
    )

    try:
        active_snapshot = _workflow_snapshot(snapshot, reporter, end_percentage=10)
        result = await _create_automatic_handoff(
            active_snapshot,
            provider,
            request,
            refine_task=refine_task,
            git_diff_request=git_diff_request,
            progress=reporter,
        )
    except BaseException as exc:
        _report_terminal_exception(reporter, exc)
        raise
    reporter.complete(message="Automatic context handoff completed.")
    return result


async def _create_automatic_handoff(
    snapshot: ProjectSnapshot,
    provider: ModelProvider,
    request: DiscoveryRequest,
    *,
    refine_task: bool,
    git_diff_request: GitDiffRequest | None,
    progress: ProgressReporter,
) -> tuple[DiscoveryHandoffResult, CompiledPrompt]:
    """Implement handoff construction under the public progress boundary."""

    architecture: ArchitectureMap | None = None
    features = None
    with suppress(IndexManifestNotFoundError, IndexManifestReadError):
        manifest = load_manifest(snapshot.root)
        architecture = load_architecture_map(snapshot.root, manifest=manifest)
        features = load_feature_map(snapshot.root, manifest=manifest)
    progress.report(
        "discovery", "Discovering and reviewing repository context.", percentage=15
    )
    result = await discover_context_handoff(
        snapshot,
        provider,
        request,
        refinement_provider=provider if refine_task else None,
        git_diff_request=git_diff_request,
        architecture=architecture,
        features=features,
        budget_limits=HandoffBudgetLimits(
            max_source_bytes=request.budget.max_context_bytes,
            max_files=request.budget.max_context_files,
        ),
        progress=progress.scaled_observer(15, 88, phase_prefix="handoff"),
        parent_operation_id=(
            progress.last_event.operation_id
            if progress.last_event is not None
            else None
        ),
    )
    progress.report("compile", "Compiling the portable context handoff.", percentage=90)
    compiled = compile_prompt(
        result.handoff,
        progress=progress.scaled_observer(90, 99, phase_prefix="compile"),
        parent_operation_id=(
            progress.last_event.operation_id
            if progress.last_event is not None
            else None
        ),
    )
    return result, compiled


def canonical_json(value: object) -> str:
    """Render deterministic human-reviewable JSON with one final LF."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def render_context_suggestion(
    selection: FinalContextSelection,
    *,
    explain: bool = False,
) -> str:
    """Render a stable table-like discovery review."""

    lines = [
        "ContextForge context suggestion",
        f"Discovery mode: {selection.mode.value}",
        f"Estimated size: {selection.budget_usage.context_bytes} bytes",
        f"Selected files: {selection.budget_usage.context_files}",
        f"Confidence: {selection.confidence:.3f}",
        "Selections:",
    ]
    for item in selection.selected:
        path = item.path or f"[{item.kind}]"
        ranges = (
            "all lines"
            if not item.ranges
            else ", ".join(
                f"{value.start_line}-{value.end_line}" for value in item.ranges
            )
        )
        confidence = "unknown" if item.confidence is None else f"{item.confidence:.3f}"
        lines.extend(
            (
                f"  {path} | {ranges} | confidence {confidence}",
                f"    reason: {item.reason.summary}",
            )
        )
        if explain:
            lines.append(f"    source: {item.reason.discovery_source}")
            lines.extend(f"    evidence: {value}" for value in item.reason.evidence)
    lines.append("Warnings:")
    if selection.completeness_warnings:
        lines.extend(
            f"  {item.code}: {item.message}" for item in selection.completeness_warnings
        )
    else:
        lines.append("  (none; this is not a proof of completeness)")
    if selection.unknowns:
        lines.append("Unknowns:")
        lines.extend(f"  {item}" for item in selection.unknowns)
    return "\n".join(lines) + "\n"


def load_task_handoff(path: Path) -> TaskHandoff:
    """Strictly load a bounded portable handoff without repository access."""

    try:
        raw = read_file_stably(path.expanduser(), max_size_bytes=MAX_HANDOFF_BYTES)
    except FileNotFoundError:
        raise
    except (FileTooLargeError, StableReadError, OSError) as exc:
        raise ArtifactReadError("unable to read context handoff") from exc
    try:
        text = raw.content.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("handoff root must be an object")
        normalized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return TaskHandoff.model_validate_json(normalized)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise ArtifactReadError("context handoff is malformed or invalid") from exc


def render_handoff_review(handoff: TaskHandoff) -> str:
    """Render portable review metadata without reading the original repository."""

    review: ContextSelectionReview = handoff.review
    refinement = review.refined_task
    lines = [
        f"Package schema: {handoff.context_package.schema_version}",
        f"Handoff schema: {handoff.schema_version}",
        f"Original task: {handoff.original_task}",
        "Refined task: "
        + (
            "(none)"
            if refinement is None or refinement.refined_task is None
            else refinement.refined_task
        ),
        f"Discovery mode: {review.discovery.mode.value}",
        f"Discovery confidence: {review.discovery.confidence:.3f}",
        f"Index generation: {review.discovery.index_generation_id or '(none)'}",
        "Model provenance: "
        + (
            "(none)"
            if refinement is None
            else (
                f"{refinement.provider}/{refinement.model} "
                f"({refinement.prompt_version})"
            )
        ),
        "Selected items:",
    ]
    for item in review.selected_items:
        ranges = (
            "all lines"
            if not item.ranges
            else ", ".join(
                f"{value.start_line}-{value.end_line}" for value in item.ranges
            )
        )
        lines.extend(
            (
                f"  {item.path} | {item.representation} | {ranges}",
                f"    reason: {item.reason.summary}",
                f"    pinned: {str(item.pinned).lower()}",
                "    confidence: "
                + ("unknown" if item.confidence is None else f"{item.confidence:.3f}"),
            )
        )
    usage = handoff.budget_usage
    limits = review.budget_limits
    lines.extend(
        (
            "Budget:",
            f"  source: {usage.source_content_bytes}/{limits.max_source_bytes} bytes",
            f"  CodeMaps: {usage.codemap_bytes}/{limits.max_codemap_bytes} bytes",
            "  architecture: "
            f"{usage.architecture_note_bytes}/{limits.max_architecture_bytes} bytes",
            f"  Git diff: {usage.git_diff_bytes}/{limits.max_git_diff_bytes} bytes",
            "Git context: "
            + (
                "not requested"
                if handoff.git_diff is None
                else (
                    f"{handoff.git_diff.mode}; "
                    f"available={handoff.git_diff.available}; "
                    f"truncated={handoff.git_diff.truncated}"
                )
            ),
            "Warnings:",
        )
    )
    if handoff.completeness_warnings:
        lines.extend(
            f"  {item.code}: {item.message}" for item in handoff.completeness_warnings
        )
    else:
        lines.append("  (none; this is not a proof of completeness)")
    return "\n".join(lines) + "\n"


def build_discovery_request(
    *,
    task: str,
    mode: str,
    includes: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
    max_files: int = 100,
    max_context_bytes: int = 1_000_000,
) -> DiscoveryRequest:
    """Translate public CLI limits into the closed discovery request contract."""

    return DiscoveryRequest(
        task=task,
        mode=DiscoveryMode(mode),
        pinned_paths=tuple(sorted(set(includes))),
        excluded_paths=tuple(sorted(set(excludes))),
        budget=DiscoveryBudget(
            max_context_files=max_files,
            max_files_read=min(1_000, max_files * 2),
            max_source_bytes=min(16 * 1024 * 1024, max_context_bytes * 2),
            max_context_bytes=max_context_bytes,
        ),
    )


def _semantic_identity(
    configuration: ProviderConfiguration | None,
    *,
    generic: bool = False,
) -> AnalyzerIdentity | None:
    if configuration is None:
        return None
    return AnalyzerIdentity(
        analyzer_id=(GENERIC_SEMANTIC_ANALYZER_ID if generic else SEMANTIC_ANALYZER_ID),
        analyzer_version=_model_dependent_analyzer_version(
            (
                GENERIC_SEMANTIC_ANALYZER_VERSION
                if generic
                else SEMANTIC_ANALYZER_VERSION
            ),
            configuration,
        ),
        analysis_prompt_version=SEMANTIC_PROMPT_VERSION,
        response_schema_version=1,
        model_identity=ModelIdentity(
            provider_id=configuration.provider_id,
            model_id=configuration.model_id,
        ),
    )


def _model_dependent_analyzer_version(
    analyzer_version: str, configuration: ProviderConfiguration
) -> str:
    if configuration.provider_id != "openai-compatible":
        return analyzer_version
    canonical = configuration.endpoint.rstrip("/")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{analyzer_version}+base.{digest}"


def _manifest_model_identity(
    manifest: IndexManifest,
    configuration: ProviderConfiguration | None,
) -> tuple[str | None, str | None]:
    for analyzer in manifest.semantic_analyzers:
        if analyzer.model_identity is not None:
            return (
                analyzer.model_identity.provider_id,
                analyzer.model_identity.model_id,
            )
    if configuration is None:
        return None, None
    return configuration.provider_id, configuration.model_id


def _global_statuses(
    root: Path,
    manifest: IndexManifest,
    repository_identity: str,
) -> tuple[
    Literal["current", "missing", "stale"],
    Literal["current", "missing", "stale"],
    Literal["current", "missing", "stale"],
]:
    def status(loader: Any) -> Literal["current", "missing", "stale"]:
        try:
            value = loader(root, manifest=manifest)
        except IndexManifestReadError:
            return "missing"
        return (
            "current"
            if value.source_snapshot_digest == repository_identity
            else "stale"
        )

    return (
        status(load_repository_overview),
        status(load_architecture_map),
        status(load_feature_map),
    )


def _progress_reporter(
    operation_type: str,
    observer: ProgressObserver | None,
    *,
    operation_id: str | None,
    parent_operation_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> ProgressReporter:
    """Create one application operation reporter with a unique default ID."""

    return ProgressReporter(
        operation_id or f"{operation_type}:{uuid.uuid4()}",
        operation_type,
        observer=observer,
        parent_operation_id=parent_operation_id,
        metadata=metadata,
    )


def _workflow_snapshot(
    source: object,
    reporter: ProgressReporter,
    *,
    end_percentage: float,
) -> ProjectSnapshot:
    """Resolve a workflow snapshot while preserving existing snapshot callers."""

    if isinstance(source, ProjectSnapshot):
        reporter.report(
            "scan",
            "Using the caller-provided repository snapshot.",
            percentage=end_percentage,
            metadata={"files": len(source.files), "reused": True},
        )
        return source
    if isinstance(source, (str, Path)):
        reporter.report(
            "scan",
            "Scanning repository files.",
            percentage=min(2, end_percentage),
        )
        snapshot = scan_repository(source)
        reporter.report(
            "scan",
            "Repository scan completed.",
            percentage=end_percentage,
            metadata={"files": len(snapshot.files), "reused": False},
        )
        return snapshot
    # Retain compatibility with duck-typed test and adapter callers; downstream
    # validation still owns rejection of an invalid snapshot.
    return cast(ProjectSnapshot, source)


def _report_terminal_exception(
    reporter: ProgressReporter, error: BaseException
) -> None:
    """Classify cancellation without allowing progress to replace the failure."""

    metadata = {"error_type": type(error).__name__}
    if isinstance(
        error,
        (
            asyncio.CancelledError,
            DiscoveryCancelledError,
            KeyboardInterrupt,
            ProviderCancelledError,
        ),
    ):
        reporter.cancel(metadata=metadata)
    else:
        reporter.fail(metadata=metadata)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


__all__ = [
    "ApplicationError",
    "ArtifactReadError",
    "IndexBuildReport",
    "IndexStatusReport",
    "MAX_HANDOFF_BYTES",
    "MissingIndexError",
    "build_discovery_request",
    "build_repository_index",
    "canonical_json",
    "clean_repository_index",
    "create_automatic_handoff",
    "inspect_repository_index",
    "load_task_handoff",
    "render_context_suggestion",
    "render_handoff_review",
    "suggest_repository_context",
]
