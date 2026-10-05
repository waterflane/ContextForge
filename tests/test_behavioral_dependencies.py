import asyncio
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import retrieve_context_candidates
from contextforge.intelligence.indexer import load_relationship_graph_projection
from contextforge.intelligence.retrieval import (
    _complementary_candidates,
    _rank_candidates,
    load_retrieval_index,
    parse_query_intent,
)


@pytest.mark.parametrize(
    ("task", "scope"),
    [
        ("execute_job", "lookup"),
        ("Find callers of execute_job", "lookup"),
        ("Where is `execute_job`?", "lookup"),
        ("Explain execute_job behavior", "behavior"),
        ("Review execute_job implementation and tests", "behavior"),
    ],
)
def test_original_syntax_distinguishes_lookup_from_behavior(
    task: str, scope: str
) -> None:
    assert parse_query_intent(task).evidence_scope == scope


@pytest.mark.parametrize("same_file", [True, False])
def test_behavior_requires_helper_without_explicit_callee_role(
    tmp_path: Path, same_file: bool
) -> None:
    helper = "def normalize_value(value):\n    return value + 1\n"
    root_source = "def execute_job(value):\n    return normalize_value(value)\n"
    if same_file:
        (tmp_path / "jobs.py").write_text(helper + root_source, encoding="utf-8")
    else:
        (tmp_path / "helpers.py").write_text(helper, encoding="utf-8")
        (tmp_path / "jobs.py").write_text(
            "from helpers import normalize_value\n" + root_source, encoding="utf-8"
        )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain execute_job behavior"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert "callee" not in {r.kind for r in retrieval.requirements.roles}
    helpers = [
        r
        for r in retrieval.requirements.source_evidence
        if r.basis == "verified-behavior"
    ]
    assert helpers
    expected_path = "jobs.py" if same_file else "helpers.py"
    assert any(
        r.path == expected_path and any(a.start_line == 1 for a in r.required_ranges)
        for r in helpers
    )
    assert not retrieval.requirements.unresolved_dependency_ids
    complete = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8000)
    )
    assert complete.compilation_sufficiency is not None
    assert complete.compilation_sufficiency.effective_status == "sufficient"
    assert complete.coverage_ledger is not None
    assert not complete.coverage_ledger.missing_requirement_ids
    # Removal retains the original frozen obligations, so a helper cannot be
    # silently erased by recomputing requirements from the smaller candidate set.
    damaged = retrieval.model_copy(
        update={
            "candidates": tuple(
                c for c in retrieval.candidates if same_file or c.path != "helpers.py"
            )
        }
    )
    if not same_file:
        compiled = compile_context_capsule(
            tmp_path, task, damaged, budget=ContextBudget(context_window_tokens=8000)
        )
        assert compiled.compilation_sufficiency is not None
        assert compiled.compilation_sufficiency.effective_status == "insufficient"
        assert (
            "required_source_evidence_missing"
            in compiled.compilation_sufficiency.reason_codes
        )


def test_requested_tests_keep_distinct_uses_of_the_anchor(tmp_path: Path) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job(value):\n    return value + 1\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_jobs.py").write_text(
        "from jobs import execute_job\n"
        "def check_positive():\n    assert execute_job(1) == 2\n"
        "def check_negative():\n    assert execute_job(-1) == 0\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Review execute_job implementation and tests"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    requirements = [
        r
        for r in retrieval.requirements.source_evidence
        if r.basis == "verified-source-test"
    ]
    assert len(requirements) == 2
    assert {a.start_line for r in requirements for a in r.required_ranges} >= {2, 4}
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"
    material = next(
        m for m in compiled.capsule.task_context if m.path == "tests/test_jobs.py"
    )
    assert all(set(r.evidence_ids) <= set(material.evidence_ids) for r in requirements)


def test_small_method_requires_initializer_without_owning_large_class(
    tmp_path: Path,
) -> None:
    (tmp_path / "meter.py").write_text(
        "class Meter:\n    def __init__(self):\n        self.offset = 7\n"
        "    def read(self):\n        return self.offset\n"
        + "".join(
            f"    def filler_{i}(self):\n        return {i}\n" for i in range(300)
        ),
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain Meter.read implementation"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    addresses = [
        a for r in retrieval.requirements.source_evidence for a in r.required_ranges
    ]
    assert any(a.start_line == 3 for a in addresses)
    assert not any(a.start_line == 1 for a in addresses)
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=4000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"
    material = next(m for m in compiled.capsule.task_context if m.path == "meter.py")
    assert any(a.start_line <= 3 <= a.end_line for a in material.ranges)
    assert "def filler_299" not in material.content
    assert all(a.end_line <= 5 for a in addresses)


def test_behavior_records_dependency_beyond_two_hops(tmp_path: Path) -> None:
    (tmp_path / "chain.py").write_text(
        "def step_three():\n    return 1\n"
        "def step_two():\n    return step_three()\n"
        "def step_one():\n    return step_two()\n"
        "def execute_job():\n    return step_one()\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain execute_job behavior"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert retrieval.requirements.unresolved_dependency_ids
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "insufficient"
    assert (
        "behavioral_dependency_unresolved"
        in compiled.compilation_sufficiency.reason_codes
    )


def test_verified_dependency_is_discovered_before_pool_truncation(
    tmp_path: Path,
) -> None:
    (tmp_path / "helpers.py").write_text(
        "def leaf_step():\n    return 7\n", encoding="utf-8"
    )
    (tmp_path / "jobs.py").write_text(
        "from helpers import leaf_step\ndef execute_job():\n    return leaf_step()\n",
        encoding="utf-8",
    )
    for i in range(72):
        (tmp_path / f"decoy_{i}.py").write_text(
            f"from jobs import execute_job\ndef execute_job_decoy_{i}():\n"
            "    return execute_job()\n",
            encoding="utf-8",
        )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = "Explain execute_job behavior"
    assert report.manifest.artifacts.structural_retrieval is not None
    index = load_retrieval_index(
        tmp_path,
        report.manifest.artifacts.structural_retrieval,
        manifest=report.manifest,
    )
    graph = load_relationship_graph_projection(tmp_path, manifest=report.manifest)
    raw = _rank_candidates(task, index, graph, working_set=(), diff_paths=())
    discovery = _complementary_candidates(task, index, raw, truncate=False)
    assert len(discovery) > 64
    result = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest, limit=64)
    )
    assert len(result.candidates) <= 64
    assert "helpers.py" in {c.path for c in result.candidates[:5]}
    assert result.requirements is not None
    assert any(
        r.path == "helpers.py" and r.basis == "verified-behavior"
        for r in result.requirements.source_evidence
    )
    assert not result.requirements.unresolved_dependency_ids
    limited = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest, limit=1)
    )
    compiled = compile_context_capsule(
        tmp_path, task, limited, budget=ContextBudget(context_window_tokens=8000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "insufficient"
