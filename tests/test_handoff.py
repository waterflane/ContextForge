import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from contextforge.context import (
    ContextPackage,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    load_context_package_json,
    render_context_package_json,
)
from contextforge.discovery import (
    CompletenessWarning,
    DiscoveryBudgetUsage,
    DiscoveryCandidate,
    DiscoveryLineRange,
    DiscoveryMode,
    DiscoveryRequest,
    FinalContextSelection,
    SelectionReason,
)
from contextforge.handoff import (
    HANDOFF_SCHEMA_VERSION,
    ArchitectureNote,
    ContextMaterializationError,
    ContextReviewError,
    ContextSelectionReview,
    HandoffBudgetLimits,
    HandoffCodeMap,
    PromptCompileError,
    ReviewLineRange,
    ReviewSelectionItem,
    SelectionOverride,
    TaskHandoff,
    TaskRefinementError,
    TaskRefinementResponse,
    calculate_context_package_identity,
    compile_prompt,
    create_task_handoff,
    discover_context_handoff,
    prepare_context_review,
    refine_task,
)
from contextforge.intelligence import (
    AnalyzerIdentity,
    ArchitectureMap,
    CoverageSummary,
    DataFlow,
    EntryPoint,
    ExternalBoundary,
    FeatureArea,
    FeatureMap,
    ModelIdentity,
    ModuleRole,
    SemanticConfidence,
    calculate_source_snapshot_digest,
)
from contextforge.models import FakeModelProvider, ProviderConfiguration
from contextforge.repositories import ProjectSnapshot, scan_repository


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _snapshot(root: Path, files: dict[str, str]) -> ProjectSnapshot:
    for path, content in files.items():
        _write(root, path, content)
    return scan_repository(root)


def _candidate(
    snapshot: ProjectSnapshot,
    path: str,
    *,
    kind: str = "full_file",
    ranges: tuple[tuple[int, int], ...] = (),
    pinned: bool = False,
    reason: str = "Relevant to the requested behavior.",
) -> DiscoveryCandidate:
    project_file = next(item for item in snapshot.files if item.path == path)
    digest = hashlib.sha256(f"{path}:{kind}:{ranges}".encode()).hexdigest()[:24]
    return DiscoveryCandidate(
        candidate_id=f"candidate:{digest}",
        kind=kind,  # type: ignore[arg-type]
        path=path,
        ranges=tuple(
            DiscoveryLineRange(start_line=start, end_line=end) for start, end in ranges
        ),
        reason=SelectionReason(
            summary=reason,
            discovery_source="model-tool:add_to_context",
            evidence=(f"current source {path}",),
        ),
        confidence=0.9,
        source_sha256=project_file.sha256,
        manually_pinned=pinned,
        model_selected=not pinned,
    )


def _selection(
    snapshot: ProjectSnapshot,
    candidates: tuple[DiscoveryCandidate, ...],
    *,
    mode: DiscoveryMode = DiscoveryMode.HYBRID,
    task: str = "Implement the requested behavior.",
    warnings: tuple[CompletenessWarning, ...] = (),
    index_generation_id: str | None = None,
) -> FinalContextSelection:
    return FinalContextSelection(
        task=task,
        mode=mode,
        source_snapshot_digest=calculate_source_snapshot_digest(snapshot),
        index_generation_id=index_generation_id,
        selected=tuple(sorted(candidates, key=lambda item: item.candidate_id)),
        summary="Reviewed relevant implementation and tests.",
        completeness_warnings=warnings,
        confidence=0.85,
        budget_usage=DiscoveryBudgetUsage(),
        run_id=f"run-{mode.value}",
    )


