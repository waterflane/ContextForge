import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import contextforge.application as application_module
from contextforge.application import build_repository_index
from contextforge.intelligence import (
    build_relationship_graph,
    calculate_source_snapshot_digest,
    extract_code_maps,
    load_manifest,
    load_orientation_map,
    load_relationship_graph,
)
from contextforge.models import ModelProvider
from contextforge.repositories import scan_repository


def test_structural_generation_contains_deterministic_graph_and_orientation(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "from service import handle\n\nhandle()\n", encoding="utf-8"
    )
    (tmp_path / "service.py").write_text(
        "def handle() -> str:\n    return 'ok'\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import handle\n\n"
        "def test_handle():\n"
        "    assert handle() == 'ok'\n",
        encoding="utf-8",
    )

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    graph = load_relationship_graph(tmp_path, manifest=report.manifest)
    orientation = load_orientation_map(tmp_path, manifest=report.manifest)

    assert report.manifest.schema_version == 3
    assert report.manifest.generation_kind == "enriched"
    assert (
        report.manifest.build.previous_generation_id
        == report.structural.manifest.generation_id
    )
    assert {item.path for item in graph.file_metrics} == {
        "main.py",
        "service.py",
        "tests/test_service.py",
    }
    assert any(
        edge.kind == "import" and edge.provenance == "verified" for edge in graph.edges
    )
    assert any(
        edge.kind == "entrypoint-handler"
        and edge.provenance == "best-effort-structural"
        for edge in graph.edges
    )
    assert any(edge.kind == "source-test" for edge in graph.edges)
    assert tuple(item.path for item in orientation.files) == (
        "main.py",
        "service.py",
        "tests/test_service.py",
    )
    assert load_relationship_graph(tmp_path, manifest=report.manifest) == graph


