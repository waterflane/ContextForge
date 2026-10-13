import asyncio
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import (
    CandidateGraphNeighbor,
    ContextPlanningMode,
    EvidencePlan,
    PlannedEvidence,
    PlanningDiagnostics,
    retrieve_context_candidates,
)
from contextforge.intelligence.retrieval import build_evidence_requirements


@pytest.mark.parametrize("damage", ["none", "unrelated", "sha", "id", "unselected"])
def test_planner_binding_uses_the_frozen_source_requirement(
    tmp_path: Path, damage: str
) -> None:
    from contextforge.intelligence.retrieval import (
        _PlanResponse,
        _validate_plan_response,
        build_coverage_ledger,
    )

    sources = {
        "jobs.py": "def execute_job():\n    return 7\n",
        "tests/test_jobs.py": "from jobs import execute_job\ndef test_job():\n"
        "    assert execute_job() == 7\n",
        "tests/test_other.py": "def test_unrelated():\n    assert True\n",
    }
    for path, source in sources.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Review execute_job implementation tests"
    result = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    unrelated = asyncio.run(
        retrieve_context_candidates(
            tmp_path, "test_unrelated", manifest=report.manifest
        )
    )
    supplied = {c.candidate_id: c for c in (*unrelated.candidates, *result.candidates)}
    target = next(c for c in supplied.values() if c.path == "tests/test_jobs.py")
    if damage == "unrelated":
        target = next(c for c in supplied.values() if c.path == "tests/test_other.py")
    if damage == "sha":
        target = target.model_copy(update={"source_sha256": "0" * 64})
        supplied[target.candidate_id] = target
    ids = tuple(e.evidence_id for e in target.evidence_ranges if e.evidence_id)
    if damage == "id":
        ids = ("unknown-evidence",)
    selected = tuple(
        {"candidate_id": c.candidate_id, "representation": "full"}
        for c in supplied.values()
        if damage != "unselected" or c.candidate_id != target.candidate_id
    )
    response = _PlanResponse.model_validate(
        {
            "selected": selected,
            "sufficiency": "sufficient",
            "role_bindings": [
                {
                    "role_id": "test",
                    "candidate_id": target.candidate_id,
                    "evidence_ids": ids,
                }
            ],
        }
    )
    plan = _validate_plan_response(
        response,
        supplied,
        result.source_snapshot_digest,
        task=task,
        mode=ContextPlanningMode.AUTO,
        provider_calls=1,
        input_tokens=1,
        output_tokens=1,
        rounds=1,
        max_files=12,
        max_ranges_per_file=16,
        requirements=result.requirements,
    )
    assert plan is not None
    assert bool(plan.role_bindings) == (damage == "none")
    ledger = build_coverage_ledger(
        task,
        tuple(supplied.values()),
        selected_candidate_ids=tuple(i.candidate_id for i in plan.items),
        planner_bindings=plan.role_bindings,
        requirements=result.requirements,
    )
    assert tuple(b for b in ledger.bindings if b.source == "planner") == (
        plan.role_bindings
    )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_class_qualified_anchor_requires_unique_source_identity(
    tmp_path: Path, ambiguous: bool
) -> None:
    source = "class Widget:\n    def run(self):\n        return 7\n"
    (tmp_path / "widget.py").write_text(source, encoding="utf-8")
    if ambiguous:
        (tmp_path / "other.py").write_text(source, encoding="utf-8")
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain Widget.run implementation"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert retrieval.requirements.topic_grounding == (
        "unresolved" if ambiguous else "exact-identifier"
    )
    assert bool(retrieval.requirements.ambiguous_identifiers) == ambiguous
    if not ambiguous:
        anchor = next(c for c in retrieval.candidates if c.path == "widget.py")
        assert anchor.resolved_symbols[0].qualified_name == "widget.Widget.run"
        assert anchor.resolved_symbols[0].query_identifier == "Widget.run"
        assert anchor.resolved_symbols[0].resolution == "unique-qualified-suffix"
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "insufficient" if ambiguous else "sufficient"
    )