@pytest.mark.parametrize(
    "mode", [DiscoveryMode.INDEXED, DiscoveryMode.FRESH, DiscoveryMode.HYBRID]
)
def test_every_discovery_mode_materializes_current_source_to_package(
    tmp_path: Path, mode: DiscoveryMode
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "src/service.py": "def serve():\n    return 1\n",
            "tests/test_service.py": (
                "from src.service import serve\n\n"
                "def test_serve():\n    assert serve() == 1\n"
            ),
        },
    )
    final = _selection(
        snapshot,
        (
            _candidate(snapshot, "src/service.py"),
            _candidate(snapshot, "tests/test_service.py", kind="related_test"),
        ),
        mode=mode,
        index_generation_id=("a" * 64 if mode is not DiscoveryMode.FRESH else None),
    )

    review = prepare_context_review(snapshot, final)
    handoff = create_task_handoff(tmp_path, review)

    assert handoff.context_package.schema_version == 1
    assert tuple(item.path for item in handoff.context_package.files) == (
        "src/service.py",
        "tests/test_service.py",
    )
    assert handoff.review.discovery.mode is mode
    assert all(item.source_sha256 for item in handoff.review.selected_items)
    assert handoff.codemaps


def test_end_to_end_flow_runs_discovery_review_verification_and_packaging(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    actions = json.dumps(
        {
            "schema_version": 1,
            "actions": [
                {
                    "schema_version": 1,
                    "action_id": "add",
                    "kind": "call_tool",
                    "tool_name": "add_to_context",
                    "arguments": {"path": "app.py", "reason": "Task source."},
                },
                {
                    "schema_version": 1,
                    "action_id": "read",
                    "kind": "call_tool",
                    "tool_name": "read_file",
                    "arguments": {"path": "app.py"},
                },
                {
                    "schema_version": 1,
                    "action_id": "finish",
                    "kind": "finalize",
                    "arguments": {
                        "summary": "Verified the selected source.",
                        "unknowns": [],
                        "completeness_claims": [],
                        "confidence": 0.9,
                    },
                },
            ],
        }
    )
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="discovery-v1",
            retry_limit=0,
        ),
        scripts=[actions],
    )

    result = asyncio.run(
        discover_context_handoff(
            snapshot,
            provider,
            DiscoveryRequest(task="Preserve VALUE.", mode=DiscoveryMode.FRESH),
            acceptance_criteria=("VALUE remains present.",),
        )
    )

    assert result.discovery_run.status == "complete"
    assert result.handoff.context_package.files[0].blocks[0].text == "VALUE = 1\n"
    assert result.handoff.acceptance_criteria == ("VALUE remains present.",)


def test_original_task_is_preserved_verbatim_in_discovery_review_and_prompt(
    tmp_path: Path,
) -> None:
    original = "  First line\nSecond line with `ticks`  "
    request = DiscoveryRequest(task=original)
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    final = _selection(snapshot, (_candidate(snapshot, "app.py"),), task=request.task)

    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(snapshot, final),
    )
    compiled = compile_prompt(handoff)

    assert request.task == original
    assert handoff.original_task == original
    original_section = compiled.prompt.body.split("## Acceptance criteria", 1)[0]
    assert original in original_section


def test_generated_refinement_is_labelled_attributed_and_adds_criteria(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    final = _selection(snapshot, (_candidate(snapshot, "app.py"),))
    review = prepare_context_review(snapshot, final)
    initial = create_task_handoff(tmp_path, review)
    response = json.dumps(
        {
            "schema_version": 1,
            "refined_task": "Clarify VALUE behavior without broadening scope.",
            "acceptance_criteria": ["VALUE remains deterministic."],
            "open_questions": ["Should VALUE be configurable?"],
            "likely_affected_areas": ["app.py"],
            "preserved_user_constraints": ["Do not broaden scope."],
        }
    )
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="refiner-v1",
            retry_limit=0,
        ),
        scripts=[response],
    )

    refinement = asyncio.run(
        refine_task(initial.original_task, initial.context_package, provider)
    )
    refined_review = review.model_copy(
        update={
            "refined_task": refinement,
            "acceptance_criteria": refinement.acceptance_criteria,
        }
    )
    handoff = create_task_handoff(tmp_path, refined_review)
    prompt = compile_prompt(handoff).prompt.body

    assert refinement.generated is True
    assert refinement.provider == "fake"
    assert refinement.model == "refiner-v1"
    assert refinement.source_package_identity == handoff.source_package_identity
    assert "## Refined task (model-generated)" in prompt
    assert "VALUE remains deterministic." in prompt


