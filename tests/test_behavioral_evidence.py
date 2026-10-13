import asyncio
from pathlib import Path

from contextforge.application import build_repository_index
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import retrieve_context_candidates
from contextforge.intelligence.retrieval import verified_source_lookup


def test_material_recovers_only_known_physically_covered_ids(tmp_path: Path) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job(value):\n    print(value)\n    return value + 1\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain execute_job implementation"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    candidate = retrieval.candidates[0]
    unit = next(u for u in candidate.source_units if u.kind == "implementation")
    retrieval = retrieval.model_copy(
        update={
            "candidates": (
                candidate.model_copy(
                    update={
                        "evidence_ranges": (
                            *candidate.evidence_ranges,
                            candidate.evidence_ranges[0].model_copy(
                                update={"evidence_id": "invented-id"}
                            ),
                        )
                    }
                ),
            )
        }
    )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8_000)
    )
    material = next(m for m in compiled.capsule.task_context if m.path == "jobs.py")
    known = verified_source_lookup(report.structural.code_maps[0])
    assert unit.evidence_id in material.evidence_ids
    assert {
        identity
        for identity in material.evidence_ids
        if identity.startswith("structural-")
    } <= known.keys()
    assert "invented-id" not in material.evidence_ids
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"


def test_test_usage_requires_its_decorator_and_behavior(tmp_path: Path) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job(value):\n    return value + 1\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_jobs.py").write_text(
        "from jobs import execute_job\n@examples([1, 2])\n"
        "def verify_job(value):\n    result = execute_job(value)\n"
        "    assert result == value + 1\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain execute_job implementation and tests"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    requirement = next(
        r
        for r in retrieval.requirements.source_evidence
        if r.basis == "verified-source-test"
    )
    assert any(r.start_line == 2 for r in requirement.required_ranges)
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8_000)
    )
    material = next(
        m for m in compiled.capsule.task_context if m.path == "tests/test_jobs.py"
    )
    assert set(requirement.evidence_ids) <= set(material.evidence_ids)
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"
