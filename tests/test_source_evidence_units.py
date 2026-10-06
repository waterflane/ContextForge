from pathlib import Path

import pytest

from contextforge.intelligence import extract_code_map
from contextforge.intelligence.retrieval import (
    _candidate_evidence,
    build_retrieval_index,
)
from contextforge.intelligence.source_evidence import (
    derive_source_evidence_units,
    select_source_evidence_units,
)
from contextforge.repositories import scan_repository


def test_units_preserve_observed_callbacks_and_test_decorators(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "@cases([1, 2])\ndef check_result(value):\n"
        "    register(lambda: value)\n    assert value > 0\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    units = derive_source_evidence_units(code_map)
    assert {u.source_sha256 for u in units} == {code_map.source_sha256}
    assert {u.kind for u in units} >= {
        "implementation",
        "decorator",
        "call",
        "callback",
    }
    assert all(
        u.basis == "observed-syntax" for u in units if u.kind != "implementation"
    )
    implementation = next(u for u in units if u.kind == "implementation")
    assert implementation.source_range.end_line == 4
    assert len({u.evidence_id for u in units}) == len(units)


def test_repeated_assignment_targets_preserve_one_initializer(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "class Meter:\n    def __init__(self):\n"
        "        self.offset = self.offset = 7\n"
        "    def read(self):\n        return self.offset\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    assert code_map.parse_status == "parsed"
    constructor = next(s for s in code_map.symbols if s.name == "__init__")
    assert len(constructor.initializations) == 1
    assert constructor.initializations[0].observed_name == "self.offset"
    method = next(s for s in code_map.symbols if s.name == "read")
    selected = select_source_evidence_units(
        derive_source_evidence_units(code_map), (method.declaration_range,)
    )
    initializers = [unit for unit in selected if unit.kind == "initializer"]
    assert len(initializers) == 1
    assert initializers[0].source_range.start_line == 3
    assert method.symbol_id in initializers[0].related_symbol_ids


def test_unit_selection_keeps_the_smallest_method_owner(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "class Container:\n    def run(self):\n        return 1\n"
        "    def unrelated(self):\n        return 2\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    method = next(s for s in code_map.symbols if s.name == "run")
    selected = select_source_evidence_units(
        derive_source_evidence_units(code_map), (method.declaration_range,)
    )
    assert {u.owner_symbol_id for u in selected} == {method.symbol_id}
    assert max(u.source_range.end_line for u in selected) == 3


def test_multiple_evidence_ids_on_one_range_survive_discovery(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    document = build_retrieval_index((code_map,), (), "0" * 64).documents[0]
    posting = document.positional_postings[0]
    document = document.model_copy(
        update={
            "positional_postings": (
                posting,
                posting.model_copy(update={"evidence_id": "second-known-id"}),
            )
        }
    )
    evidence = _candidate_evidence(document, ("run",), ())
    assert {e.evidence_id for e in evidence} == {posting.evidence_id, "second-known-id"}


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        (
            "sample.py",
            "class Meter:\n    def __init__(self):\n        self.offset = 7\n"
            "    def read(self):\n        return self.offset\n",
        ),
        (
            "sample.ts",
            "class Meter {\n  constructor() { this.offset = 7; }\n"
            "  read() { return this.offset; }\n}\n",
        ),
    ],
)
def test_state_units_bind_observed_initialization_to_reader_without_class_body(
    tmp_path: Path, filename: str, source: str
) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    method = next(s for s in code_map.symbols if s.name == "read")
    units = derive_source_evidence_units(code_map)
    selected = select_source_evidence_units(units, (method.declaration_range,))
    initializers = [u for u in selected if u.kind == "initializer"]
    assert len(initializers) == 1
    assert method.symbol_id in initializers[0].related_symbol_ids
    assert initializers[0].source_range.start_line < method.declaration_range.start_line
    assert not any(
        u.kind == "implementation" and u.owner_symbol_id != method.symbol_id
        for u in selected
    )


def test_test_usage_unit_includes_full_behavior_and_retains_source_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_sample.py").write_text(
        "def check_case():\n    result = execute_job()\n    assert result == 7\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    usage = next(
        u for u in derive_source_evidence_units(code_map) if u.kind == "test-usage"
    )
    assert usage.source_range.end_line == 3
    assert usage.source_sha256 == code_map.source_sha256
    assert usage.basis == "observed-syntax"