def test_malformed_refinement_response_returns_no_refinement(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "app.py"),)),
        ),
    )
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="refiner-v1",
            retry_limit=0,
        ),
        scripts=['{"schema_version":1,"invented":true}'],
    )

    with pytest.raises(TaskRefinementError):
        asyncio.run(
            refine_task(handoff.original_task, handoff.context_package, provider)
        )


def test_full_file_line_range_codemap_and_review_reasons(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "primary.py": "one\ntwo\nthree\n",
            "support.py": "alpha\nbeta\ngamma\n",
        },
    )
    final = _selection(
        snapshot,
        (
            _candidate(snapshot, "primary.py", reason="Primary implementation."),
            _candidate(
                snapshot,
                "support.py",
                kind="line_ranges",
                ranges=((2, 2),),
                reason="Supporting branch only.",
            ),
        ),
    )

    review = prepare_context_review(snapshot, final)
    handoff = create_task_handoff(tmp_path, review)

    by_path = {item.path: item for item in review.selected_items}
    assert by_path["primary.py"].representation == "full_source"
    assert by_path["support.py"].representation == "source_ranges"
    assert by_path["support.py"].ranges[0].start_line == 2
    assert by_path["primary.py"].reason.summary == "Primary implementation."
    assert by_path["primary.py"].estimated_source_bytes > 0
    assert {item.path for item in handoff.codemaps} == {"primary.py", "support.py"}
    support = next(
        item for item in handoff.context_package.files if item.path == "support.py"
    )
    assert support.blocks[0].text == "beta\n"


def _maps(snapshot: ProjectSnapshot) -> tuple[ArchitectureMap, FeatureMap]:
    analyzer = AnalyzerIdentity(
        analyzer_id="architecture-test",
        analyzer_version="1",
        analysis_prompt_version="1",
        response_schema_version=1,
        model_identity=ModelIdentity(provider_id="fake", model_id="maps-v1"),
    )
    confidence = SemanticConfidence(value=0.8, rationale="Bounded evidence.")
    coverage = CoverageSummary(
        total_files=len(snapshot.files),
        parsed_files=len(snapshot.files),
        semantically_analyzed_files=len(snapshot.files),
        total_symbols=1,
        represented_files=1,
        represented_symbols=1,
        test_files=0,
        partial=False,
    )
    digest = calculate_source_snapshot_digest(snapshot)
    common: dict[str, Any] = {
        "source_snapshot_digest": digest,
        "facts_digest": "1" * 64,
        "source_interpretations_digest": "2" * 64,
        "analyzer": analyzer,
        "analysis_options_digest": "3" * 64,
        "confidence": confidence,
        "coverage": coverage,
    }
    architecture = ArchitectureMap(
        **common,
        module_roles=(
            ModuleRole(
                role_id="role:service",
                role_kind="domain-core",
                title="Service layer",
                description="Coordinates the selected service behavior.",
                files=("service.py",),
                confidence=confidence,
            ),
        ),
        data_flows=(
            DataFlow(
                flow_id="flow:request",
                flow_kind="request",
                title="Request flow",
                description="Routes requests through the service.",
                source="caller",
                target="service",
                files=("service.py",),
                confidence=confidence,
            ),
        ),
        entry_points=(
            EntryPoint(
                entry_point_id="entry:service",
                entry_point_kind="library",
                title="Service entry",
                description="Exposes the service entry point.",
                file="service.py",
                confidence=confidence,
            ),
        ),
        external_boundaries=(
            ExternalBoundary(
                boundary_id="boundary:filesystem",
                boundary_kind="filesystem",
                title="Filesystem boundary",
                description="Represents bounded filesystem interaction.",
                files=("service.py",),
                confidence=confidence,
            ),
        ),
    )
    features = FeatureMap(
        **common,
        feature_areas=(
            FeatureArea(
                feature_id="feature:serve",
                title="Serving",
                description="Provides the serving feature.",
                participating_files=("service.py",),
                confidence=confidence,
            ),
        ),
    )
    return architecture, features


