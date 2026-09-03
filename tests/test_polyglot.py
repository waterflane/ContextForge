from pathlib import Path

import pytest

from contextforge.intelligence import POLYGLOT_ANALYZER, extract_code_map
from contextforge.repositories import scan_repository


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    [
        (
            "sample.js",
            "export function run() {}\nclass Box { method() {} }\n",
            {"run", "Box", "method"},
        ),
        (
            "sample.ts",
            "interface Worker { run(): void }\nexport function start() {}\n",
            {"Worker", "run", "start"},
        ),
        (
            "Sample.java",
            "class Service { Service() {} void run() {} }\n",
            {"Service", "run"},
        ),
        (
            "Sample.cs",
            "namespace Demo { public class Service { "
            "public Service() {} public void Run() {} } }\n",
            {"Demo", "Service", "Run"},
        ),
        (
            "sample.go",
            "package demo\nfunc Run() {}\ntype Service struct{}\n",
            {"Run", "Service"},
        ),
        (
            "sample.rs",
            "pub struct Service;\npub fn run() {}\ntrait Work { fn work(&self); }\n",
            {"Service", "run", "Work", "work"},
        ),
        (
            "sample.c",
            "struct Service { int value; };\nint run(void) { return 1; }\n",
            {"Service", "run"},
        ),
        (
            "sample.cpp",
            "namespace Demo { class Service { public: void run() {} }; }\n"
            "int execute() { return 1; }\n",
            {"Demo", "Service", "run", "execute"},
        ),
        (
            "sample.php",
            "<?php class Service { public function run() {} } function execute() {}\n",
            {"Service", "run", "execute"},
        ),
        (
            "sample.rb",
            "module Demo\n class Service\n  def run; end\n end\nend\n",
            {"Demo", "Service", "run"},
        ),
    ],
)
def test_polyglot_extracts_verified_declarations(
    tmp_path: Path, filename: str, source: str, expected: set[str]
) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    code_map = extract_code_map(snapshot, snapshot.files[0])

    assert code_map.analyzer == POLYGLOT_ANALYZER
    assert code_map.parse_status == "parsed"
    assert expected <= {symbol.name for symbol in code_map.symbols}
    assert all(symbol.declaration_range.start_line >= 1 for symbol in code_map.symbols)


def test_polyglot_keeps_valid_siblings_around_parse_errors(tmp_path: Path) -> None:
    (tmp_path / "broken.ts").write_text(
        "export function intact() { return 1; }\nconst broken = ;\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)

    code_map = extract_code_map(snapshot, snapshot.files[0])

    assert code_map.parse_status == "partial"
    assert "intact" in {symbol.name for symbol in code_map.symbols}
    assert code_map.diagnostics


def test_typescript_symbol_range_matches_source_line(tmp_path: Path) -> None:
    source = (
        "\n" * 147
        + "export function preparationProgressStage() { return 'index'; }\n"
    )
    (tmp_path / "progress.ts").write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    code_map = extract_code_map(snapshot, snapshot.files[0])
    symbol = next(
        item for item in code_map.symbols if item.name == "preparationProgressStage"
    )

    assert symbol.declaration_range.start_line == 148


def test_tsx_async_and_visibility_metadata_are_verified(tmp_path: Path) -> None:
    (tmp_path / "component.tsx").write_text(
        "export async function loadData() { return 1; }\n"
        "class Service { private stop() {} public start() {} }\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)

    code_map = extract_code_map(snapshot, snapshot.files[0])
    symbols = {item.name: item for item in code_map.symbols}

    assert symbols["loadData"].kind == "async_function"
    assert symbols["loadData"].is_async is True
    assert symbols["loadData"].visibility == "explicit_export"
    assert symbols["stop"].visibility == "private"
    assert symbols["start"].parent_symbol_id == symbols["Service"].symbol_id
