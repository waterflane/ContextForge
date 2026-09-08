from pathlib import Path

import pytest

from contextforge.intelligence import (
    acquire_index_lock,
    build_structural_index,
    extract_code_map,
    extractors,
    initialize_index,
)
from contextforge.intelligence.codemap import FileCodeMap, SymbolKind
from contextforge.repositories import ProjectFile, ProjectSnapshot, scan_repository


@pytest.mark.parametrize(
    ("filename", "source", "owner", "method"),
    [
        ("a.ts", "const api = { run() {} };", "api", "run"),
        ("a.ts", "type API = { run(): void };", "API", "run"),
        ("A.java", "enum A { ONE; void run() {} }", "A", "run"),
        ("a.rb", "module A\n def run; end\nend\n", "A", "run"),
        ("a.ts", "class A { run = async () => 1; }", "A", "run"),
        ("a.rs", "struct Box<T>{x:T} impl<T> Box<T> {fn run(&self) {}}", "Box", "run"),
    ],
)
def test_review_method_owners(
    tmp_path: Path,
    filename: str,
    source: str,
    owner: str,
    method: str,
) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    assert code_map.parse_status == "parsed", code_map.diagnostics
    parent = next(s for s in code_map.symbols if s.name == owner)
    child = next(s for s in code_map.symbols if s.name == method)
    assert child.parent_symbol_id == parent.symbol_id
    assert child.symbol_id in parent.contained_methods
    assert child.kind == SymbolKind.METHOD


@pytest.mark.parametrize(
    ("filename", "source", "name", "body"),
    [
        ("a.ts", "const wrapped = (() => 1);", "wrapped", True),
        ("a.ts", "declare function run(): void;", "run", False),
        ("a.c", "int run(int x); int (*pointer)(int);", "run", False),
        ("a.c", "char * run(int x);", "run", False),
        ("a.cpp", "class A { public: void run(); };", "A.run", False),
        ("a.cs", "namespace Demo; class A { void Run() {} }", "Demo.A.Run", True),
    ],
)
def test_review_declarations(
    tmp_path: Path,
    filename: str,
    source: str,
    name: str,
    body: bool,
) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    assert code_map.parse_status == "parsed", code_map.diagnostics
    found = [s for s in code_map.symbols if s.qualified_name == name]
    assert len(found) == 1
    assert found[0].kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
    assert (found[0].body_range is not None) == body
    assert not any(
        s.name == "pointer" and s.kind == SymbolKind.FUNCTION for s in code_map.symbols
    )


def test_failed_extractor_is_isolated_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("bad.ts", "good.ts"):
        (tmp_path / name).write_text("function run() {}", encoding="utf-8")
    initialize_index(tmp_path)
    snapshot = scan_repository(tmp_path)
    original = extractors._EXTRACTORS["TypeScript"]

    def broken(
        snapshot: ProjectSnapshot, project_file: ProjectFile, **kwargs: int
    ) -> FileCodeMap:
        if project_file.path == "bad.ts":
            raise ValueError("invalid extractor output")
        return original(snapshot, project_file, **kwargs)

    monkeypatch.setitem(extractors._EXTRACTORS, "TypeScript", broken)
    with acquire_index_lock(tmp_path, "first") as lock:
        result = build_structural_index(snapshot, lock)
    assert result.code_maps[0].parse_status == "parse_error"
    assert result.code_maps[0].diagnostics[0].code == "extractor_error"
    assert result.code_maps[0].symbols == ()
    assert result.code_maps[1].symbols
    monkeypatch.setitem(extractors._EXTRACTORS, "TypeScript", original)
    with acquire_index_lock(tmp_path, "retry") as lock:
        retried = build_structural_index(snapshot, lock)
    assert retried.extracted_paths == ("bad.ts",)
    assert retried.reused_paths == ("good.ts",)
    assert all(item.record_status == "complete" for item in retried.manifest.files)


def test_anonymous_returned_object_does_not_make_callable_a_method_owner(
    tmp_path: Path,
) -> None:
    (tmp_path / "work.ts").write_text(
        "class API { prepareCall() { return { execute() {} }; } }",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    assert code_map.parse_status == "parsed"
    symbols = {s.name: s for s in code_map.symbols}
    assert symbols["prepareCall"].contained_methods == ()
    assert symbols["execute"].parent_symbol_id == symbols["prepareCall"].symbol_id