def test_architecture_and_feature_notes_are_snapshot_bound_and_rendered(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"service.py": "def serve():\n    pass\n"})
    architecture, features = _maps(snapshot)
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "service.py"),)),
    )

    handoff = create_task_handoff(
        tmp_path,
        review,
        architecture=architecture,
        features=features,
    )
    prompt = compile_prompt(handoff).prompt.body

    assert {item.kind for item in handoff.architecture_notes} == {
        "module_role",
        "feature",
        "data_flow",
        "entry_point",
        "boundary",
    }
    assert "Model-generated note" in prompt
    assert '"kind": "module_role"' in prompt
    assert "Provides the serving feature." in prompt


def test_review_override_uses_existing_manual_selectors_before_rendering(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {"a.py": "A = 1\n", "b.py": "B = 1\n", "docs/note.md": "note\n"},
    )
    final = _selection(snapshot, (_candidate(snapshot, "a.py"),))
    override = SelectionOverride(
        selection=ContextSelection(
            exact_paths=("b.py",),
            exclusions=("a.py",),
        )
    )

    review = prepare_context_review(snapshot, final, override=override)
    handoff = create_task_handoff(tmp_path, review)

    assert tuple(item.path for item in review.selected_items) == ("b.py",)
    assert review.selected_items[0].automatic is False
    assert review.selected_items[0].reason.discovery_source == "reviewer-override"
    assert handoff.context_package.files[0].path == "b.py"


def test_budget_prioritizes_primary_then_reduces_supporting_to_ranges(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "primary.py": "PRIMARY\n",
            "support.py": "one\ntwo\nthree\n",
        },
    )
    primary = _candidate(snapshot, "primary.py")
    support = _candidate(
        snapshot,
        "support.py",
        kind="line_ranges",
        ranges=((2, 2),),
    )
    limit = len(b"PRIMARY\n") + len(b"two\n")

    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (primary, support)),
        budget_limits=HandoffBudgetLimits(max_source_bytes=limit),
    )

    assert [item.representation for item in review.selected_items] == [
        "full_source",
        "source_ranges",
    ]
    assert review.estimated_budget_usage.source_content_bytes == limit


def test_pinned_file_is_preserved_over_budget_with_explicit_warning(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"required.py": "required content\n"})
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "required.py", pinned=True),)),
        budget_limits=HandoffBudgetLimits(max_source_bytes=1),
    )
    handoff = create_task_handoff(tmp_path, review)
    compiled = compile_prompt(handoff)

    assert review.selected_items[0].representation == "full_source"
    assert any(item.code == "pinned-source-over-budget" for item in review.warnings)
    assert compiled.metadata.budget_usage.source_content_bytes > 1


def test_missing_required_test_and_other_completeness_warnings_are_explicit(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {"service.py": "service\n", "tests/test_service.py": "test\n"},
    )
    inherited = CompletenessWarning(
        code="dynamic-callers-unknown",
        message="Dynamic callers cannot be proven.",
        confidence=0.3,
    )
    final = _selection(
        snapshot,
        (
            _candidate(snapshot, "service.py"),
            _candidate(snapshot, "tests/test_service.py", kind="related_test"),
        ),
        warnings=(inherited,),
    )
    review = prepare_context_review(
        snapshot,
        final,
        budget_limits=HandoffBudgetLimits(max_source_bytes=len(b"service\n")),
    )
    handoff = create_task_handoff(tmp_path, review)

    representations = {item.path: item.representation for item in review.selected_items}
    assert representations["tests/test_service.py"] == "omitted"
    codes = {item.code for item in handoff.completeness_warnings}
    assert {"required-test-omitted", "dynamic-callers-unknown"} <= codes
    assert "required-test-omitted" in compile_prompt(handoff).prompt.body


