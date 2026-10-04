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
        changed = tuple(
            e.model_copy(update={"evidence_id": "substituted-source-id"})
            if missing == "id"
            else e
            for e in anchor.evidence_ranges[1:]
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
    assert (
        "required_source_evidence_missing"
        in missing.compilation_sufficiency.reason_codes
    )
