from pathlib import Path

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