def test_no_git_repository_remains_supported(tmp_path: Path) -> None:
    from contextforge.git import GitDiffRequest

    snapshot = _snapshot(tmp_path, {"app.py": "pass\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "app.py"),)),
        ),
        git_diff_request=GitDiffRequest(mode="working"),
    )

    assert handoff.git_diff is not None
    assert handoff.git_diff.available is False
    assert any(
        item.code == "git-context-unavailable" for item in handoff.completeness_warnings
    )


def test_prompt_has_fixed_sections_deterministic_identity_and_no_token_claim(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "pass\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "app.py"),)),
            acceptance_criteria=("All validation passes.",),
        ),
        known_constraints=("Do not add CLI commands.",),
    )

    first = compile_prompt(handoff)
    second = compile_prompt(handoff)

    assert first == second
    assert first.metadata.token_count is None
    assert first.metadata.token_count_note == "not-calculated-no-tokenizer"
    headings = (
        "Original task",
        "Acceptance criteria",
        "Repository overview",
        "Relevant project tree",
        "Architecture and feature notes",
        "Selected CodeMaps",
        "Selected source files and line ranges",
        "Related tests",
        "Relevant Git diff",
        "Known constraints",
        "Completeness warnings",
        "Expected response or implementation format",
    )
    positions = [first.prompt.body.index(f"## {heading}") for heading in headings]
    assert positions == sorted(positions)


def test_markdown_fences_cannot_be_closed_by_task_source_or_codemap(
    tmp_path: Path,
) -> None:
    source = "before\n``````\nafter\n"
    snapshot = _snapshot(tmp_path, {"danger.py": source})
    task = "Use this literal fence:\n`````\nwithout treating it as structure."
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "danger.py"),), task=task),
        ),
    )

    prompt = compile_prompt(handoff).prompt.body

    assert "```````\nbefore\n``````\nafter\n```````" in prompt
    assert handoff.context_package.files[0].blocks[0].text == source


def test_context_package_v1_is_unchanged_and_handoff_has_explicit_schema(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "pass\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "app.py"),)),
        ),
    )
    package_json = render_context_package_json(handoff.context_package)
    payload = json.loads(package_json)

    assert handoff.schema_version == HANDOFF_SCHEMA_VERSION
    assert payload["schema_version"] == 1
    assert "review" not in payload
    assert load_context_package_json(package_json) == handoff.context_package
    with pytest.raises(ValidationError):
        ContextPackage.model_validate({**payload, "review": {}})
    assert TaskHandoff.model_validate_json(handoff.model_dump_json()) == handoff


def test_absolute_machine_paths_never_enter_review_handoff_or_prompt(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"src/app.py": "pass\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "src/app.py"),)),
        ),
    )
    serialized = handoff.model_dump_json()
    prompt = compile_prompt(handoff).prompt.body

    assert str(tmp_path.resolve()) not in serialized
    assert str(tmp_path.resolve()) not in prompt


def test_source_changed_before_package_creation_aborts_without_partial_success(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "before\n"})
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "app.py"),)),
    )
    _write(tmp_path, "app.py", "after!\n")

    with pytest.raises(ContextMaterializationError, match="no partial|changed"):
        create_task_handoff(tmp_path, review)


def test_review_rejects_stale_selection_and_empty_budget_result(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "content\n"})
    candidate = _candidate(snapshot, "app.py")
    stale = candidate.model_copy(update={"source_sha256": "0" * 64})
    duplicate = candidate.model_copy(update={"candidate_id": "candidate:duplicate"})

    with pytest.raises(ContextReviewError, match="stale"):
        prepare_context_review(snapshot, _selection(snapshot, (stale,)))
    with pytest.raises(ContextReviewError, match="duplicate source paths"):
        prepare_context_review(
            snapshot,
            _selection(snapshot, (candidate, duplicate)),
        )
    with pytest.raises(ContextReviewError, match="retained no"):
        prepare_context_review(
            snapshot,
            _selection(snapshot, (candidate,)),
            budget_limits=HandoffBudgetLimits(max_source_bytes=1),
        )


