"""End-to-end model-guided discovery to verified task handoff orchestration."""

from __future__ import annotations

import asyncio
import uuid

from contextforge.discovery import (
    DiscoveryCancelledError,
    DiscoveryRequest,
    discover_repository,
)
from contextforge.discovery.tools import GitDiffProvider
from contextforge.git import GitDiffRequest
from contextforge.intelligence import ArchitectureMap, FeatureMap
from contextforge.models import ModelProvider, ProviderCancelledError
from contextforge.progress import ProgressObserver, ProgressReporter
from contextforge.repositories import ProjectSnapshot

from .materialize import (
    DEFAULT_EXPECTED_RESPONSE_FORMAT,
    create_task_handoff,
    prepare_context_review,
)
from .models import (
    ContextSelectionReview,
    DiscoveryHandoffResult,
    HandoffBudgetLimits,
    SelectionOverride,
)
from .refinement import refine_task


async def discover_context_handoff(
    snapshot: ProjectSnapshot,
    discovery_provider: ModelProvider,
    request: DiscoveryRequest,
    *,
    refinement_provider: ModelProvider | None = None,
    discovery_git_diff_provider: GitDiffProvider | None = None,
    git_diff_request: GitDiffRequest | None = None,
    architecture: ArchitectureMap | None = None,
    features: FeatureMap | None = None,
    acceptance_criteria: tuple[str, ...] = (),
    override: SelectionOverride | None = None,
    budget_limits: HandoffBudgetLimits | None = None,
    known_constraints: tuple[str, ...] = (),
    expected_response_format: str = DEFAULT_EXPECTED_RESPONSE_FORMAT,
    cancellation: asyncio.Event | None = None,
    progress: ProgressObserver | None = None,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
) -> DiscoveryHandoffResult:
    """Run discovery, review, current verification, packaging, and handoff creation."""

    reporter = ProgressReporter(
        operation_id or uuid.uuid4().hex,
        "repository.handoff.discover",
        observer=progress,
        parent_operation_id=parent_operation_id,
        metadata={"mode": request.mode.value},
    )
    reporter.report("discovery", "Discovering repository context.", percentage=0)
    try:
        result = await _discover_context_handoff(
            snapshot,
            discovery_provider,
            request,
            refinement_provider=refinement_provider,
            discovery_git_diff_provider=discovery_git_diff_provider,
            git_diff_request=git_diff_request,
            architecture=architecture,
            features=features,
            acceptance_criteria=acceptance_criteria,
            override=override,
            budget_limits=budget_limits,
            known_constraints=known_constraints,
            expected_response_format=expected_response_format,
            cancellation=cancellation,
            progress=reporter,
        )
    except (asyncio.CancelledError, ProviderCancelledError, DiscoveryCancelledError):
        reporter.cancel()
        raise
    except BaseException as exc:
        reporter.fail(metadata={"error_type": type(exc).__name__})
        raise
    reporter.complete(message="Discovery handoff completed.")
    return result


async def _discover_context_handoff(
    snapshot: ProjectSnapshot,
    discovery_provider: ModelProvider,
    request: DiscoveryRequest,
    *,
    refinement_provider: ModelProvider | None,
    discovery_git_diff_provider: GitDiffProvider | None,
    git_diff_request: GitDiffRequest | None,
    architecture: ArchitectureMap | None,
    features: FeatureMap | None,
    acceptance_criteria: tuple[str, ...],
    override: SelectionOverride | None,
    budget_limits: HandoffBudgetLimits | None,
    known_constraints: tuple[str, ...],
    expected_response_format: str,
    cancellation: asyncio.Event | None,
    progress: ProgressReporter,
) -> DiscoveryHandoffResult:
    """Implement handoff discovery under one weighted progress boundary."""

    run = await discover_repository(
        snapshot,
        discovery_provider,
        request,
        git_diff_provider=discovery_git_diff_provider,
        cancellation=cancellation,
        progress=progress.scaled_observer(0, 55, phase_prefix="discovery"),
        parent_operation_id=(
            progress.last_event.operation_id
            if progress.last_event is not None
            else None
        ),
    )
    final = run.final_selection
    if final is None:  # pragma: no cover - DiscoveryRunRecord enforces this contract
        raise RuntimeError("complete discovery did not return a final selection")
    review = prepare_context_review(
        snapshot,
        final,
        original_task=request.task,
        acceptance_criteria=acceptance_criteria,
        override=override,
        budget_limits=budget_limits,
    )
    progress.report(
        "review",
        "Prepared the verified context selection review.",
        percentage=62,
        metadata={"selected": len(review.selected_items)},
    )
    if refinement_provider is not None:
        preliminary = create_task_handoff(
            snapshot.root,
            review,
            include_codemaps=False,
            known_constraints=known_constraints,
            expected_response_format=expected_response_format,
            progress=progress.scaled_observer(
                62, 72, phase_prefix="preliminary_materialization"
            ),
        )
        progress.report("refinement", "Refining the task.", percentage=74)
        refinement = await refine_task(
            request.task,
            preliminary.context_package,
            refinement_provider,
            cancellation=cancellation,
        )
        progress.report("refinement", "Task refinement completed.", percentage=82)
        criteria = tuple(
            dict.fromkeys((*acceptance_criteria, *refinement.acceptance_criteria))
        )
        review = ContextSelectionReview.model_validate(
            {
                **review.model_dump(),
                "refined_task": refinement,
                "acceptance_criteria": criteria,
            }
        )
    handoff = create_task_handoff(
        snapshot.root,
        review,
        architecture=architecture,
        features=features,
        git_diff_request=git_diff_request,
        known_constraints=known_constraints,
        expected_response_format=expected_response_format,
        progress=progress.scaled_observer(82, 98, phase_prefix="materialization"),
    )
    return DiscoveryHandoffResult(discovery_run=run, handoff=handoff)


__all__ = ["discover_context_handoff"]