@pytest.mark.parametrize("duplicate", [False, True])
def test_qualified_resolution_preserves_priority_and_symbol_ambiguity(
    tmp_path: Path, duplicate: bool
) -> None:
    (tmp_path / "widget.py").write_text(
        "class Widget:\n    def run(self):\n        return 7\n"
        + ("    def run(self):\n        return 9\n" if duplicate else ""),
        encoding="utf-8",
    )
    for i in range(70):
        (tmp_path / f"other{i:02}.py").write_text(
            f"class Other{i}:\n    def run(self):\n        return 0\n",
            encoding="utf-8",
        )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain Widget.run implementation"
    result = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert result.requirements is not None
    if not duplicate:
        assert result.candidates[0].path == "widget.py"
        assert result.candidates[0].exact_group == "exact_qualified_symbol"
    else:
        assert result.requirements.ambiguous_identifiers == ("Widget.run",)
    compiled = compile_context_capsule(
        tmp_path, task, result, budget=ContextBudget(context_window_tokens=8_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "insufficient" if duplicate else "sufficient"
    )


@pytest.mark.parametrize("long_method", [False, True])
def test_required_method_does_not_require_its_large_container(
    tmp_path: Path, long_method: bool
) -> None:
    padding = "".join(f"    unused_{i} = {repr('x' * 60)}\n" for i in range(800))
    body = (
        "".join(f"        value_{i} = {repr('x' * 60)}\n" for i in range(800))
        if long_method
        else ""
    )
    (tmp_path / "widget.py").write_text(
        "class Widget:\n    def run(self):\n" + body + "        return 7\n" + padding,
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain Widget.run implementation"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=2_500)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "insufficient" if long_method else "sufficient"
    )
    assert compiled.token_count <= 2_500
    if not long_method:
        assert all(
            r.end_line < 20 for m in compiled.capsule.task_context for r in m.ranges
        )


@pytest.mark.parametrize("direction", ["callers", "callees"])
def test_same_file_call_endpoints_use_symbol_bound_source(
    tmp_path: Path, direction: str
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def finish_job():\n    return 7\ndef execute_job():\n"
        "    return finish_job()\ndef invoke_job():\n    return execute_job()\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = f"Find {direction} of execute_job"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"
    assert retrieval.requirements is not None
    assert all(r.anchor_symbol_id for r in retrieval.requirements.source_evidence)


@pytest.mark.parametrize("missing", [False, True])
def test_two_symbols_in_one_file_keep_separate_test_obligations(
    tmp_path: Path, missing: bool
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\ndef finish_job():\n    return 9\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_jobs.py").write_text(
        "from jobs import execute_job, finish_job\ndef test_execute():\n"
        "    assert execute_job() == 7\ndef test_finish():\n"
        "    assert finish_job() == 9\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Review jobs.execute_job jobs.finish_job implementation tests"
    result = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert result.requirements is not None
    test_requirements = [
        r for r in result.requirements.source_evidence if r.role_id == "test"
    ]
    assert len({r.anchor_symbol_id for r in test_requirements}) == 2
    if missing:
        removed = set(test_requirements[0].evidence_ids)
        result = result.model_copy(
            update={
                "candidates": tuple(
                    c.model_copy(
                        update={
                            "evidence_ranges": tuple(
                                e
                                for e in c.evidence_ranges
                                if e.evidence_id not in removed
                            )
                        }
                    )
                    for c in result.candidates
                )
            }
        )
    compiled = compile_context_capsule(
        tmp_path, task, result, budget=ContextBudget(context_window_tokens=8_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "insufficient" if missing else "sufficient"
    )


@pytest.mark.parametrize("direction", ["caller", "callers", "callee", "callees"])
@pytest.mark.parametrize("missing", [False, True])
def test_directed_role_requests_require_the_requested_endpoint(
    tmp_path: Path, direction: str, missing: bool
) -> None:
    (tmp_path / "jobs.py").write_text(
        "from leaf import finish_job\ndef execute_job():\n    return finish_job()\n",
        encoding="utf-8",
    )
    (tmp_path / "client.py").write_text(
        "from jobs import execute_job\ndef invoke_job():\n    return execute_job()\n",
        encoding="utf-8",
    )
    (tmp_path / "leaf.py").write_text(
        "def finish_job():\n    return 7\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = f"Find {direction} of execute_job"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    kind = "caller" if direction.startswith("caller") else "callee"
    other = "callee" if kind == "caller" else "caller"
    assert kind in {r.kind for r in retrieval.requirements.roles}
    assert other not in {r.kind for r in retrieval.requirements.roles}
    endpoint = "client.py" if kind == "caller" else "leaf.py"
    if missing:
        retrieval = retrieval.model_copy(
            update={
                "candidates": tuple(
                    c for c in retrieval.candidates if c.path != endpoint
                )
            }
        )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=16_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "insufficient" if missing else "sufficient"
    )


@pytest.mark.parametrize(
    "task",
    [
        "Explain job behavior",
        "missing_job implementation",
        "Explain AcmeTools job behavior",
    ],
)
def test_heuristic_anchor_cannot_certify_task_topic(tmp_path: Path, task: str) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert retrieval.requirements.topic_grounding == "unresolved"
    assert all(
        r.basis != "exact-symbol" for r in retrieval.requirements.source_evidence
    )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "insufficient"
    assert "task_anchor_unresolved" in compiled.compilation_sufficiency.reason_codes


@pytest.mark.parametrize("damage", [None, "ungrounded-topic", "missing-source"])
def test_grounded_topic_requires_materialized_support(
    tmp_path: Path, damage: str | None
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    retrieval = asyncio.run(
        retrieve_context_candidates(
            tmp_path, "execute_job implementation", manifest=report.manifest
        )
    )
    candidate = retrieval.candidates[0]
    interpreted = candidate.model_copy(
        update={
            "exact_group": "approximate",
            "matched_concepts": ("job behavior",),
            "evidence_ranges": tuple(
                e.model_copy(
                    update={
                        "strength": "verified"
                        if damage == "ungrounded-topic"
                        else "grounded"
                    }
                )
                for e in candidate.evidence_ranges
            ),
        }
    )
    task = "Explain job behavior"
    requirements = build_evidence_requirements(task, (interpreted,))
    if damage == "missing-source":
        interpreted = interpreted.model_copy(update={"evidence_ranges": ()})
    compiled = compile_context_capsule(
        tmp_path,
        task,
        retrieval.model_copy(
            update={
                "task": task,
                "candidates": (interpreted,),
                "requirements": requirements,
            }
        ),
        budget=ContextBudget(context_window_tokens=8_000),
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "sufficient" if damage is None else "insufficient"
    )


@pytest.mark.parametrize("missing", [None, "amber.py", "cobalt.py"])
def test_grounded_facets_keep_separate_source_obligations(
    tmp_path: Path, missing: str | None
) -> None:
    for name in ("amber", "cobalt"):
        (tmp_path / f"{name}.py").write_text(
            f"def {name}_stage():\n    return 7\n", encoding="utf-8"
        )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    retrieval = asyncio.run(
        retrieve_context_candidates(
            tmp_path, "amber_stage cobalt_stage", manifest=report.manifest
        )
    )
    candidates = tuple(
        candidate.model_copy(
            update={
                "match_origin": "grounded-semantic",
                "matched_concepts": (Path(candidate.path).stem,),
                "topical_term_weights": {Path(candidate.path).stem: 1.0},
                "evidence_ranges": tuple(
                    evidence.model_copy(update={"strength": "grounded"})
                    for evidence in candidate.evidence_ranges
                ),
            }
        )
        for candidate in retrieval.candidates
    )
    task = "Explain amber cobalt behavior"
    requirements = build_evidence_requirements(task, candidates)
    assert {r.path for r in requirements.source_evidence} == {"amber.py", "cobalt.py"}
    compiled = compile_context_capsule(
        tmp_path,
        task,
        retrieval.model_copy(
            update={
                "task": task,
                "requirements": requirements,
                "candidates": tuple(c for c in candidates if c.path != missing),
            }
        ),
        budget=ContextBudget(context_window_tokens=8_000),
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "sufficient" if missing is None else "insufficient"
    )


@pytest.mark.parametrize("identifier", ["jobs.py", "jobs.execute_job", "`execute_job`"])
def test_exact_anchor_forms_resolve_verified_source(
    tmp_path: Path, identifier: str
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = f"Explain {identifier} implementation"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert retrieval.requirements.topic_grounding == "exact-identifier"
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"


@pytest.mark.parametrize(
    "task",
    [
        "Trace execute_job implementation flow tests",
        "Проследи поток execute_job: реализация и тесты",
    ],
)
@pytest.mark.parametrize(
    "missing", [None, "leaf.py", "tests/test_jobs.py", "range", "id"]
)
def test_complete_and_damaged_source_sets(
    tmp_path: Path,
    task: str,
    missing: str | None,
) -> None:
    sources = {
        "jobs.py": "from middle import route_job\ndef execute_job():\n"
        "    return route_job()\n",
        "middle.py": "from leaf import finish_job\ndef route_job():\n"
        "    return finish_job()\n",
        "leaf.py": "def finish_job():\n    return 7\n",
        "tests/test_jobs.py": "from jobs import execute_job\ndef test_execute():\n"
        "    assert execute_job() == 7\n",
    }
    for path, source in sources.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert {"jobs.py", "middle.py", "leaf.py", "tests/test_jobs.py"} <= {
        r.path for r in retrieval.requirements.source_evidence
    }
    candidates = retrieval.candidates
    if missing in sources:
        candidates = tuple(c for c in candidates if c.path != missing)
    elif missing in {"range", "id"}:
        anchor = next(c for c in candidates if c.path == "jobs.py")
        required_id = anchor.resolved_symbols[0].implementation_evidence_id
        changed = tuple(
            e.model_copy(update={"evidence_id": "substituted-source-id"})
            if missing == "id"
            else e
            for e in anchor.evidence_ranges
            if missing == "id" or e.evidence_id != required_id
        )
        candidates = tuple(
            c.model_copy(update={"evidence_ranges": changed})
            if c.path == anchor.path
            else c
            for c in candidates
        )
    compiled = compile_context_capsule(
        tmp_path,
        task,
        retrieval.model_copy(update={"candidates": candidates}),
        budget=ContextBudget(context_window_tokens=16_000),
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "sufficient" if missing is None else "insufficient"
    )
    assert compiled.coverage_ledger is not None
    assert bool(compiled.coverage_ledger.missing_requirement_ids) == (
        missing is not None
    )
    assert compiled.token_count <= 16_000


def test_map_planner_claim_and_unrelated_neighbor_do_not_prove_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "execute_job implementation"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    candidate = retrieval.candidates[0]
    expanded = candidate.model_copy(
        update={
            "graph_neighbors": (
                CandidateGraphNeighbor(
                    path="irrelevant.py",
                    distance=1,
                    relationship_kinds=("call",),
                    provenance=("verified",),
                ),
            )
        }
    )
    budget = ContextBudget(context_window_tokens=8_000)
    complete = compile_context_capsule(tmp_path, task, retrieval, budget=budget)
    unrelated = compile_context_capsule(
        tmp_path,
        task,
        retrieval.model_copy(update={"candidates": (expanded,)}),
        budget=budget,
    )
    assert complete.compilation_sufficiency == unrelated.compilation_sufficiency
    plan = EvidencePlan(
        source_snapshot_digest=retrieval.source_snapshot_digest,
        items=(
            PlannedEvidence(
                candidate_id=candidate.candidate_id,
                path=candidate.path,
                source_sha256=candidate.source_sha256,
                evidence_ids=(),
                representation="map",
            ),
        ),
        sufficiency="sufficient",
        diagnostics=PlanningDiagnostics(
            mode=ContextPlanningMode.AUTO, status="planned"
        ),
        requirements=retrieval.requirements,
    )
    missing = compile_context_capsule(
        tmp_path,
        task,
        retrieval.model_copy(update={"evidence_plan": plan}),
        budget=budget,
    )
    assert missing.compilation_sufficiency is not None
    assert missing.compilation_sufficiency.effective_status == "insufficient"
    assert "plan_replaced" in missing.compilation_sufficiency.reason_codes


@pytest.mark.parametrize("link", ["call", "import", "unrelated"])
@pytest.mark.parametrize("planner_claim", [False, True])
def test_requested_test_must_be_bound_to_the_implementation(
    tmp_path: Path, link: str, planner_claim: bool
) -> None:
    sources = {
        "jobs.py": "def execute_job():\n    return 7\n",
        "checks/test_focus.py": (
            "from jobs import execute_job\ndef test_focus():\n"
            + (
                "    assert execute_job() == 7\n"
                if link == "call"
                else "    assert True\n"
            )
        ),
        "checks/test_unrelated.py": "def test_unrelated():\n    assert True\n",
    }
    if link == "unrelated":
        sources["checks/test_focus.py"] = "def test_focus():\n    assert True\n"
    for path, source in sources.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Review execute_job implementation and tests"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    # Supply all source candidates so absence from lexical search cannot hide the bug.
    all_candidates = {
        c.path: c
        for query in ("execute_job", "test_focus", "test_unrelated")
        for c in asyncio.run(
            retrieve_context_candidates(tmp_path, query, manifest=report.manifest)
        ).candidates
    }
    all_candidates.update({c.path: c for c in retrieval.candidates})
    candidates = tuple(all_candidates[path] for path in sorted(all_candidates))
    requirements = build_evidence_requirements(task, candidates)
    retrieval = retrieval.model_copy(
        update={"candidates": candidates, "requirements": requirements}
    )
    if planner_claim:
        retrieval = retrieval.model_copy(
            update={
                "evidence_plan": EvidencePlan(
                    source_snapshot_digest=retrieval.source_snapshot_digest,
                    items=tuple(
                        PlannedEvidence(
                            candidate_id=c.candidate_id,
                            path=c.path,
                            source_sha256=c.source_sha256,
                            evidence_ids=tuple(
                                sorted(
                                    e.evidence_id
                                    for e in c.evidence_ranges
                                    if e.evidence_id
                                )
                            ),
                            representation="full",
                        )
                        for c in candidates
                    ),
                    sufficiency="sufficient",
                    diagnostics=PlanningDiagnostics(
                        mode=ContextPlanningMode.AUTO, status="planned"
                    ),
                    requirements=requirements,
                )
            }
        )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=16_000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == (
        "sufficient" if link == "call" else "insufficient"
    )
    assert compiled.coverage_ledger is not None
    assert ("test" in compiled.coverage_ledger.covered_role_ids) == (link == "call")
    if link == "call":
        assert any(
            r.path == "checks/test_focus.py" and r.role_id == "test"
            for r in requirements.source_evidence
        )
        damaged = compile_context_capsule(
            tmp_path,
            task,
            retrieval.model_copy(
                update={
                    "candidates": tuple(
                        c for c in candidates if c.path != "checks/test_focus.py"
                    ),
                    "evidence_plan": None,
                }
            ),
            budget=ContextBudget(context_window_tokens=16_000),
        )
        assert damaged.compilation_sufficiency is not None
        assert damaged.compilation_sufficiency.effective_status == "insufficient"
