"""Public model-free repository discovery application workflows."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from contextforge.context import (
    ContextBuildOptions,
    ContextPackage,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    ReaderLimits,
    build_context_package,
    read_selected_text_files,
)
from contextforge.discovery.models import (
    CompletenessWarning,
    DiscoveryBudgetUsage,
    DiscoveryCandidatePreparation,
    DiscoveryExpansionRequest,
    DiscoveryExpansionResult,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoverySelection,
    PreparedDiscoveryCandidate,
    VerifiedContext,
    VerifiedContextBlock,
    VerifiedContextFile,
)
from contextforge.discovery.session import DiscoverySession
from contextforge.discovery.tools import DiscoveryToolExecutor, ToolBudgetTracker
from contextforge.intelligence import (
    calculate_source_snapshot_digest,
    canonical_json_bytes,
)
from contextforge.repositories import ProjectSnapshot, scan_repository

DiscoverySource = ProjectSnapshot | str | Path

DISCOVERY_EXPANSION_TOOLS = frozenset(
    {
        "get_repository_overview",
        "list_tree",
        "search_index",
        "search_symbols",
        "search_text",
        "get_file_summary",
        "get_symbol_summary",
        "find_imports",
        "find_importers",
        "find_references",
        "find_callers",
        "find_related_tests",
        "read_file",
        "read_lines",
        "get_context_budget",
    }
)


class DiscoveryApplicationError(RuntimeError):
    """Base failure at the immutable model-free application boundary."""


class DiscoveryPreparationMismatchError(DiscoveryApplicationError):
    """Raised when a DTO is not pinned to the supplied repository snapshot."""


class DiscoverySelectionError(DiscoveryApplicationError):
    """Raised when a caller selection is outside a prepared candidate set."""


def prepare_discovery_candidates(
    source: DiscoverySource,
    request: DiscoveryRequest,
    *,
    cancellation: asyncio.Event | None = None,
) -> DiscoveryCandidatePreparation:
    """Return deterministic mode-aware candidates without invoking a model."""

    _raise_if_cancelled(cancellation)
    snapshot = _snapshot(source)
    session, _ = _prepare_runtime(snapshot, request)
    knowledge = session._require_knowledge()
    files = {item.path: item for item in snapshot.files}
    manifest_paths = (
        set()
        if knowledge.manifest is None
        else {item.path for item in knowledge.manifest.files}
    )
    candidates = tuple(
        PreparedDiscoveryCandidate(
            candidate_id=record.candidate_id,
            path=record.path,
            language=record.language,
            rank=record.rank,
            score=record.score,
            ranking_signals=record.ranking_signals,
            source_sha256=files[record.path].sha256,
            source_size_bytes=files[record.path].size_bytes,
            evidence_origin=_evidence_origin(
                request.mode,
                record.path,
                manifest_paths=manifest_paths,
                structural_paths=set(knowledge.code_maps),
            ),
            structural_evidence=record.path in knowledge.code_maps,
            semantic_evidence=record.path in knowledge.semantic_analyses,
        )
        for record in session._preselected_candidates
    )
    warnings = list(session.warnings)
    if knowledge.stale_index_paths:
        warnings.append(
            CompletenessWarning(
                code="stale-index-coverage",
                message=(
                    "Stale index paths were excluded from indexed evidence; "
                    "current snapshot identities remain authoritative."
                ),
                related_paths=knowledge.stale_index_paths[:50],
                confidence=1.0,
            )
        )
    values: dict[str, Any] = {
        "task": request.task,
        "mode": request.mode,
        "pinned_paths": request.pinned_paths,
        "excluded_paths": request.excluded_paths,
        "strict": request.strict,
        "source_snapshot_digest": session.snapshot_digest,
        "index_generation_id": (
            knowledge.manifest.generation_id if knowledge.manifest is not None else None
        ),
        "index_status": _index_status(request.mode, knowledge),
        "candidates": candidates,
        "total_candidate_count": len(session._ranked_candidates),
        "stale_index_paths": knowledge.stale_index_paths,
        "warnings": _canonical_warnings(warnings),
        "budget": request.budget,
        "budget_usage": session.budget.usage(),
    }
    preparation_id = hashlib.sha256(
        canonical_json_bytes(_json_value(values))
    ).hexdigest()
    _raise_if_cancelled(cancellation)
    return DiscoveryCandidatePreparation(
        preparation_id=preparation_id,
        **values,
    )


def expand_discovery(
    source: DiscoverySource,
    preparation: DiscoveryCandidatePreparation,
    expansion: DiscoveryExpansionRequest,
    *,
    cancellation: asyncio.Event | None = None,
) -> DiscoveryExpansionResult:
    """Execute one bounded deterministic evidence lookup against repository truth."""

    if expansion.preparation_id != preparation.preparation_id:
        raise DiscoveryPreparationMismatchError(
            "expansion does not reference the supplied preparation"
        )
    if expansion.tool_name not in DISCOVERY_EXPANSION_TOOLS:
        raise DiscoveryApplicationError(
            "operation is not part of the public read-only expansion contract"
        )
    _raise_if_cancelled(cancellation)
    snapshot = _snapshot(source)
    current = prepare_discovery_candidates(snapshot, _request(preparation))
    _require_same_preparation(preparation, current)
    session, executor = _prepare_runtime(snapshot, _request(preparation))
    usage = _merged_usage(preparation.budget_usage, expansion.budget_usage)
    _restore_budget(session.budget, usage)
    if session.budget.steps >= preparation.budget.max_steps:
        raise DiscoveryApplicationError("maximum discovery steps exceeded")
    session.budget.steps += 1
    observation = executor.execute(
        step=session.budget.steps,
        action_id=expansion.action_id,
        tool_name=expansion.tool_name,
        arguments=expansion.arguments,
    )
    _raise_if_cancelled(cancellation)
    return DiscoveryExpansionResult(
        preparation_id=preparation.preparation_id,
        observation=observation,
        budget_usage=session.budget.usage(),
    )


def read_verified_context(
    source: DiscoverySource,
    preparation: DiscoveryCandidatePreparation,
    selection: DiscoverySelection,
    *,
    cancellation: asyncio.Event | None = None,
) -> VerifiedContext:
    """Read a caller selection all-or-nothing and verify every source identity."""

    if selection.preparation_id != preparation.preparation_id:
        raise DiscoveryPreparationMismatchError(
            "selection does not reference the supplied preparation"
        )
    _raise_if_cancelled(cancellation)
    snapshot = _snapshot(source)
    _require_snapshot(preparation, snapshot)
    candidates = {item.candidate_id: item for item in preparation.candidates}
    files = {item.path: item for item in snapshot.files}
    selected_files = []
    line_ranges: list[LineRangeRequest] = []
    for item in selection.items:
        candidate = candidates.get(item.candidate_id)
        if candidate is None:
            raise DiscoverySelectionError(
                f"candidate is outside the prepared set: {item.candidate_id}"
            )
        project_file = files.get(candidate.path)
        if project_file is None or project_file.sha256 != candidate.source_sha256:
            raise DiscoveryPreparationMismatchError(
                f"prepared source identity is stale: {candidate.path}"
            )
        selected_files.append(project_file)
        line_ranges.extend(
            LineRangeRequest(
                path=candidate.path,
                range=LineRange(value.start_line, value.end_line),
            )
            for value in item.ranges
        )
    if len(selected_files) > preparation.budget.max_context_files:
        raise DiscoveryApplicationError("maximum context files exceeded")
    usage = _merged_usage(preparation.budget_usage, selection.budget_usage)
    tracker = ToolBudgetTracker(preparation.budget)
    _restore_budget(tracker, usage)
    for project_file in selected_files:
        tracker.charge_read(project_file.size_bytes)
    verified = read_selected_text_files(
        snapshot,
        tuple(selected_files),
        line_ranges=tuple(line_ranges),
        limits=ReaderLimits(
            max_files=preparation.budget.max_context_files,
            max_source_bytes=preparation.budget.max_source_bytes,
            max_content_bytes=preparation.budget.max_context_bytes,
        ),
    )
    total_bytes = sum(item.included_content_bytes for item in verified)
    if total_bytes > preparation.budget.max_context_bytes:
        raise DiscoveryApplicationError("maximum context bytes exceeded")
    tracker.context_files = len(verified)
    tracker.context_bytes = total_bytes
    candidate_by_path = {
        candidates[item.candidate_id].path: item.candidate_id
        for item in selection.items
    }
    result_files = tuple(
        VerifiedContextFile(
            candidate_id=candidate_by_path[item.project_file.path],
            path=item.project_file.path,
            language=item.project_file.language,
            source_size_bytes=item.project_file.size_bytes,
            source_sha256=item.project_file.sha256,
            source_line_count=item.source_line_count,
            blocks=tuple(
                VerifiedContextBlock(
                    start_line=(
                        block.line_range.start if block.line_range is not None else None
                    ),
                    end_line=(
                        block.line_range.end if block.line_range is not None else None
                    ),
                    text=block.text,
                    line_count=block.line_count,
                    size_bytes=block.size_bytes,
                    sha256=block.sha256,
                )
                for block in item.blocks
            ),
            included_line_count=item.included_line_count,
            included_content_bytes=item.included_content_bytes,
        )
        for item in verified
    )
    _raise_if_cancelled(cancellation)
    return VerifiedContext(
        preparation_id=preparation.preparation_id,
        task=preparation.task,
        mode=preparation.mode,
        source_snapshot_digest=preparation.source_snapshot_digest,
        index_generation_id=preparation.index_generation_id,
        files=result_files,
        budget_usage=tracker.usage(),
    )


def package_verified_context(
    source: DiscoverySource,
    verified: VerifiedContext,
    *,
    include_tree: bool = True,
    cancellation: asyncio.Event | None = None,
) -> ContextPackage:
    """Re-verify and package one immutable verified-context DTO."""

    _raise_if_cancelled(cancellation)
    snapshot = _snapshot(source)
    if calculate_source_snapshot_digest(snapshot) != verified.source_snapshot_digest:
        raise DiscoveryPreparationMismatchError(
            "verified context does not match the current repository snapshot"
        )
    ranges = tuple(
        LineRangeRequest(
            path=item.path,
            range=LineRange(block.start_line, block.end_line),
        )
        for item in verified.files
        for block in item.blocks
        if block.start_line is not None and block.end_line is not None
    )
    package = build_context_package(
        snapshot,
        ContextBuildOptions(
            title=verified.task,
            selection=ContextSelection(
                exact_paths=tuple(item.path for item in verified.files),
                line_ranges=ranges,
            ),
            include_tree=include_tree,
            max_files=max(len(verified.files), 1),
            max_source_bytes_per_file=max(
                max(item.source_size_bytes for item in verified.files), 1
            ),
            max_total_content_bytes=max(
                sum(item.included_content_bytes for item in verified.files), 1
            ),
        ),
    )
    expected = tuple(
        (
            item.path,
            item.source_sha256,
            tuple(
                (block.start_line, block.end_line, block.sha256)
                for block in item.blocks
            ),
        )
        for item in verified.files
    )
    actual = tuple(
        (
            item.path,
            item.source_sha256,
            tuple(
                (block.start_line, block.end_line, block.sha256)
                for block in item.blocks
            ),
        )
        for item in package.files
    )
    if actual != expected:
        raise DiscoveryPreparationMismatchError(
            "verified content changed before package construction"
        )
    _raise_if_cancelled(cancellation)
    return package


def _prepare_runtime(
    snapshot: ProjectSnapshot, request: DiscoveryRequest
) -> tuple[DiscoverySession, DiscoveryToolExecutor]:
    session = DiscoverySession(snapshot, None, request)
    executor, _ = session.prepare_read_only_tools()
    return session, executor


def _snapshot(source: DiscoverySource) -> ProjectSnapshot:
    if isinstance(source, ProjectSnapshot):
        return source
    return scan_repository(source)


def _request(preparation: DiscoveryCandidatePreparation) -> DiscoveryRequest:
    return DiscoveryRequest(
        task=preparation.task,
        mode=preparation.mode,
        pinned_paths=preparation.pinned_paths,
        excluded_paths=preparation.excluded_paths,
        strict=preparation.strict,
        budget=preparation.budget,
    )


def _require_snapshot(
    preparation: DiscoveryCandidatePreparation, snapshot: ProjectSnapshot
) -> None:
    if calculate_source_snapshot_digest(snapshot) != preparation.source_snapshot_digest:
        raise DiscoveryPreparationMismatchError(
            "preparation does not match the current repository snapshot"
        )


def _require_same_preparation(
    expected: DiscoveryCandidatePreparation,
    actual: DiscoveryCandidatePreparation,
) -> None:
    if expected != actual:
        raise DiscoveryPreparationMismatchError(
            "repository or pinned index changed after candidate preparation"
        )


def _evidence_origin(
    mode: DiscoveryMode,
    path: str,
    *,
    manifest_paths: set[str],
    structural_paths: set[str],
) -> Literal["snapshot", "fresh", "indexed", "hybrid"]:
    if path not in structural_paths:
        return "snapshot"
    if mode is DiscoveryMode.FRESH:
        return "fresh"
    if path in manifest_paths:
        return "indexed"
    return "hybrid"


def _index_status(
    mode: DiscoveryMode, knowledge: Any
) -> Literal["not_used", "unavailable", "current", "stale"]:
    if mode is DiscoveryMode.FRESH:
        return "not_used"
    if knowledge.manifest is None:
        return "unavailable"
    if knowledge.stale_index_paths:
        return "stale"
    return "current"


def _canonical_warnings(
    warnings: list[CompletenessWarning],
) -> tuple[CompletenessWarning, ...]:
    unique = {(item.code, item.path, item.related_paths): item for item in warnings}
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda item: (item[0], item[1] or "", item[2]))
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _merged_usage(
    baseline: DiscoveryBudgetUsage, supplied: DiscoveryBudgetUsage
) -> DiscoveryBudgetUsage:
    left = baseline.model_dump()
    right = supplied.model_dump()
    return DiscoveryBudgetUsage(**{key: max(left[key], right[key]) for key in left})


def _restore_budget(tracker: ToolBudgetTracker, usage: DiscoveryBudgetUsage) -> None:
    for name, value in usage.model_dump().items():
        setattr(tracker, name, value)


def _raise_if_cancelled(cancellation: asyncio.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise asyncio.CancelledError


__all__ = [
    "DISCOVERY_EXPANSION_TOOLS",
    "DiscoveryApplicationError",
    "DiscoveryPreparationMismatchError",
    "DiscoverySelectionError",
    "expand_discovery",
    "package_verified_context",
    "prepare_discovery_candidates",
    "read_verified_context",
]
