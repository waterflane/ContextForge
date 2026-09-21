import json
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
    load_relationship_graph,
)
from contextforge.intelligence.graph import project_relationship_graph
from contextforge.intelligence.indexer import load_relationship_graph_projection
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
    assert not (first.generation_path / "symbols.jsonl").exists()
    assert not (first.generation_path / "relationships.jsonl").exists()
    assert load_relationship_graph(tmp_path) == load_relationship_graph(
        tmp_path, manifest=first.manifest
    )
    assert load_relationship_graph_projection(
        tmp_path, manifest=first.manifest
    ) == project_relationship_graph(load_relationship_graph(tmp_path))
    graph_shards = tuple((first.generation_path / "graph").glob("*.jsonl"))
    assert graph_shards
    assert all(path.stat().st_size <= 4 * 1024 * 1024 for path in graph_shards)
    retrieval_shards = tuple((first.generation_path / "retrieval").glob("*.jsonl"))
    assert retrieval_shards
    assert all(path.stat().st_size <= 4 * 1024 * 1024 for path in retrieval_shards)
    retrieval_header = json.loads(
        (first.generation_path / "retrieval-structural.json").read_text("utf-8")
    )
    assert retrieval_header["record_kind"] == "retrieval_posting_shards"
    assert sum(
        item["record_count"] for item in retrieval_header["document_shards"]
    ) == len(first.code_maps)


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
    assert changed.reused_paths == ("a.py",)
    assert load_file_code_map(tmp_path, "b.py").source_sha256 != (
        next(item for item in first.code_maps if item.path == "b.py").source_sha256
    )


def test_repeated_unresolved_occurrences_are_compact_not_graph_relationships(
    tmp_path: Path,
) -> None:
    initialize_index(tmp_path)
    calls = "".join("    missing()\n" for _ in range(20))
    _write(tmp_path, "app.py", f"def run():\n{calls}")
    snapshot = scan_repository(tmp_path)

    with acquire_index_lock(tmp_path, "compact") as lock:
        result = build_structural_index(snapshot, lock)

    code_map = result.code_maps[0]
    run = code_map.symbols[0]
    assert len(run.direct_calls) == 8
    assert code_map.occurrence_counts[0].identifier == "missing"
    assert code_map.occurrence_counts[0].total_count == 20
    assert code_map.occurrence_counts[0].retained_count == 8
    assert not [item for item in code_map.relationships if item.kind == "call"]


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
    assert reference.detection_method == "python_unambiguous_import_alias"


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
    assert relationship.detection_method == "file_policy_test_naming_convention"


@pytest.mark.parametrize(
    ("source_path", "source", "test_path", "test_source"),
    [
        (
            "src/service.js",
            "export function serve() {}\n",
            "tests/service.test.js",
            (
                'import { serve } from "../src/service.js"; '
                'test("serve", () => serve());\n'
            ),
        ),
        (
            "src/service.ts",
            "export function serve(): void {}\n",
            "__tests__/service.spec.ts",
            (
                'import { serve } from "../src/service.js"; '
                "function testServe() { serve(); }\n"
            ),
        ),
        (
            "src/main/kotlin/example/Service.kt",
            "package example\nclass Service\n",
            "src/test/kotlin/example/ServiceTest.kt",
            "package example\nclass ServiceTest\n",
        ),
        (
            "src/main/java/example/Service.java",
            (
                "package example; public class Service { public static void serve() "
                "{} }\n"
            ),
            "src/test/java/example/ServiceTest.java",
            (
                "package example; import example.Service; public class ServiceTest { "
                "void test() { Service.serve(); } }\n"
            ),
        ),
        (
            "src/Service.cs",
            (
                "namespace Example { public class Service { public static void Serve() "
                "{} } }\n"
            ),
            "spec/ServiceTests.cs",
            (
                "namespace Example { public class ServiceTests { void Test() { "
                "Service.Serve(); } } }\n"
            ),
        ),
        (
            "src/service.py",
            "def serve() -> None:\n    pass\n",
            "tests/test_service.py",
            "from src.service import serve\n\ndef test_serve() -> None:\n    serve()\n",
        ),
    ],
)
def test_file_policy_links_polyglot_and_python_tests_to_sources(
    tmp_path: Path,
    source_path: str,
    source: str,
    test_path: str,
    test_source: str,
) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, source_path, source)
    _write(tmp_path, test_path, test_source)
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "test-policy") as lock:
        result = build_structural_index(snapshot, lock)

    test_map = next(item for item in result.code_maps if item.path == test_path)
    relationship = next(item for item in test_map.relationships if item.kind == "tests")

    assert relationship.target.file_path == source_path
    assert relationship.target.resolution == "internal"