def test_enrichment_failure_keeps_published_structural_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    async def fail_enrichment(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise RuntimeError("enrichment failed")

    monkeypatch.setattr(
        application_module, "build_semantic_card_index", fail_enrichment
    )
    with pytest.raises(RuntimeError, match="enrichment failed"):
        asyncio.run(
            build_repository_index(
                tmp_path,
                provider=cast(ModelProvider, object()),
                provider_configuration=None,
            )
        )

    active = load_manifest(tmp_path)
    assert active.generation_kind == "structural"
    assert active.artifacts.relationship_graph is not None
    assert (
        load_relationship_graph(tmp_path, manifest=active).file_metrics[0].path
        == "app.py"
    )


@pytest.mark.parametrize(
    ("target_path", "target_source", "source_path", "source"),
    [
        (
            "helper.h",
            "int serve(void);\n",
            "main.c",
            '#include "helper.h"\n'
            "int run(void) { int (*selected)(void) = serve; return serve(); }\n",
        ),
        (
            "helper.hpp",
            "int serve();\n",
            "main.cpp",
            '#include "helper.hpp"\n'
            "int run() { auto selected = serve; return serve(); }\n",
        ),
        (
            "Demo/Helper.cs",
            "namespace Demo { public class Helper { "
            "public static void Serve() {} } }\n",
            "Demo/App.cs",
            "using Demo.Helper; namespace Demo { class App { "
            "void Run() { var selected = Helper.Serve; Helper.Serve(); } } }\n",
        ),
        (
            "pkg/helper.go",
            "package helper\nfunc Serve() {}\n",
            "main.go",
            'package main\nimport "pkg/helper"\n'
            "func run() { selected := helper.Serve; helper.Serve(); _ = selected }\n",
        ),
        (
            "pkg/Helper.java",
            "package pkg; public class Helper { public static void serve() {} }\n",
            "pkg/App.java",
            "package pkg; import pkg.Helper; class App { "
            "void run() { Object selected = Helper::serve; Helper.serve(); } }\n",
        ),
        (
            "lib.js",
            "export function serve() {}\n",
            "app.js",
            'import { serve } from "./lib.js";\n'
            "function run() { serve(); const selected = serve; }\n",
        ),
        (
            "Demo/Helper.php",
            "<?php namespace Demo; class Helper { "
            "public static function serve() {} }\n",
            "app.php",
            "<?php use Demo\\Helper; "
            "function run() { $selected = Helper::class; Helper::serve(); }\n",
        ),
        (
            "helper.rb",
            "class Helper; def self.serve; end; end\n",
            "app.rb",
            "require_relative './helper'\n"
            "def run; selected = Helper; Helper.serve(); end\n",
        ),
        (
            "helper.rs",
            "pub fn serve() {}\n",
            "main.rs",
            "mod helper; fn run() { let selected = helper::serve; helper::serve(); }\n",
        ),
        (
            "lib.ts",
            "export function serve(): void {}\n",
            "app.ts",
            'import { serve } from "./lib";\n'
            "function run(): void { serve(); const selected = serve; }\n",
        ),
    ],
)
def test_polyglot_imports_resolve_only_to_snapshot_files(
    tmp_path: Path,
    target_path: str,
    target_source: str,
    source_path: str,
    source: str,
) -> None:
    for path, content in ((target_path, target_source), (source_path, source)):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    snapshot = scan_repository(tmp_path)
    maps = extract_code_maps(snapshot)
    source_map = next(item for item in maps if item.path == source_path)
    graph = build_relationship_graph(maps, calculate_source_snapshot_digest(snapshot))

    assert source_map.imports
    assert source_map.imports[0].resolution == "internal"
    assert source_map.imports[0].target_file_path == target_path
    import_edge = next(
        edge
        for edge in graph.edges
        if edge.kind == "import" and edge.source_file_path == source_path
    )
    expected_provenance = (
        "best-effort-structural"
        if source_path in {"Demo/App.cs", "main.go", "pkg/App.java", "app.php"}
        else "verified"
    )
    assert import_edge.provenance == expected_provenance
    assert any(
        edge.kind == "call" and edge.source_file_path == source_path
        for edge in graph.edges
    )
    assert any(
        edge.kind == "reference" and edge.source_file_path == source_path
        for edge in graph.edges
    )


def test_ambiguous_polyglot_package_import_stays_unresolved(tmp_path: Path) -> None:
    for path in ("alpha/lib.js", "beta/lib.js"):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("export function serve() {}\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        'import { serve } from "lib";\nfunction run() { serve(); }\n',
        encoding="utf-8",
    )

    source_map = next(
        item
        for item in extract_code_maps(scan_repository(tmp_path))
        if item.path == "app.js"
    )

    assert source_map.imports[0].resolution == "unresolved"
    assert source_map.imports[0].target_file_path is None
    call = source_map.symbols[0].direct_calls[0]
    assert call.resolution == "unresolved"
    assert call.target_file_path is None


@pytest.mark.parametrize(
    ("emitted_suffix", "source_suffix"),
    [
        (".js", ".ts"),
        (".jsx", ".tsx"),
        (".mjs", ".mts"),
        (".cjs", ".cts"),
    ],
)
def test_typescript_relative_emitted_suffixes_resolve_to_declared_source_suffixes(
    tmp_path: Path,
    emitted_suffix: str,
    source_suffix: str,
) -> None:
    target_path = f"ui/view{source_suffix}"
    (tmp_path / "ui").mkdir()
    (tmp_path / target_path).write_text(
        "export function serve(): void {}\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.ts").write_text(
        f'import {{ serve }} from "../ui/view{emitted_suffix}";\n'
        "function run(): void { serve(); const selected = serve; }\n",
        encoding="utf-8",
    )

    snapshot = scan_repository(tmp_path)
    maps = extract_code_maps(snapshot)
    source_map = next(item for item in maps if item.path == "src/app.ts")
    graph = build_relationship_graph(maps, calculate_source_snapshot_digest(snapshot))

    assert source_map.imports[0].resolution == "internal"
    assert source_map.imports[0].target_file_path == target_path
    edges = [item for item in graph.edges if item.source_file_path == "src/app.ts"]
    resolved_edges = [
        item for item in edges if item.kind in {"import", "call", "reference"}
    ]
    assert {item.kind for item in resolved_edges} == {"import", "call", "reference"}
    assert {item.detection_method for item in resolved_edges} == {
        "polyglot_typescript_emitted_suffix_resolution"
    }
    assert {item.provenance for item in resolved_edges} == {"best-effort-structural"}


def test_typescript_existing_javascript_file_beats_emitted_suffix_substitution(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.js").write_text(
        "export function serve() {}\n", encoding="utf-8"
    )
    (tmp_path / "config.ts").write_text(
        "export function serve(): void {}\n", encoding="utf-8"
    )
    (tmp_path / "app.ts").write_text(
        'import { serve } from "./config.js";\nfunction run() { serve(); }\n',
        encoding="utf-8",
    )

    snapshot = scan_repository(tmp_path)
    maps = extract_code_maps(snapshot)
    source_map = next(
        item
        for item in maps
        if item.path == "app.ts"
    )
    graph = build_relationship_graph(maps, calculate_source_snapshot_digest(snapshot))

    assert source_map.imports[0].resolution == "internal"
    assert source_map.imports[0].target_file_path == "config.js"
    import_edge = next(
        item
        for item in graph.edges
        if item.kind == "import" and item.source_file_path == "app.ts"
    )
    assert import_edge.detection_method == "polyglot_snapshot_path_resolution"
    assert import_edge.provenance == "verified"


def test_ambiguous_typescript_emitted_suffix_substitution_stays_unresolved(
    tmp_path: Path,
) -> None:
    (tmp_path / "view.ts").write_text(
        "export function serve(): void {}\n", encoding="utf-8"
    )
    (tmp_path / "view.tsx").write_text(
        "export function serve(): void {}\n", encoding="utf-8"
    )
    (tmp_path / "app.ts").write_text(
        'import { serve } from "./view.js";\nfunction run() { serve(); }\n',
        encoding="utf-8",
    )

    source_map = next(
        item
        for item in extract_code_maps(scan_repository(tmp_path))
        if item.path == "app.ts"
    )

    assert source_map.imports[0].resolution == "unresolved"
    assert source_map.imports[0].target_file_path is None


def test_typescript_package_import_is_external_even_with_matching_basename(
    tmp_path: Path,
) -> None:
    (tmp_path / "react.ts").write_text(
        "export function useState(): void {}\n", encoding="utf-8"
    )
    (tmp_path / "app.ts").write_text(
        'import { useState } from "react";\nfunction run() { useState(); }\n',
        encoding="utf-8",
    )

    source_map = next(
        item
        for item in extract_code_maps(scan_repository(tmp_path))
        if item.path == "app.ts"
    )

    assert source_map.imports[0].resolution == "external"
    assert source_map.imports[0].target_file_path is None


def test_dsh_style_emitted_imports_create_import_call_and_reference_edges(
    tmp_path: Path,
) -> None:
    files = {
        "src/index-lifecycle.ts": "export function synchronizeIndex(): void {}\n",
        "src/worker-manager.ts": "export function startWorker(): void {}\n",
        "src/worker.ts": (
            'import { synchronizeIndex } from "./index-lifecycle.js";\n'
            'import { startWorker } from "./worker-manager.js";\n'
            "export function run(): void {\n"
            "  synchronizeIndex();\n"
            "  startWorker();\n"
            "  const selected = synchronizeIndex;\n"
            "}\n"
        ),
    }
    for path, source in files.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")

    snapshot = scan_repository(tmp_path)
    graph = build_relationship_graph(
        extract_code_maps(snapshot), calculate_source_snapshot_digest(snapshot)
    )
    edges = [item for item in graph.edges if item.source_file_path == "src/worker.ts"]
    resolved_edges = [
        item for item in edges if item.kind in {"import", "call", "reference"}
    ]

    assert {item.kind for item in resolved_edges} == {"import", "call", "reference"}
    assert {item.provenance for item in resolved_edges} == {"best-effort-structural"}


def test_typescript_emitted_import_pagerank_is_hash_seed_independent() -> None:
    script = "\n".join(
        (
            "import json, tempfile",
            "from pathlib import Path",
            "from contextforge.intelligence import (",
            "    build_relationship_graph, calculate_source_snapshot_digest,",
            "    extract_code_maps,",
            ")",
            "from contextforge.repositories import scan_repository",
            "with tempfile.TemporaryDirectory() as directory:",
            "    root = Path(directory)",
            "    (root / 'one.ts').write_text(",
            "        'export function one(): void {}\\n', encoding='utf-8'",
            "    )",
            "    (root / 'two.ts').write_text(",
            "        'export function two(): void {}\\n', encoding='utf-8'",
            "    )",
            "    source = (",
            "        'import { one } from \\\"./one.js\\\";\\n'",
            "        'import { two } from \\\"./two.js\\\";\\n'",
            "        'function run() { one(); two(); }\\n'",
            "    )",
            "    (root / 'app.ts').write_text(source, encoding='utf-8')",
            "    snapshot = scan_repository(root)",
            "    maps = extract_code_maps(snapshot)",
            "    graph = build_relationship_graph(",
            "        maps, calculate_source_snapshot_digest(snapshot)",
            "    )",
            "    print(json.dumps(",
            "        [(item.path, item.pagerank) for item in graph.file_metrics]",
            "    ))",
        )
    )
    outputs = []
    for seed in ("1", "42", "random"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.append(json.loads(result.stdout))

    assert outputs[0] == outputs[1] == outputs[2]


def test_conventional_source_test_edges_are_best_effort(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_foo.py").write_text(
        "def test_value():\n    pass\n", encoding="utf-8"
    )

    snapshot = scan_repository(tmp_path)
    maps = extract_code_maps(snapshot)
    graph = build_relationship_graph(maps, calculate_source_snapshot_digest(snapshot))
    edges = [item for item in graph.edges if item.kind == "source-test"]

    assert edges
    assert {item.provenance for item in edges} == {"best-effort-structural"}


def test_config_consumers_require_matching_key_digests(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        'import os\nAPI_URL = os.getenv("API_URL")\n', encoding="utf-8"
    )
    (tmp_path / "settings.toml").write_text(
        'api_url_env = "API_URL"\n', encoding="utf-8"
    )
    (tmp_path / "unrelated.toml").write_text(
        'other_env = "OTHER_KEY"\n', encoding="utf-8"
    )

    snapshot = scan_repository(tmp_path)
    maps = extract_code_maps(snapshot)
    graph = build_relationship_graph(maps, calculate_source_snapshot_digest(snapshot))
    edges = [item for item in graph.edges if item.kind == "config-consumer"]

    assert len(edges) == 1
    assert edges[0].source_file_path == "settings.toml"
