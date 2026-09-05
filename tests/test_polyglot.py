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
    (tmp_path / filename).write_text(source + "\n???\n", encoding="utf-8")
    damaged = scan_repository(tmp_path)
    partial = extract_code_map(damaged, damaged.files[0])
    assert partial.parse_status == "partial"
    assert partial.diagnostics
    # Recovery may wrap the declaration immediately preceding the bad tokens in
    # ERROR. Such nodes must be discarded too, not promoted as verified siblings.
    assert expected & {symbol.name for symbol in partial.symbols}


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
        "\n" * 147 + "export function preparationProgressStage() { return 'index'; }\n"
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
        "class Service { constructor() {} private async stop() {} "
        "public start() {} }\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)

    code_map = extract_code_map(snapshot, snapshot.files[0])
    symbols = {item.name: item for item in code_map.symbols}

    assert symbols["loadData"].kind == "async_function"
    assert symbols["loadData"].is_async is True
    assert symbols["loadData"].visibility == "explicit_export"
    assert symbols["constructor"].kind == "constructor"
    assert symbols["stop"].visibility == "private"
    assert symbols["stop"].is_async is True
    assert symbols["start"].parent_symbol_id == symbols["Service"].symbol_id
    assert symbols["Service"].visibility == "unknown"
    assert symbols["Service"].is_async is False


def test_typescript_metadata_uses_modifier_nodes_not_header_text(
    tmp_path: Path,
) -> None:
    long_comment = "x" * 200
    (tmp_path / "metadata.ts").write_text(
        "export function run(/* private */ value: number) { return value; }\n"
        f"class Service {{ public /* {long_comment} */ async load() {{}} }}\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)

    symbols = {
        item.name: item
        for item in extract_code_map(snapshot, snapshot.files[0]).symbols
    }

    assert symbols["run"].visibility == "explicit_export"
    assert symbols["load"].visibility == "public"
    assert symbols["load"].is_async is True


def test_public_modifier_is_not_conflated_with_explicit_export(tmp_path: Path) -> None:
    (tmp_path / "Service.java").write_text(
        "public class Service { public Service() {} public void run() {} }\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)

    code_map = extract_code_map(snapshot, snapshot.files[0])
    service = next(item for item in code_map.symbols if item.kind == "class")
    run = next(item for item in code_map.symbols if item.name == "run")

    assert service.visibility == "public"
    assert run.visibility == "public"


@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_c_declarators_do_not_take_names_from_return_types_or_bodies(
    tmp_path: Path, suffix: str
) -> None:
    (tmp_path / f"declarations.{suffix}").write_text(
        "struct Result { int value; };\n"
        "struct { int anonymousValue; } instance;\n"
        "struct Result create(void) { struct Result result; return result; }\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    symbols = extract_code_map(snapshot, snapshot.files[0]).symbols
    assert len([symbol for symbol in symbols if symbol.kind == "struct"]) == 1
    assert any(
        symbol.name == "create" and symbol.kind == "function" for symbol in symbols
    )
    assert not any(
        symbol.name == "anonymousValue" and symbol.kind == "struct"
        for symbol in symbols
    )


@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_c_prototypes_preserve_declaration_types_without_function_pointer_duplicates(
    tmp_path: Path, suffix: str
) -> None:
    (tmp_path / f"prototypes.{suffix}").write_text(
        "char *\nbuild_name(int id);\n"
        "int first(void), second(int value);\n"
        "int (*callback)(int);\n"
        "int (*factory(void))(int);\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)

    symbols = extract_code_map(snapshot, snapshot.files[0]).symbols
    functions = [symbol for symbol in symbols if symbol.kind == "function"]
    by_name = {symbol.name: symbol for symbol in functions}

    assert set(by_name) == {"build_name", "first", "second", "factory"}
    assert by_name["build_name"].declaration_range.start_line == 1
    assert by_name["build_name"].signature == "char * build_name(int id);"
    assert len([symbol for symbol in functions if symbol.name == "factory"]) == 1
    assert "callback" not in by_name


@pytest.mark.parametrize("suffix", ["js", "ts", "tsx"])
def test_named_callable_bindings_and_variables(tmp_path: Path, suffix: str) -> None:
    (tmp_path / f"bindings.{suffix}").write_text(
        "export const loadData = async () => { return 1; };\r\n"
        "const compute = function internal() { return 2; };\r\n"
        "let изменяемое = 3; const LIMIT = 4;\r\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    symbols = {symbol.name: symbol for symbol in code_map.symbols}
    assert symbols["loadData"].kind == "async_function"
    assert symbols["loadData"].is_async
    assert symbols["loadData"].body_range is not None
    assert symbols["compute"].kind == "function"
    assert "internal" not in symbols
    assert symbols["изменяемое"].kind == "variable"
    assert symbols["LIMIT"].kind == "constant"


@pytest.mark.parametrize(
    "filename,source",
    [
        ("sample.go", "package x\nconst LIMIT=1\nvar value=2\n"),
        ("sample.rs", "const LIMIT: i32=1; static value:i32=2;"),
        ("sample.c", "const int LIMIT=1; int value;"),
        ("sample.cpp", "const int LIMIT=1; int value;"),
        ("Sample.cs", "class Box { const int LIMIT=1; int value; }"),
        ("Sample.java", "class Box { static final int LIMIT=1; int value; }"),
        ("sample.php", "<?php class Box { const LIMIT=1; public $value=2; }"),
        ("sample.rb", "LIMIT=1\nvalue=2\n"),
    ],
)
def test_polyglot_constants_and_variables(
    tmp_path: Path, filename: str, source: str
) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    symbols = {
        symbol.name: symbol
        for symbol in extract_code_map(snapshot, snapshot.files[0]).symbols
    }
    assert symbols["LIMIT"].kind == "constant"
    variable = symbols.get("value", symbols.get("$value"))
    assert variable is not None and variable.kind == "variable"


@pytest.mark.parametrize(
    "filename,source",
    [
        ("sample.go", "package x\nfunc (b *Box) run() {}\ntype Box struct{}\n"),
        ("sample.rs", "impl Box { fn run(&self) {} }\nstruct Box;"),
        ("sample.cpp", "class Box { public: void run() {} };"),
    ],
)
def test_receiver_and_impl_methods_belong_to_types(
    tmp_path: Path, filename: str, source: str
) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    symbols = {
        symbol.name: symbol
        for symbol in extract_code_map(snapshot, snapshot.files[0]).symbols
    }
    assert symbols["run"].kind == "method"
    assert symbols["run"].qualified_name == "Box.run"
    assert symbols["run"].parent_symbol_id == symbols["Box"].symbol_id