def test_file_policy_ignores_ambiguous_conventions(
    tmp_path: Path,
) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "alpha/service.py", "VALUE = 1\n")
    _write(tmp_path, "beta/service.py", "VALUE = 2\n")
    _write(tmp_path, "tests/test_service.py", "def test_placeholder():\n    pass\n")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "ambiguous-test") as lock:
        result = build_structural_index(snapshot, lock)

    test_map = next(
        item for item in result.code_maps if item.path == "tests/test_service.py"
    )

    assert not [item for item in test_map.relationships if item.kind == "tests"]


def test_file_policy_links_colocated_typescript_test_by_unique_name(
    tmp_path: Path,
) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "src/widget.ts", "export function render(): void {}\n")
    _write(tmp_path, "src/widget.test.ts", "function testRender() {}\n")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "colocated-test") as lock:
        result = build_structural_index(snapshot, lock)

    test_map = next(
        item for item in result.code_maps if item.path == "src/widget.test.ts"
    )
    relationship = next(item for item in test_map.relationships if item.kind == "tests")

    assert relationship.target.file_path == "src/widget.ts"
    assert relationship.detection_method == "file_policy_test_naming_convention"


def test_file_policy_follows_passive_typescript_barrels(tmp_path: Path) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "src/service.ts", "export function serve(): void {}\n")
    _write(
        tmp_path,
        "src/index.ts",
        'export { serve } from "./service.js";\n',
    )
    _write(
        tmp_path,
        "tests/index.test.ts",
        'import { serve } from "../src/index.js";\nfunction testIndex() { serve(); }\n',
    )
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "barrel-test") as lock:
        result = build_structural_index(snapshot, lock)

    test_map = next(
        item for item in result.code_maps if item.path == "tests/index.test.ts"
    )
    targets = {
        item.target.file_path for item in test_map.relationships if item.kind == "tests"
    }

    assert targets == {"src/index.ts", "src/service.ts"}


def test_incremental_update_replaces_renamed_source_test_edges(tmp_path: Path) -> None:
    initialize_index(tmp_path)
    _write(tmp_path, "src/service.py", "def serve() -> None:\n    pass\n")
    _write(tmp_path, "tests/test_service.py", "def test_service() -> None:\n    pass\n")
    with acquire_index_lock(tmp_path, "initial-test-edge") as lock:
        initial = build_structural_index(scan_repository(tmp_path), lock)
    assert any(
        item.kind == "tests" and item.target.file_path == "src/service.py"
        for item in next(
            map for map in initial.code_maps if map.path == "tests/test_service.py"
        ).relationships
    )

    (tmp_path / "src/service.py").unlink()
    (tmp_path / "tests/test_service.py").unlink()
    _write(tmp_path, "src/renamed.py", "def renamed() -> None:\n    pass\n")
    _write(tmp_path, "tests/test_renamed.py", "def test_renamed() -> None:\n    pass\n")
    with acquire_index_lock(tmp_path, "renamed-test-edge") as lock:
        updated = build_structural_index(scan_repository(tmp_path), lock)

    all_targets = {
        item.target.file_path
        for code_map in updated.code_maps
        for item in code_map.relationships
        if item.kind in {"tests", "tested_by"}
    }

    assert "src/service.py" not in all_targets
    assert "tests/test_service.py" not in all_targets
    assert {"src/renamed.py", "tests/test_renamed.py"} <= all_targets


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