def test_compiler_rejects_non_handoff_and_instruction_overflow(
    tmp_path: Path,
) -> None:
    with pytest.raises(PromptCompileError, match="TaskHandoff"):
        compile_prompt(object())  # type: ignore[arg-type]

    snapshot = _snapshot(tmp_path, {"app.py": "x\n"})
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "app.py", pinned=True),)),
        budget_limits=HandoffBudgetLimits(
            max_source_bytes=1,
            max_prompt_instruction_bytes=1,
        ),
    )
    handoff = create_task_handoff(tmp_path, review)
    with pytest.raises(PromptCompileError, match="prompt instructions"):
        compile_prompt(handoff)


def test_review_can_replace_discovery_with_manual_range_selection(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {"a.py": "A\n", "b.py": "one\ntwo\nthree\n"},
    )
    override = SelectionOverride(
        replace_discovered=True,
        selection=ContextSelection(
            exact_paths=("b.py",),
            line_ranges=(LineRangeRequest("b.py", LineRange(2, 2)),),
        ),
    )
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "a.py"),)),
        override=override,
    )
    handoff = create_task_handoff(tmp_path, review)

    assert review.selected_items[0].representation == "source_ranges"
    assert handoff.context_package.files[0].blocks[0].text == "two\n"


def test_explicit_codemap_only_selection_and_disabled_codemap_output(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {"primary.py": "PRIMARY = 1\n", "structural.py": "STRUCTURAL = 1\n"},
    )
    final = _selection(
        snapshot,
        (
            _candidate(snapshot, "primary.py"),
            _candidate(snapshot, "structural.py", kind="codemap"),
        ),
    )
    review = prepare_context_review(snapshot, final)

    assert {item.path: item.representation for item in review.selected_items} == {
        "primary.py": "full_source",
        "structural.py": "codemap_only",
    }
    with_maps = create_task_handoff(tmp_path, review)
    without_maps = create_task_handoff(tmp_path, review, include_codemaps=False)
    assert {item.path for item in with_maps.codemaps} == {
        "primary.py",
        "structural.py",
    }
    assert without_maps.codemaps == ()


def test_codemap_and_architecture_budgets_emit_omission_warnings(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"service.py": "def serve():\n    pass\n"})
    architecture, features = _maps(snapshot)
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "service.py"),)),
        budget_limits=HandoffBudgetLimits(
            max_codemap_bytes=1,
            max_architecture_bytes=1,
        ),
    )

    handoff = create_task_handoff(
        tmp_path,
        review,
        architecture=architecture,
        features=features,
    )

    assert handoff.codemaps == ()
    assert handoff.architecture_notes == ()
    codes = {item.code for item in handoff.completeness_warnings}
    assert {"codemap-budget-omitted", "architecture-budget-omitted"} <= codes


def test_stale_maps_are_omitted_with_separate_warnings(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"service.py": "def serve():\n    pass\n"})
    architecture, features = _maps(snapshot)
    stale_architecture = architecture.model_copy(
        update={"source_snapshot_digest": "0" * 64}
    )
    stale_features = features.model_copy(update={"source_snapshot_digest": "0" * 64})
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "service.py"),)),
    )

    handoff = create_task_handoff(
        tmp_path,
        review,
        architecture=stale_architecture,
        features=stale_features,
    )

    codes = {item.code for item in handoff.completeness_warnings}
    assert {"stale-architecture-notes", "stale-feature-notes"} <= codes


