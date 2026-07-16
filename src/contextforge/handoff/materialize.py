"""Validated discovery review and all-or-nothing ContextPackage materialization."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from contextforge.context import (
    ContextBuildOptions,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    ReaderLimits,
    SelectionResult,
    build_context_package,
    read_selected_text_file,
    resolve_selection,
)
from contextforge.discovery import (
    CompletenessWarning,
    DiscoveryCandidate,
    FinalContextSelection,
    SelectionReason,
)
from contextforge.git import GitDiffRequest, collect_git_diff
from contextforge.intelligence import (
    ArchitectureMap,
    FeatureMap,
    calculate_source_snapshot_digest,
    extract_code_map,
    serialize_code_map,
)
from contextforge.repositories import ProjectSnapshot, scan_repository

from .models import (
    ArchitectureNote,
    ContextSelectionReview,
    DiscoveryProvenance,
    HandoffBudgetLimits,
    HandoffBudgetUsage,
    HandoffCodeMap,
    ReviewLineRange,
    ReviewSelectionItem,
    SelectionOverride,
    TaskHandoff,
    TaskRefinement,
    calculate_context_package_identity,
    canonical_json_bytes,
)

DEFAULT_EXPECTED_RESPONSE_FORMAT = (
    "Describe the implementation outcome, list changed files, explain important "
    "design choices, and report validation performed. Do not claim work that was "
    "not completed."
)
HARD_MAX_CONTEXT_BYTES = 10 * 1024 * 1024


class ContextReviewError(ValueError):
    """Raised when discovery output cannot form a safe review checkpoint."""


class ContextMaterializationError(RuntimeError):
    """Raised before returning any partial package or handoff."""


def prepare_context_review(
    snapshot: ProjectSnapshot,
    selection: FinalContextSelection,
    *,
    original_task: str | None = None,
    refined_task: TaskRefinement | None = None,
    acceptance_criteria: tuple[str, ...] = (),
    override: SelectionOverride | None = None,
    budget_limits: HandoffBudgetLimits | None = None,
) -> ContextSelectionReview:
    """Validate and budget a model selection for reviewer modification/approval."""

    if not isinstance(snapshot, ProjectSnapshot):
        raise TypeError("context review requires a ProjectSnapshot")
    if not isinstance(selection, FinalContextSelection):
        raise TypeError("context review requires a FinalContextSelection")
    current_digest = calculate_source_snapshot_digest(snapshot)
    if selection.source_snapshot_digest != current_digest:
        raise ContextReviewError(
            "discovery selection does not match the current snapshot"
        )
    task = selection.task if original_task is None else original_task
    if not task.strip() or "\x00" in task:
        raise ContextReviewError("original task must be bounded non-empty text")
    limits = budget_limits or HandoffBudgetLimits()
    files = {item.path: item for item in snapshot.files}
    source_candidates: list[tuple[str, DiscoveryCandidate]] = []
    for item in selection.selected:
        if item.path is not None and item.kind != "git_diff":
            source_candidates.append((item.path, item))
    if len(source_candidates) != len({path for path, _ in source_candidates}):
        raise ContextReviewError("discovery selection contains duplicate source paths")
    candidates = dict(source_candidates)
    for path, selected_candidate in candidates.items():
        project_file = files.get(path)
        if project_file is None:
            raise ContextReviewError(f"selected path is absent from snapshot: {path}")
        if selected_candidate.source_sha256 != project_file.sha256:
            raise ContextReviewError(f"selected path has stale source identity: {path}")

    resolved, automatic_paths = _resolve_review_selection(
        snapshot, candidates, override
    )
    warnings = list(selection.completeness_warnings)
    planned: list[ReviewSelectionItem] = []
    used_bytes = 0
    source_count = 0
    ranges_by_path = {
        path: tuple(
            request.range for request in resolved.line_ranges if request.path == path
        )
        for path in {request.path for request in resolved.line_ranges}
    }
    ordered = sorted(
        resolved.files,
        key=lambda item: _review_priority(
            item.path,
            candidates.get(item.path),
            item.path not in automatic_paths,
        ),
    )
    decisions: dict[str, ReviewSelectionItem] = {}
    for project_file in ordered:
        candidate = candidates.get(project_file.path)
        ranges = ranges_by_path.get(project_file.path, ())
        manually_added = project_file.path not in automatic_paths
        pinned = bool(candidate and candidate.manually_pinned)
        category = _category(project_file.path, candidate, ranges)
        reason = (
            candidate.reason
            if candidate is not None and not manually_added
            else SelectionReason(
                summary="Added or modified by the reviewer through manual selectors.",
                discovery_source="reviewer-override",
            )
        )
        selected = read_selected_text_file(
            snapshot,
            project_file,
            line_ranges=ranges,
            limits=ReaderLimits(
                max_files=1,
                max_source_bytes=max(project_file.size_bytes, 1),
                max_content_bytes=max(project_file.size_bytes, 1),
            ),
        )
        included_bytes = selected.included_content_bytes
        structural_only = candidate is not None and candidate.kind == "codemap"
        can_include = (
            not structural_only
            and source_count < limits.max_files
            and used_bytes + included_bytes <= limits.max_source_bytes
        )
        if pinned:
            can_include = True
            if (
                source_count >= limits.max_files
                or used_bytes + included_bytes > limits.max_source_bytes
            ):
                warnings.append(
                    CompletenessWarning(
                        code="pinned-source-over-budget",
                        path=project_file.path,
                        message=(
                            "A manually pinned required file is preserved beyond the "
                            "configured review budget."
                        ),
                        confidence=1.0,
                    )
                )
        if can_include:
            representation: Literal[
                "full_source", "source_ranges", "codemap_only", "omitted"
            ] = "source_ranges" if ranges else "full_source"
            used_bytes += included_bytes
            source_count += 1
            estimated = included_bytes
        else:
            representation = "omitted" if category == "test" else "codemap_only"
            estimated = 0
            code = (
                "required-test-omitted"
                if category == "test"
                else "source-reduced-to-codemap"
            )
            message = (
                "A related test could not fit the approved source budget and was not "
                "silently discarded."
                if category == "test"
                else "A lower-priority source item was reduced to a current CodeMap."
            )
            warnings.append(
                CompletenessWarning(
                    code=code,
                    path=project_file.path,
                    message=message,
                    confidence=0.5,
                )
            )
        decisions[project_file.path] = ReviewSelectionItem(
            path=project_file.path,
            source_sha256=project_file.sha256,
            representation=representation,
            ranges=tuple(
                ReviewLineRange(start_line=item.start, end_line=item.end)
                for item in ranges
            ),
            reason=reason,
            confidence=candidate.confidence if candidate is not None else 1.0,
            estimated_source_bytes=project_file.size_bytes,
            estimated_included_bytes=estimated,
            pinned=pinned,
            automatic=not manually_added,
            category=category,
        )
    planned = [decisions[path] for path in sorted(decisions)]
    if not any(
        item.representation in {"full_source", "source_ranges"} for item in planned
    ):
        raise ContextReviewError("budgeting retained no verified source content")
    criteria = acceptance_criteria
    if refined_task is not None:
        criteria = tuple(dict.fromkeys((*criteria, *refined_task.acceptance_criteria)))
    usage = HandoffBudgetUsage(source_content_bytes=used_bytes)
    return ContextSelectionReview(
        original_task=task,
        refined_task=refined_task,
        acceptance_criteria=criteria,
        discovery=DiscoveryProvenance(
            mode=selection.mode,
            run_id=selection.run_id,
            source_snapshot_digest=selection.source_snapshot_digest,
            index_generation_id=selection.index_generation_id,
            summary=selection.summary,
            confidence=selection.confidence,
        ),
        selected_items=tuple(planned),
        warnings=_canonical_warnings(warnings),
        budget_limits=limits,
        estimated_budget_usage=usage,
        override=override,
    )


def create_task_handoff(
    source: str | Path | ProjectSnapshot,
    review: ContextSelectionReview,
    *,
    architecture: ArchitectureMap | None = None,
    features: FeatureMap | None = None,
    git_diff_request: GitDiffRequest | None = None,
    include_codemaps: bool = True,
    known_constraints: tuple[str, ...] = (),
    expected_response_format: str = DEFAULT_EXPECTED_RESPONSE_FORMAT,
) -> TaskHandoff:
    """Re-scan, verify every source item, and return one complete handoff or fail."""

    if not isinstance(review, ContextSelectionReview):
        raise TypeError("handoff creation requires a ContextSelectionReview")
    root = source.root if isinstance(source, ProjectSnapshot) else source
    try:
        snapshot = scan_repository(root)
        digest = calculate_source_snapshot_digest(snapshot)
        if digest != review.discovery.source_snapshot_digest:
            raise ContextMaterializationError(
                "repository source changed after review; "
                "rediscovery/reapproval is required"
            )
        files = {item.path: item for item in snapshot.files}
        source_items = tuple(
            item
            for item in review.selected_items
            if item.representation in {"full_source", "source_ranges"}
        )
        for item in review.selected_items:
            current = files.get(item.path)
            if current is None or current.sha256 != item.source_sha256:
                raise ContextMaterializationError(
                    f"reviewed source identity is no longer current: {item.path}"
                )
        context_selection = ContextSelection(
            exact_paths=tuple(item.path for item in source_items),
            line_ranges=tuple(
                LineRangeRequest(
                    path=item.path,
                    range=LineRange(value.start_line, value.end_line),
                )
                for item in source_items
                for value in item.ranges
            ),
        )
        allowed_source_bytes = max(
            review.budget_limits.max_source_bytes,
            review.estimated_budget_usage.source_content_bytes,
        )
        if allowed_source_bytes > HARD_MAX_CONTEXT_BYTES:
            raise ContextMaterializationError(
                "reviewed pinned source exceeds the hard ContextPackage byte ceiling"
            )
        package = build_context_package(
            snapshot,
            ContextBuildOptions(
                title="ContextForge task context",
                selection=context_selection,
                include_tree=True,
                max_files=max(review.budget_limits.max_files, len(source_items)),
                max_source_bytes_per_file=HARD_MAX_CONTEXT_BYTES,
                max_total_content_bytes=max(allowed_source_bytes, 1),
            ),
        )
        package_identity = calculate_context_package_identity(package)
        if (
            review.refined_task is not None
            and review.refined_task.source_package_identity != package_identity
        ):
            raise ContextMaterializationError(
                "generated task refinement targets a different source package identity"
            )

        warnings = list(review.warnings)
        codemaps = _build_codemaps(
            snapshot,
            review,
            include_codemaps=include_codemaps,
            warnings=warnings,
        )
        notes = _build_architecture_notes(
            digest,
            review,
            architecture=architecture,
            features=features,
            warnings=warnings,
        )
        git_diff = None
        if git_diff_request is not None:
            bounded_request = git_diff_request.model_copy(
                update={
                    "max_bytes": min(
                        git_diff_request.max_bytes,
                        review.budget_limits.max_git_diff_bytes,
                    )
                }
            )
            git_diff = collect_git_diff(snapshot, bounded_request)
            if not git_diff.available:
                warnings.append(
                    CompletenessWarning(
                        code="git-context-unavailable",
                        message=(
                            "Optional bounded Git context was unavailable; package "
                            "creation continued."
                        ),
                        confidence=1.0,
                    )
                )
        architecture_bytes = len(
            canonical_json_bytes([item.model_dump(mode="json") for item in notes])
        )
        usage = HandoffBudgetUsage(
            source_content_bytes=package.statistics.included_content_bytes,
            codemap_bytes=sum(item.size_bytes for item in codemaps),
            architecture_note_bytes=architecture_bytes,
            git_diff_bytes=(
                0 if git_diff is None else len(git_diff.text.encode("utf-8"))
            ),
        )
        return TaskHandoff(
            original_task=review.original_task,
            refined_task=review.refined_task,
            acceptance_criteria=review.acceptance_criteria,
            review=review,
            context_package=package,
            source_package_identity=package_identity,
            codemaps=codemaps,
            architecture_notes=notes,
            git_diff=git_diff,
            known_constraints=known_constraints,
            completeness_warnings=_canonical_warnings(warnings),
            expected_response_format=expected_response_format,
            budget_usage=usage,
        )
    except ContextMaterializationError:
        raise
    except Exception as exc:
        raise ContextMaterializationError(
            "context handoff materialization failed; no partial package was returned"
        ) from exc


def _resolve_review_selection(
    snapshot: ProjectSnapshot,
    candidates: dict[str, DiscoveryCandidate],
    override: SelectionOverride | None,
) -> tuple[SelectionResult, set[str]]:
    discovered_paths = tuple(sorted(candidates))
    discovered_ranges = tuple(
        LineRangeRequest(
            path=path,
            range=LineRange(item.start_line, item.end_line),
        )
        for path in discovered_paths
        for item in candidates[path].ranges
    )
    automatic_paths = set(discovered_paths)
    if override is None:
        request = ContextSelection(
            exact_paths=discovered_paths,
            line_ranges=discovered_ranges,
        )
    else:
        manual = override.selection
        base_paths = () if override.replace_discovered else discovered_paths
        base_ranges = () if override.replace_discovered else discovered_ranges
        if override.replace_discovered:
            automatic_paths.clear()
        request = ContextSelection(
            exact_paths=(*base_paths, *manual.exact_paths),
            directories=manual.directories,
            globs=manual.globs,
            exclusions=manual.exclusions,
            line_ranges=(*base_ranges, *manual.line_ranges),
        )
    return resolve_selection(snapshot, request), automatic_paths


def _review_priority(
    path: str,
    candidate: DiscoveryCandidate | None,
    manually_added: bool,
) -> tuple[int, str]:
    if candidate is not None and candidate.manually_pinned:
        return (0, path)
    if manually_added:
        return (1, path)
    if (
        candidate is not None
        and candidate.kind == "full_file"
        and not _looks_like_test(path)
    ):
        return (2, path)
    if candidate is not None and candidate.ranges:
        return (3, path)
    if _looks_like_test(path):
        return (4, path)
    return (5, path)


def _category(
    path: str,
    candidate: DiscoveryCandidate | None,
    ranges: tuple[LineRange, ...],
) -> Literal["primary", "supporting", "test", "structural"]:
    if _looks_like_test(path) or (
        candidate is not None and candidate.kind == "related_test"
    ):
        return "test"
    if ranges:
        return "supporting"
    if candidate is not None and candidate.kind == "codemap":
        return "structural"
    return "primary"


def _build_codemaps(
    snapshot: ProjectSnapshot,
    review: ContextSelectionReview,
    *,
    include_codemaps: bool,
    warnings: list[CompletenessWarning],
) -> tuple[HandoffCodeMap, ...]:
    if not include_codemaps:
        return ()
    files = {item.path: item for item in snapshot.files}
    ordered = sorted(
        review.selected_items,
        key=lambda item: (
            0 if item.representation == "codemap_only" else 1,
            0 if item.category in {"supporting", "test", "structural"} else 1,
            item.path,
        ),
    )
    used = 0
    values: list[HandoffCodeMap] = []
    for item in ordered:
        if item.representation == "omitted":
            continue
        code_map = extract_code_map(snapshot, files[item.path])
        serialized = serialize_code_map(code_map)
        if used + len(serialized) > review.budget_limits.max_codemap_bytes:
            warnings.append(
                CompletenessWarning(
                    code="codemap-budget-omitted",
                    path=item.path,
                    message=(
                        "A lower-priority CodeMap was omitted from the explicit "
                        "CodeMap budget."
                    ),
                    confidence=1.0,
                )
            )
            continue
        used += len(serialized)
        values.append(
            HandoffCodeMap(
                path=item.path,
                reason=(
                    "Preserves structural facts for source reduced by budget. "
                    + item.reason.summary
                    if item.representation == "codemap_only"
                    else "Provides verified structural context for selected source. "
                    + item.reason.summary
                ),
                size_bytes=len(serialized),
                sha256=hashlib.sha256(serialized).hexdigest(),
                code_map=code_map,
            )
        )
    return tuple(sorted(values, key=lambda item: item.path))


def _build_architecture_notes(
    snapshot_digest: str,
    review: ContextSelectionReview,
    *,
    architecture: ArchitectureMap | None,
    features: FeatureMap | None,
    warnings: list[CompletenessWarning],
) -> tuple[ArchitectureNote, ...]:
    relevant = {
        item.path for item in review.selected_items if item.representation != "omitted"
    }
    candidates: list[ArchitectureNote] = []
    if architecture is not None:
        if architecture.source_snapshot_digest != snapshot_digest:
            warnings.append(
                CompletenessWarning(
                    code="stale-architecture-notes",
                    message=(
                        "Architecture interpretations were omitted because their "
                        "source snapshot is stale."
                    ),
                    confidence=1.0,
                )
            )
        else:
            for role in architecture.module_roles:
                paths = tuple(path for path in role.files if path in relevant)
                if paths:
                    candidates.append(
                        _note(
                            role.role_id,
                            "module_role",
                            role.title,
                            role.description,
                            paths,
                            role.confidence.value,
                        )
                    )
            for flow in architecture.data_flows:
                paths = tuple(path for path in flow.files if path in relevant)
                if paths:
                    candidates.append(
                        _note(
                            flow.flow_id,
                            "data_flow",
                            flow.title,
                            flow.description,
                            paths,
                            flow.confidence.value,
                        )
                    )
            for entry_point in architecture.entry_points:
                paths = tuple(
                    sorted(
                        {
                            path
                            for path in (
                                entry_point.file,
                                entry_point.handler_file,
                            )
                            if path is not None and path in relevant
                        }
                    )
                )
                if paths:
                    candidates.append(
                        _note(
                            entry_point.entry_point_id,
                            "entry_point",
                            entry_point.title,
                            entry_point.description,
                            paths,
                            entry_point.confidence.value,
                        )
                    )
            for boundary in architecture.external_boundaries:
                paths = tuple(path for path in boundary.files if path in relevant)
                if paths:
                    candidates.append(
                        _note(
                            boundary.boundary_id,
                            "boundary",
                            boundary.title,
                            boundary.description,
                            paths,
                            boundary.confidence.value,
                        )
                    )
    if features is not None:
        if features.source_snapshot_digest != snapshot_digest:
            warnings.append(
                CompletenessWarning(
                    code="stale-feature-notes",
                    message=(
                        "Feature interpretations were omitted because their source "
                        "snapshot is stale."
                    ),
                    confidence=1.0,
                )
            )
        else:
            for feature in features.feature_areas:
                paths = tuple(
                    path for path in feature.participating_files if path in relevant
                )
                if paths:
                    candidates.append(
                        _note(
                            feature.feature_id,
                            "feature",
                            feature.title,
                            feature.description,
                            paths,
                            feature.confidence.value,
                        )
                    )
    selected: list[ArchitectureNote] = []
    for note in sorted(candidates, key=lambda value: value.note_id):
        proposed = (*selected, note)
        size = len(
            canonical_json_bytes([item.model_dump(mode="json") for item in proposed])
        )
        if size > review.budget_limits.max_architecture_bytes:
            warnings.append(
                CompletenessWarning(
                    code="architecture-budget-omitted",
                    message=(
                        "Lower-priority architecture notes were omitted from their "
                        "explicit budget."
                    ),
                    confidence=1.0,
                )
            )
            break
        selected.append(note)
    return tuple(selected)


def _note(
    identifier: str,
    kind: Literal[
        "module_role", "data_flow", "entry_point", "boundary", "feature", "diagnostic"
    ],
    title: str,
    description: str,
    paths: tuple[str, ...],
    confidence: float,
) -> ArchitectureNote:
    return ArchitectureNote(
        note_id=f"{kind}:{identifier}",
        kind=kind,
        title=title,
        description=description,
        paths=tuple(sorted(set(paths))),
        confidence=confidence,
    )


def _canonical_warnings(
    values: list[CompletenessWarning] | tuple[CompletenessWarning, ...],
) -> tuple[CompletenessWarning, ...]:
    warnings = {(item.code, item.path, item.related_paths): item for item in values}
    return tuple(
        warnings[key]
        for key in sorted(warnings, key=lambda item: (item[0], item[1] or "", item[2]))
    )


def _looks_like_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (
        path.lower().startswith(("test/", "tests/"))
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))
    )


__all__ = [
    "DEFAULT_EXPECTED_RESPONSE_FORMAT",
    "ContextMaterializationError",
    "ContextReviewError",
    "create_task_handoff",
    "prepare_context_review",
]
