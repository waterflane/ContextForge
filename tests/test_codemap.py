import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.context import (
    SelectedFileChangedError,
    SelectedFileDecodeError,
    SelectedFileNotInSnapshotError,
    SelectedFileNotRegularError,
    SelectedFileOutsideRootError,
)
from contextforge.intelligence import (
    CallReference,
    FileCodeMap,
    ImportRecord,
    ParserDiagnostic,
    RelationshipTarget,
    SourceRange,
    SymbolKind,
    SymbolRecord,
    deserialize_code_map,
    extract_code_map,
    extract_code_maps,
    serialize_code_map,
)
from contextforge.repositories import (
    ProjectFile,
    ProjectSnapshot,
    ScanSummary,
    scan_repository,
)


def _write(root: Path, path: str, content: str | bytes) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        destination.write_bytes(content)
    else:
        destination.write_text(content, encoding="utf-8", newline="")


def _map(root: Path, path: str = "module.py") -> FileCodeMap:
    snapshot = scan_repository(root)
    project_file = next(item for item in snapshot.files if item.path == path)
    return extract_code_map(snapshot, project_file)


def test_empty_file_has_only_verified_file_facts(tmp_path: Path) -> None:
    _write(tmp_path, "empty.py", "")

    code_map = _map(tmp_path, "empty.py")

    assert code_map.parse_status == "parsed"
    assert code_map.line_count == 0
    assert code_map.symbols == ()
    assert code_map.imports == ()
    assert code_map.relationships == ()
    assert code_map.source_sha256 == hashlib.sha256(b"").hexdigest()


def test_python_symbols_signatures_annotations_and_nested_order(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/sample.py",
        '''"""Module docs."""
CONSTANT = 42

@outer.decorator("x")
def café(a: int, /, b: str = "x", *args: bytes, flag: bool, **kwargs: object) -> str:
    """Function docs."""
    def nested(value: int) -> None:
        helper(value)
    nested(a)
    raise ValueError("bad")

async def fetch(item: str) -> bytes:
    return b""

class Base: pass

class Service(Base):
    @classmethod
    async def run(cls, value: int) -> None:
        os.getenv("SERVICE_TOKEN")
        os.environ["SERVICE_MODE"]
''',
    )

    code_map = _map(tmp_path, "pkg/sample.py")
    names = tuple(symbol.name for symbol in code_map.symbols)

    assert code_map.module_docstring == "Module docs."
    assert code_map.top_level_constants == ("CONSTANT",)
    assert names == ("CONSTANT", "café", "nested", "fetch", "Base", "Service", "run")
    function = code_map.symbols[1]
    nested = code_map.symbols[2]
    fetch = code_map.symbols[3]
    service = code_map.symbols[5]
    method = code_map.symbols[6]
    assert function.qualified_name == "pkg.sample.café"
    assert nested.qualified_name == "pkg.sample.café.nested"
    assert nested.parent_symbol_id == function.symbol_id
    assert function.signature is not None
    assert function.signature.startswith("def café(")
    assert function.signature.endswith(") -> str:")
    assert tuple(parameter.kind for parameter in function.parameters) == (
        "positional_only",
        "positional_or_keyword",
        "var_positional",
        "keyword_only",
        "var_keyword",
    )
    assert function.decorators[0].expression == 'outer.decorator("x")'
    assert function.raised_exceptions == ("ValueError",)
    assert fetch.kind == SymbolKind.ASYNC_FUNCTION
    assert fetch.is_async is True
    assert method.kind == SymbolKind.METHOD
    assert method.is_async is True
    assert service.base_classes == ("Base",)
    assert service.contained_methods == (method.symbol_id,)
    assert method.configuration_keys == ("SERVICE_MODE", "SERVICE_TOKEN")
    assert function.declaration_range.start_line == 5
    assert function.declaration_range.start_column == 0


def test_import_aliases_relative_imports_all_and_calls(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/impl.py",
        "def helper(value: int) -> None:\n    return None\n",
    )
    _write(
        tmp_path,
        "pkg/app.py",
        """from .impl import helper as run_helper
import external.library as library
__all__ = ["public"] + ("run_helper",)

def public() -> None:
    run_helper(1)
    library.call()
    dynamic()
""",
    )

    maps = extract_code_maps(scan_repository(tmp_path))
    app = next(item for item in maps if item.path == "pkg/app.py")
    helper_import = next(item for item in app.imports if item.alias == "run_helper")
    external_import = next(item for item in app.imports if item.alias == "library")
    function = next(item for item in app.symbols if item.name == "public")
    calls = {call.observed_name: call for call in function.direct_calls}

    assert helper_import.level == 1
    assert helper_import.module == "impl"
    assert helper_import.imported_name == "helper"
    assert helper_import.resolution == "internal"
    assert helper_import.target_file_path == "pkg/impl.py"
    assert external_import.resolution == "external"
    assert tuple((item.kind, item.name) for item in app.exports) == (
        ("explicit", "public"),
        ("explicit", "run_helper"),
        ("conventional", "public"),
    )
    assert calls["run_helper"].resolution == "internal"
    assert calls["run_helper"].target_file_path == "pkg/impl.py"
    assert calls["library.call"].resolution == "unresolved"
    assert calls["dynamic"].resolution == "unresolved"