def test_refinement_for_another_package_identity_cannot_materialize(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    review = prepare_context_review(
        snapshot,
        _selection(snapshot, (_candidate(snapshot, "app.py"),)),
    )
    initial = create_task_handoff(tmp_path, review)
    response = TaskRefinementResponse(
        refined_task="Clarified task.",
        acceptance_criteria=("Criterion.",),
    )
    from contextforge.handoff import TaskRefinement

    refinement = TaskRefinement.from_response(
        response,
        provider="fake",
        model="refiner",
        source_package_identity="0" * 64,
    )
    wrong_review = review.model_copy(update={"refined_task": refinement})

    assert initial.source_package_identity != refinement.source_package_identity
    with pytest.raises(ContextMaterializationError, match="different source package"):
        create_task_handoff(tmp_path, wrong_review)


def test_public_type_guards_and_review_snapshot_mismatch(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    final = _selection(snapshot, (_candidate(snapshot, "app.py"),))
    stale_final = final.model_copy(update={"source_snapshot_digest": "0" * 64})

    with pytest.raises(TypeError, match="ProjectSnapshot"):
        prepare_context_review(object(), final)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="FinalContextSelection"):
        prepare_context_review(snapshot, object())  # type: ignore[arg-type]
    with pytest.raises(ContextReviewError, match="current snapshot"):
        prepare_context_review(snapshot, stale_final)
    with pytest.raises(TypeError, match="ContextSelectionReview"):
        create_task_handoff(tmp_path, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="original task"):
        prepare_context_review(snapshot, final, original_task="  ")


def test_closed_handoff_models_reject_inconsistent_shapes(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "app.py"),)),
        ),
    )
    item = handoff.review.selected_items[0]

    with pytest.raises(ValidationError):
        TaskRefinementResponse(acceptance_criteria=("same", "same"))
    with pytest.raises(ValidationError):
        TaskRefinementResponse(refined_task="\x00")
    from contextforge.handoff import TaskRefinement

    with pytest.raises(ValidationError):
        TaskRefinement(
            provider="bad\nprovider",
            model="model",
            source_package_identity=handoff.source_package_identity,
        )
    with pytest.raises(ValidationError):
        ReviewLineRange(start_line=2, end_line=1)
    with pytest.raises(ValidationError):
        ReviewSelectionItem(
            **{
                **item.model_dump(),
                "representation": "source_ranges",
                "ranges": (),
            }
        )
    with pytest.raises(ValidationError):
        ArchitectureNote(
            note_id="bad",
            kind="feature",
            title="bad",
            description="bad",
            paths=("b.py", "a.py"),
        )
    with pytest.raises(ValidationError):
        ArchitectureNote(
            note_id="bad",
            kind="feature",
            title="bad\x00title",
            description="bad",
        )
    code_map = handoff.codemaps[0]
    with pytest.raises(ValidationError):
        HandoffCodeMap(**{**code_map.model_dump(), "size_bytes": 0})
    with pytest.raises(ValidationError):
        ContextSelectionReview(
            **{
                **handoff.review.model_dump(),
                "selected_items": (
                    item.model_copy(update={"representation": "omitted"}),
                ),
            }
        )
    with pytest.raises(ValidationError):
        TaskHandoff(
            **{
                **handoff.model_dump(),
                "expected_response_format": "\x00",
            }
        )


def test_compiler_handles_package_without_tree_or_structural_extras(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"app.py": "VALUE = 1\n"})
    handoff = create_task_handoff(
        tmp_path,
        prepare_context_review(
            snapshot,
            _selection(snapshot, (_candidate(snapshot, "app.py"),)),
        ),
        include_codemaps=False,
    )
    package = handoff.context_package.model_copy(update={"tree": None})
    portable = handoff.model_copy(
        update={
            "context_package": package,
            "source_package_identity": calculate_context_package_identity(package),
        }
    )

    prompt = compile_prompt(portable).prompt.body

    assert "_(Project tree was not included.)_" in prompt
    assert "## Selected CodeMaps\n\n_(none selected)_" in prompt
