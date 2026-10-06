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
    build_evidence_requirements,
    load_retrieval_index,
    parse_query_intent,
)


def test_javascript_behavior_requires_helpers_constants_and_complete_test_scopes(
    tmp_path: Path,
) -> None:
    (tmp_path / "jobs.js").write_text(
        "export const ALLOWED = new Set(['raw']);\n"
        "function clean(value) { return value.trim(); }\n"
        "export function execute_job(value) {\n"
        "  const normalized = clean(value);\n"
        "  return ALLOWED.has(normalized);\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "test_jobs.js").write_text(
        "import { execute_job } from './jobs.js';\n"
        "test('direct', () => {\n"
        "  assert.equal(execute_job(' raw '), true);\n});\n"
        "test('local', () => {\n  const result = execute_job('other');\n"
        "  assert.equal(result, false);\n});\n",
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
    source = retrieval.requirements.source_evidence
    assert {
        a.start_line for r in source if r.path == "jobs.js" for a in r.required_ranges
    } >= {1, 2, 3}
    test_ranges = [
        a for r in source if r.path == "test_jobs.js" for a in r.required_ranges
    ]
    assert any(a.start_line == 2 and a.end_line == 4 for a in test_ranges)
    assert any(a.start_line == 5 and a.end_line == 8 for a in test_ranges)
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=12000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"
    removed_ids = {
        u.evidence_id
        for c in retrieval.candidates
        if c.path == "test_jobs.js"
        for u in c.source_units
        if u.kind == "test-usage"
    }
    assert removed_ids
    damaged = retrieval.model_copy(
        update={
            "candidates": tuple(
                c.model_copy(
                    update={
                        "source_units": tuple(
                            u for u in c.source_units if u.kind != "test-usage"
                        ),
                        "evidence_ranges": tuple(
                            e
                            for e in c.evidence_ranges
                            if e.evidence_id not in removed_ids
                        ),
                    }
                )
                if c.path == "test_jobs.js"
                else c
                for c in retrieval.candidates
            )
        }
    )
    missing = compile_context_capsule(
        tmp_path, task, damaged, budget=ContextBudget(context_window_tokens=12000)
    )
    assert missing.compilation_sufficiency is not None
    assert missing.compilation_sufficiency.effective_status == "insufficient"


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


@pytest.mark.parametrize(
    "receiver", ["constant", "python-member", "typescript-member", "factory"]
)
def test_behavior_retains_receiver_and_factory_dependencies(
    tmp_path: Path, receiver: str
) -> None:
    if receiver == "constant":
        source = (
            "CONFIG = {'limit': 7}\n"
            + "".join(f"def filler_{i}():\n    return {i}\n" for i in range(150))
            + "def execute_job():\n    return CONFIG.get('limit', 0)\n"
        )
        filename, task, required_line = "jobs.py", "Explain execute_job behavior", 1
    elif receiver == "factory":
        source = (
            "def do_work():\n    return 7\ndef make_worker():\n    return do_work\n"
            + "".join(f"def filler_{i}():\n    return {i}\n" for i in range(150))
            + "def execute_job():\n    return make_worker()()\n"
        )
        filename, task, required_line = "jobs.py", "Explain execute_job behavior", 1
    elif receiver == "python-member":
        source = (
            "class Meter:\n    def __init__(self):\n"
            "        self.config = {'limit': 7}\n"
            + "".join(
                f"    def filler_{i}(self):\n        return {i}\n" for i in range(150)
            )
            + "    def read(self):\n        return self.config.get('limit', 0)\n"
        )
        filename, task, required_line = "jobs.py", "Explain Meter.read behavior", 3
    else:
        source = (
            "class Meter {\n constructor() { this.config = new Map([['limit', 7]]); }\n"
            + "".join(f" filler_{i}() {{ return {i}; }}\n" for i in range(150))
            + " read() { return this.config.get('limit'); }\n}\n"
        )
        filename, task, required_line = "jobs.ts", "Explain Meter.read behavior", 2
    (tmp_path / filename).write_text(source, encoding="utf-8")
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    requirements = [
        r
        for r in retrieval.requirements.source_evidence
        if any(a.start_line == required_line for a in r.required_ranges)
    ]
    assert requirements
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=6000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"
    material = next(m for m in compiled.capsule.task_context if m.path == filename)
    assert any(a.start_line <= required_line <= a.end_line for a in material.ranges)
    assert "filler_75" not in material.content
    missing_ids = {identity for r in requirements for identity in r.evidence_ids}
    damaged = retrieval.model_copy(
        update={
            "candidates": tuple(
                c.model_copy(
                    update={
                        "source_units": tuple(
                            u
                            for u in c.source_units
                            if u.evidence_id not in missing_ids
                        ),
                        "evidence_ranges": tuple(
                            e
                            for e in c.evidence_ranges
                            if e.evidence_id not in missing_ids
                        ),
                    }
                )
                for c in retrieval.candidates
            )
        }
    )
    missing = compile_context_capsule(
        tmp_path, task, damaged, budget=ContextBudget(context_window_tokens=6000)
    )
    assert missing.compilation_sufficiency is not None
    assert missing.compilation_sufficiency.effective_status == "insufficient"


def test_inline_callback_binding_is_a_reference_with_complete_behavior(
    tmp_path: Path,
) -> None:
    (tmp_path / "jobs.js").write_text(
        "function clean(value) { return value.trim(); }\n"
        "export function execute_job(values) {\n"
        "  return values.map(value => clean(value));\n}\n",
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
    assert not retrieval.requirements.unresolved_dependency_ids
    relations = report.structural.code_maps[0].relationships
    callback = next(s for s in report.structural.code_maps[0].symbols if s.is_anonymous)
    binding = [r for r in relations if r.target.symbol_id == callback.symbol_id]
    assert any(r.kind == "reference" for r in binding)
    assert not any(r.kind == "call" for r in binding)
    assert not any(
        "callback" in s for c in retrieval.candidates for s in c.matched_symbols
    )
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"


@pytest.mark.parametrize("shadowed", [False, True])
def test_python_lambda_dependencies_are_references_and_respect_parameters(
    tmp_path: Path, shadowed: bool
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def clean(value):\n    return value + 1\n"
        "def execute_job(values):\n"
        + (
            "    return map(lambda clean: clean(1), values)\n"
            if shadowed
            else "    return map(lambda value: clean(value), values)\n"
        ),
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    code_map = report.structural.code_maps[0]
    function = next(s for s in code_map.symbols if s.name == "execute_job")
    assert not any(c.observed_name == "clean" for c in function.direct_calls)
    assert any(
        r.observed_name == "clean" and r.resolution == "internal"
        for r in function.direct_references
    ) == (not shadowed)
    task = "Explain execute_job behavior"
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert retrieval.requirements is not None
    assert any(
        a.start_line == 1
        for q in retrieval.requirements.source_evidence
        for a in q.required_ranges
    ) == (not shadowed)
    compiled = compile_context_capsule(
        tmp_path, task, retrieval, budget=ContextBudget(context_window_tokens=8000)
    )
    assert compiled.compilation_sufficiency is not None
    assert compiled.compilation_sufficiency.effective_status == "sufficient"


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


@pytest.mark.parametrize("unit_kind", ["constant", "initializer", "test-usage"])
def test_behavior_rejects_removed_mandatory_source_unit(
    tmp_path: Path, unit_kind: str
) -> None:
    if unit_kind == "constant":
        source = "LIMIT = 7\ndef execute_job(value):\n    return value > LIMIT\n"
        task = "Explain execute_job behavior"
    elif unit_kind == "initializer":
        source = (
            "class Meter:\n    def __init__(self):\n        self.offset = 7\n"
            "    def read(self):\n        return self.offset\n"
        )
        task = "Explain Meter.read behavior"
    else:
        source = "def execute_job(value):\n    return value + 1\n"
        task = "Review execute_job implementation and tests"
        (tmp_path / "test_jobs.py").write_text(
            "from jobs import execute_job\ndef check_result():\n"
            "    actual = execute_job(1)\n    assert actual == 2\n",
            encoding="utf-8",
        )
    (tmp_path / "jobs.py").write_text(source, encoding="utf-8")
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            task,
            manifest=report.manifest,
        )
    )
    budget = ContextBudget(context_window_tokens=8000)
    complete = compile_context_capsule(tmp_path, task, result, budget=budget)
    assert complete.compilation_sufficiency is not None
    assert complete.compilation_sufficiency.effective_status == "sufficient"
    removed_ids = {
        unit.evidence_id
        for candidate in result.candidates
        for unit in candidate.source_units
        if (
            unit.kind == unit_kind
            or (unit_kind == "constant" and unit.kind == "initializer")
        )
    }
    assert removed_ids
    damaged = result.model_copy(
        update={
            "candidates": tuple(
                candidate.model_copy(
                    update={
                        "source_units": tuple(
                            u
                            for u in candidate.source_units
                            if u.evidence_id not in removed_ids
                        ),
                        "evidence_ranges": tuple(
                            e
                            for e in candidate.evidence_ranges
                            if e.evidence_id not in removed_ids
                        ),
                    }
                )
                for candidate in result.candidates
            )
        }
    )
    incomplete = compile_context_capsule(tmp_path, task, damaged, budget=budget)
    assert incomplete.compilation_sufficiency is not None
    assert incomplete.compilation_sufficiency.effective_status == "insufficient"
    assert (
        "required_source_evidence_missing"
        in incomplete.compilation_sufficiency.reason_codes
    )


@pytest.mark.parametrize("legacy", ["missing-units", "old-capability"])
@pytest.mark.parametrize("capability", [0, 2, 3])
def test_legacy_missing_source_units_do_not_certify_behavior(
    tmp_path: Path, legacy: str, capability: int
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    task = "Explain execute_job behavior"
    result = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    candidates = tuple(
        c.model_copy(
            update={
                "source_units": () if legacy == "missing-units" else c.source_units,
                "source_evidence_version": capability,
            }
        )
        for c in result.candidates
    )
    legacy_result = result.model_copy(
        update={
            "candidates": candidates,
            "requirements": (
                result.requirements
                if legacy == "old-capability"
                else build_evidence_requirements(task, candidates)
            ),
        }
    )
    compiled = compile_context_capsule(
        tmp_path,
        task,
        legacy_result,
        budget=ContextBudget(context_window_tokens=8000),
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