def test_syntax_error_is_diagnostic_without_fabricated_symbols(tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", "def broken(:\n    pass\n")

    code_map = _map(tmp_path, "broken.py")

    assert code_map.parse_status == "parse_error"
    assert code_map.symbols == ()
    assert code_map.relationships == ()
    assert code_map.diagnostics[0].code == "python_syntax_error"
    assert code_map.diagnostics[0].range is not None
    assert str(tmp_path) not in code_map.diagnostics[0].message


def test_unsupported_language_is_file_level_only_and_deterministic(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.js", "function invented() { return 1; }\n")

    first = _map(tmp_path, "app.js")
    second = _map(tmp_path, "app.js")

    assert first == second
    assert first.parse_status == "unsupported"
    assert first.symbols == ()
    assert first.relationships == ()
    assert first.diagnostics[0].code == "unsupported_language"


def test_serialization_is_canonical_strict_and_round_trips(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "def f():\n    unresolved()\n")
    code_map = _map(tmp_path)

    first = serialize_code_map(code_map)
    second = serialize_code_map(_map(tmp_path))

    assert first == second
    assert first.endswith(b"\n")
    assert deserialize_code_map(first) == code_map
    with pytest.raises(ValueError, match="duplicate"):
        deserialize_code_map('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValidationError):
        FileCodeMap(**{**code_map.model_dump(), "unknown": True})


def test_changed_source_after_snapshot_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "value = 1\n")
    snapshot = scan_repository(tmp_path)
    _write(tmp_path, "module.py", "value = 2\n")

    with pytest.raises(SelectedFileChangedError):
        extract_code_map(snapshot, snapshot.files[0])


def test_only_snapshot_owned_entries_are_authorized(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "pass\n")
    snapshot = scan_repository(tmp_path)

    with pytest.raises(SelectedFileNotInSnapshotError):
        extract_code_map(snapshot, snapshot.files[0].model_copy())


def test_malformed_utf8_after_scanner_sample_fails_strictly(tmp_path: Path) -> None:
    _write(tmp_path, "late.py", b"#" * 8192 + b"\xff")
    snapshot = scan_repository(tmp_path)

    with pytest.raises(SelectedFileDecodeError):
        extract_code_map(snapshot, snapshot.files[0])


def test_link_substitution_after_snapshot_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    external = tmp_path / "external.py"
    source.write_text("pass\n", encoding="utf-8")
    external.write_text("pass\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    source.unlink()
    try:
        source.symlink_to(external)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    project_file = next(item for item in snapshot.files if item.path == "module.py")
    with pytest.raises(SelectedFileNotRegularError):
        extract_code_map(snapshot, project_file)


def test_source_is_never_executed_and_imports_have_no_side_effects(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "marker.txt"
    _write(
        tmp_path,
        "danger.py",
        f'Path({str(marker)!r}).write_text("executed")\n',
    )
    _write(
        tmp_path,
        "app.py",
        "import danger\nraise AssertionError('repository code executed')\n",
    )

    extract_code_maps(scan_repository(tmp_path))

    assert not marker.exists()


def test_ambiguous_snapshot_module_and_relative_missing_stay_unresolved(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/pkg/shared.py", "def value():\n    return 1\n")
    _write(tmp_path, "lib/pkg/shared.py", "def value():\n    return 2\n")
    _write(
        tmp_path,
        "pkg/app.py",
        "from pkg.shared import value\nfrom .missing import absent\n",
    )

    maps = extract_code_maps(scan_repository(tmp_path))
    app = next(item for item in maps if item.path == "pkg/app.py")

    assert tuple(item.resolution for item in app.imports) == (
        "unresolved",
        "unresolved",
    )


def test_forged_outside_path_never_reaches_the_filesystem(tmp_path: Path) -> None:
    project_file = ProjectFile.model_construct(
        path="../outside.py",
        size_bytes=0,
        language="Python",
        sha256=hashlib.sha256(b"").hexdigest(),
        is_text=True,
    )
    snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(project_file,),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=1, ignored_count=0, total_size_bytes=0),
    )

    with pytest.raises(SelectedFileOutsideRootError, match="portable"):
        extract_code_map(snapshot, project_file)


def test_codemap_models_reject_false_resolution_and_noncanonical_shapes() -> None:
    source_range = SourceRange(
        start_line=1,
        start_column=0,
        end_line=1,
        end_column=1,
    )
    with pytest.raises(ValidationError, match="precede"):
        SourceRange(start_line=2, start_column=0, end_line=1, end_column=0)
    with pytest.raises(ValidationError, match="require a symbol"):
        CallReference(
            observed_name="call",
            source_range=source_range,
            resolution="internal",
        )
    with pytest.raises(ValidationError, match="cannot claim"):
        CallReference(
            observed_name="call",
            source_range=source_range,
            resolution="unresolved",
            target_symbol_id="symbol:claimed",
        )
    with pytest.raises(ValidationError, match="require a target file"):
        ImportRecord(
            import_id="import:test",
            module="module",
            imported_name=None,
            alias=None,
            observed_text="import module",
            source_range=source_range,
            resolution="internal",
        )
    with pytest.raises(ValidationError, match="cannot claim a target file"):
        ImportRecord(
            import_id="import:test",
            module="module",
            imported_name=None,
            alias=None,
            observed_text="import module",
            source_range=source_range,
            resolution="external",
            target_file_path="module.py",
        )
    with pytest.raises(ValidationError, match="must name"):
        ImportRecord(
            import_id="import:test",
            module=None,
            imported_name=None,
            alias=None,
            observed_text="import",
            source_range=source_range,
        )
    with pytest.raises(ValidationError, match="require an internal target"):
        RelationshipTarget(resolution="internal")
    with pytest.raises(ValidationError, match="cannot claim internal"):
        RelationshipTarget(resolution="external", file_path="module.py")
    with pytest.raises(ValidationError, match="bounded ASCII"):
        ParserDiagnostic(code="ошибка", message="message", severity="error")
    with pytest.raises(ValidationError, match="bounded text"):
        ParserDiagnostic(code="error", message="bad\x00message", severity="error")
    variable = SymbolRecord(
        symbol_id="symbol:variable",
        name="VALUE",
        qualified_name="module.VALUE",
        kind=SymbolKind.VARIABLE,
        declaration_range=source_range,
    )
    with pytest.raises(ValidationError, match="callable"):
        SymbolRecord(
            **{
                **variable.model_dump(),
                "parameters": (
                    {
                        "name": "value",
                        "kind": "positional_or_keyword",
                    },
                ),
            }
        )
    assert variable.source_range == source_range
    with pytest.raises(ValidationError, match="only classes"):
        SymbolRecord(**{**variable.model_dump(), "contained_methods": ("method",)})
    with pytest.raises(ValidationError, match="canonical"):
        SymbolRecord(
            **{
                **variable.model_dump(),
                "kind": SymbolKind.CLASS,
                "contained_methods": ("z", "a"),
            }
        )
    with pytest.raises(ValidationError, match="configuration keys"):
        SymbolRecord(
            **{
                **variable.model_dump(),
                "configuration_keys": ("DUPLICATE", "DUPLICATE"),
            }
        )


def test_file_codemap_rejects_tampered_references_and_order(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "module.py",
        "def first():\n    pass\n\ndef second():\n    pass\n",
    )
    code_map = _map(tmp_path)
    payload = code_map.model_dump()
    symbols = list(payload["symbols"])

    reversed_symbols = {**payload, "symbols": tuple(reversed(symbols))}
    with pytest.raises(ValidationError, match="source order"):
        FileCodeMap.model_validate(reversed_symbols)

    duplicate_ids = [dict(item) for item in symbols]
    duplicate_ids[1]["symbol_id"] = duplicate_ids[0]["symbol_id"]
    with pytest.raises(ValidationError, match="unique"):
        FileCodeMap.model_validate({**payload, "symbols": tuple(duplicate_ids)})

    absent_parent = [dict(item) for item in symbols]
    absent_parent[0]["parent_symbol_id"] = "symbol:absent"
    with pytest.raises(ValidationError, match="parent symbol"):
        FileCodeMap.model_validate({**payload, "symbols": tuple(absent_parent)})

    absent_method = [dict(item) for item in symbols]
    absent_method[0]["kind"] = SymbolKind.CLASS
    absent_method[0]["contained_methods"] = ("symbol:absent",)
    with pytest.raises(ValidationError, match="contained method"):
        FileCodeMap.model_validate({**payload, "symbols": tuple(absent_method)})

    with pytest.raises(ValidationError, match="top-level constants"):
        FileCodeMap.model_validate({**payload, "top_level_constants": ("Z", "A")})
    with pytest.raises(ValidationError, match="unparsed"):
        FileCodeMap.model_validate({**payload, "parse_status": "parse_error"})

    with pytest.raises(ValueError, match="bytes or text"):
        deserialize_code_map(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="valid UTF-8"):
        deserialize_code_map(b"\xff")
    with pytest.raises(ValueError, match="malformed"):
        deserialize_code_map("{")
