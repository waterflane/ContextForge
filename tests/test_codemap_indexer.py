from pathlib import Path

import pytest

from contextforge.context import SelectedFileChangedError
from contextforge.intelligence import (
    IndexManifestReadError,
    acquire_index_lock,
    build_structural_index,
    initialize_index,
    load_file_code_map,
    load_manifest,
)
from contextforge.repositories import scan_repository


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def test_structural_index_round_trip_and_unchanged_reuse(tmp_path: Path) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "src/app.py", "def run():\n    missing()\n")
    _write(tmp_path, "README.md", "# Project\n")
    snapshot = scan_repository(tmp_path)

    with acquire_index_lock(tmp_path, "first") as lock:
        first = build_structural_index(snapshot, lock)
    with acquire_index_lock(tmp_path, "second") as lock:
        second = build_structural_index(snapshot, lock)

    assert first.extracted_paths == (
        ".contextforge/config.toml",
        "README.md",
        "src/app.py",
    )
    assert first.reused_paths == ()
    assert second.manifest == first.manifest
    assert second.extracted_paths == ()
    assert second.reused_paths == first.extracted_paths
    assert second.generation_path == first.generation_path
    loaded = load_file_code_map(
        tmp_path, "src/app.py", manifest=load_manifest(tmp_path)
    )
    expected = next(item for item in first.code_maps if item.path == "src/app.py")
    assert loaded == expected
    assert (first.generation_path / "symbols.jsonl").read_bytes().endswith(b"\n")
    assert (first.generation_path / "relationships.jsonl").read_bytes().endswith(b"\n")


def test_changed_source_invalidates_only_its_extraction_input(tmp_path: Path) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "a.py", "def a():\n    return 1\n")
    _write(tmp_path, "b.py", "def b():\n    return 2\n")
    first_snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "first") as lock:
        first = build_structural_index(first_snapshot, lock)

    _write(tmp_path, "b.py", "def b():\n    return 3\n")
    changed_snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "changed") as lock:
        changed = build_structural_index(changed_snapshot, lock)

    assert changed.manifest.generation_id != first.manifest.generation_id
    assert changed.extracted_paths == ("b.py",)
    assert changed.reused_paths == (".contextforge/config.toml", "a.py")
    assert load_file_code_map(tmp_path, "b.py").source_sha256 != (
        next(item for item in first.code_maps if item.path == "b.py").source_sha256
    )


def test_cached_record_does_not_bypass_stale_snapshot_detection(tmp_path: Path) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "app.py", "value = 1\n")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "first") as lock:
        build_structural_index(snapshot, lock)

    _write(tmp_path, "app.py", "value = 2\n")
    with (
        acquire_index_lock(tmp_path, "stale") as lock,
        pytest.raises(SelectedFileChangedError),
    ):
        build_structural_index(snapshot, lock)


def test_source_and_test_relationships_are_bidirectional_and_explicit(
    tmp_path: Path,
) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "src/pkg/__init__.py", "")
    _write(tmp_path, "src/pkg/service.py", "def serve():\n    return None\n")
    _write(
        tmp_path,
        "tests/test_service.py",
        "from pkg.service import serve\n\ndef test_serve():\n    serve()\n",
    )
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "relationships") as lock:
        result = build_structural_index(snapshot, lock)

    test_map = next(
        item for item in result.code_maps if item.path == "tests/test_service.py"
    )
    implementation = next(
        item for item in result.code_maps if item.path == "src/pkg/service.py"
    )
    test_kinds = {item.kind for item in test_map.relationships}
    implementation_kinds = {item.kind for item in implementation.relationships}

    assert {"tests", "test_reference"} <= test_kinds
    assert "tested_by" in implementation_kinds
    reference = next(
        item for item in test_map.relationships if item.kind == "test_reference"
    )
    assert reference.target.resolution == "internal"
    assert reference.target.file_path == "src/pkg/service.py"
    assert reference.detection_method == "python_resolved_test_call"


def test_test_path_convention_is_best_effort_and_records_its_basis(
    tmp_path: Path,
) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    _write(tmp_path, "tests/service_test.py", "def test_placeholder():\n    pass\n")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "convention") as lock:
        result = build_structural_index(snapshot, lock)

    test_map = next(
        item for item in result.code_maps if item.path == "tests/service_test.py"
    )
    relationship = next(item for item in test_map.relationships if item.kind == "tests")

    assert relationship.target.file_path == "service.py"
    assert relationship.detection_method == "python_test_path_convention"


def test_record_tampering_is_rejected_on_round_trip(tmp_path: Path) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "app.py", "pass\n")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "build") as lock:
        result = build_structural_index(snapshot, lock)
    state = next(item for item in result.manifest.files if item.path == "app.py")
    assert state.record_location is not None
    record = result.generation_path.joinpath(*state.record_location.split("/"))
    record.write_text("{}\n", encoding="utf-8")

    with pytest.raises(IndexManifestReadError, match="digest"):
        load_file_code_map(tmp_path, "app.py", manifest=result.manifest)
