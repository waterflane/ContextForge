"""End-to-end model-guided discovery to verified task handoff orchestration."""

from __future__ import annotations

import asyncio

from contextforge.discovery import DiscoveryRequest, discover_repository
from contextforge.discovery.tools import GitDiffProvider
from contextforge.git import GitDiffRequest
from contextforge.intelligence import ArchitectureMap, FeatureMap
from contextforge.models import ModelProvider
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
) -> DiscoveryHandoffResult:
    """Run discovery, review, current verification, packaging, and handoff creation."""

    run = await discover_repository(
        snapshot,
        discovery_provider,
        request,
        git_diff_provider=discovery_git_diff_provider,
        cancellation=cancellation,
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
    if refinement_provider is not None:
        preliminary = create_task_handoff(
            snapshot.root,
            review,
            include_codemaps=False,
            known_constraints=known_constraints,
            expected_response_format=expected_response_format,
        )
        refinement = await refine_task(
            request.task,
            preliminary.context_package,
            refinement_provider,
            cancellation=cancellation,
        )
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
    )
    return DiscoveryHandoffResult(discovery_run=run, handoff=handoff)


__all__ = ["discover_context_handoff"]
