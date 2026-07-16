"""Closed deterministic schemas for verified structural repository facts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from contextforge.intelligence.manifest import canonical_json_bytes
from contextforge.intelligence.models import (
    RECORD_SCHEMA_VERSION,
    AnalyzerIdentity,
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)

CODEMAP_SCHEMA_VERSION: Literal[1] = 1
RESOLVER_VERSION = "1"

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
Resolution = Literal["internal", "external", "unresolved"]
ParseStatus = Literal["parsed", "unsupported", "parse_error"]
Visibility = Literal["public", "private", "explicit_export", "unknown"]


class _DuplicateKeyError(ValueError):
    pass


class SymbolKind(StrEnum):
    """Source declaration kinds approved for verified CodeMaps."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    VARIABLE = "variable"
    TYPE_ALIAS = "type_alias"


class SourceRange(IndexModel):
    """One-based lines and zero-based half-open source columns."""

    start_line: PositiveInt
    start_column: NonNegativeInt
    end_line: PositiveInt
    end_column: NonNegativeInt

    @model_validator(mode="after")
    def validate_order(self) -> SourceRange:
        """Reject backwards or empty-negative source positions."""

        start = (self.start_line, self.start_column)
        end = (self.end_line, self.end_column)
        if end < start:
            raise ValueError("source range end must not precede its start")
        return self


class ParserDiagnostic(IndexModel):
    """Bounded deterministic parser or fallback diagnostic."""

    code: str
    message: str
    severity: Literal["info", "warning", "error"]
    range: SourceRange | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value or len(value) > 100 or not value.isascii():
            raise ValueError("diagnostic code must be bounded ASCII")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value or len(value) > 1_000 or "\x00" in value:
            raise ValueError("diagnostic message must be bounded text")
        return value


class DecoratorRecord(IndexModel):
    """Exact decorator expression observed in source."""

    expression: str
    source_range: SourceRange


class ParameterRecord(IndexModel):
    """One statically declared Python parameter."""

    name: str
    kind: Literal[
        "positional_only",
        "positional_or_keyword",
        "var_positional",
        "keyword_only",
        "var_keyword",
    ]
    annotation: str | None = None
    default: str | None = None


class CallReference(IndexModel):
    """Observed call syntax with a deliberately conservative target status."""

    observed_name: str
    source_range: SourceRange
    resolution: Resolution = "unresolved"
    target_symbol_id: str | None = None
    target_file_path: str | None = None
    detection_method: str = "python_ast_call"

    @field_validator("target_file_path")
    @classmethod
    def validate_target_path(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_resolution(self) -> CallReference:
        if self.resolution == "internal" and self.target_symbol_id is None:
            raise ValueError("internal call targets require a symbol ID")
        if self.resolution != "internal" and (
            self.target_symbol_id is not None or self.target_file_path is not None
        ):
            raise ValueError("non-internal calls cannot claim an internal target")
        return self


class ImportRecord(IndexModel):
    """One alias from an import statement, without importing the module."""

    import_id: str
    module: str | None
    imported_name: str | None
    alias: str | None
    level: NonNegativeInt = 0
    observed_text: str
    source_range: SourceRange
    resolution: Resolution = "unresolved"
    target_file_path: str | None = None

    @field_validator("target_file_path")
    @classmethod
    def validate_target_path(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_resolution(self) -> ImportRecord:
        if self.resolution == "internal" and self.target_file_path is None:
            raise ValueError("internal imports require a target file")
        if self.resolution != "internal" and self.target_file_path is not None:
            raise ValueError("non-internal imports cannot claim a target file")
        if self.module is None and self.imported_name is None:
            raise ValueError("an import must name a module or imported member")
        return self


class ExportRecord(IndexModel):
    """An explicit ``__all__`` or conventional public-name export."""

    export_id: str
    name: str
    kind: Literal["explicit", "conventional"]
    source_range: SourceRange
    target_symbol_id: str | None = None


class RelationshipTarget(IndexModel):
    """Resolution-bearing relationship target descriptor."""

    resolution: Resolution
    file_path: str | None = None
    symbol_id: str | None = None
    module_name: str | None = None
    observed_name: str | None = None

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_target(self) -> RelationshipTarget:
        if self.resolution == "internal" and not (
            self.file_path is not None or self.symbol_id is not None
        ):
            raise ValueError("internal relationships require an internal target")
        if self.resolution != "internal" and (
            self.file_path is not None or self.symbol_id is not None
        ):
            raise ValueError("non-internal relationships cannot claim internal data")
        return self


class RelationshipRecord(IndexModel):
    """One deterministic structural edge with an explicit detection basis."""

    relationship_id: str
    kind: Literal[
        "import",
        "contains",
        "call",
        "export",
        "tests",
        "tested_by",
        "test_reference",
    ]
    source_file_path: str
    source_symbol_id: str | None = None
    source_range: SourceRange
    observed_text: str
    target: RelationshipTarget
    detection_method: str
    resolver_version: str = RESOLVER_VERSION

    @field_validator("source_file_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class SymbolRecord(IndexModel):
    """Verified declaration and directly contained syntax facts."""

    schema_version: Literal[1] = RECORD_SCHEMA_VERSION
    record_kind: Literal["verified_symbol"] = "verified_symbol"
    symbol_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    is_async: bool = False
    signature: str | None = None
    declaration_range: SourceRange
    body_range: SourceRange | None = None
    parent_symbol_id: str | None = None
    docstring: str | None = None
    decorators: tuple[DecoratorRecord, ...] = ()
    parameters: tuple[ParameterRecord, ...] = ()
    return_annotation: str | None = None
    base_classes: tuple[str, ...] = ()
    contained_methods: tuple[str, ...] = ()
    direct_calls: tuple[CallReference, ...] = ()
    raised_exceptions: tuple[str, ...] = ()
    configuration_keys: tuple[str, ...] = ()
    visibility: Visibility = "unknown"

    @model_validator(mode="after")
    def validate_shape(self) -> SymbolRecord:
        if self.kind not in {
            SymbolKind.FUNCTION,
            SymbolKind.ASYNC_FUNCTION,
            SymbolKind.METHOD,
        } and (self.parameters or self.return_annotation is not None):
            raise ValueError("only callable symbols can declare parameters")
        if self.kind != SymbolKind.CLASS and self.contained_methods:
            raise ValueError("only classes can contain method IDs")
        if tuple(self.contained_methods) != tuple(sorted(self.contained_methods)):
            raise ValueError("contained method IDs must be canonical")
        if tuple(self.configuration_keys) != tuple(
            sorted(set(self.configuration_keys))
        ):
            raise ValueError("configuration keys must be unique and canonical")
        return self

    @property
    def source_range(self) -> SourceRange:
        """Compatibility name for callers that request the declaration range."""

        return self.declaration_range


class FileCodeMap(IndexModel):
    """Complete model-free structural projection for one snapshot file."""

    schema_version: Literal[1] = CODEMAP_SCHEMA_VERSION
    record_kind: Literal["verified_file_codemap"] = "verified_file_codemap"
    path: str
    source_sha256: Sha256
    source_size_bytes: NonNegativeInt
    language: str | None
    analyzer: AnalyzerIdentity
    parse_status: ParseStatus
    line_count: NonNegativeInt
    module_docstring: str | None = None
    imports: tuple[ImportRecord, ...] = ()
    exports: tuple[ExportRecord, ...] = ()
    top_level_constants: tuple[str, ...] = ()
    symbols: tuple[SymbolRecord, ...] = ()
    relationships: tuple[RelationshipRecord, ...] = ()
    diagnostics: tuple[ParserDiagnostic, ...] = ()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_canonical_content(self) -> FileCodeMap:
        symbol_keys = tuple(_symbol_order(item) for item in self.symbols)
        if symbol_keys != tuple(sorted(symbol_keys)):
            raise ValueError("symbols must use canonical source order")
        symbol_ids = tuple(item.symbol_id for item in self.symbols)
        if len(symbol_ids) != len(set(symbol_ids)):
            raise ValueError("symbol IDs must be unique")
        known = set(symbol_ids)
        for symbol in self.symbols:
            if (
                symbol.parent_symbol_id is not None
                and symbol.parent_symbol_id not in known
            ):
                raise ValueError("parent symbol ID is absent from the CodeMap")
            if any(method not in known for method in symbol.contained_methods):
                raise ValueError("contained method ID is absent from the CodeMap")
        if tuple(self.top_level_constants) != tuple(
            sorted(set(self.top_level_constants))
        ):
            raise ValueError("top-level constants must be unique and canonical")
        key_groups = (
            (tuple(_import_order(item) for item in self.imports), "imports"),
            (tuple(_export_order(item) for item in self.exports), "exports"),
            (
                tuple(_relationship_order(item) for item in self.relationships),
                "relationships",
            ),
            (
                tuple(_diagnostic_order(item) for item in self.diagnostics),
                "diagnostics",
            ),
        )
        for keys, label in key_groups:
            if keys != tuple(sorted(keys)):
                raise ValueError(f"{label} must use canonical order")
        if self.parse_status != "parsed" and (self.symbols or self.relationships):
            raise ValueError("unparsed CodeMaps cannot claim symbols or relationships")
        return self


def stable_fact_id(prefix: str, *parts: object) -> str:
    """Build a readable content-stable fact identifier."""

    encoded = canonical_json_bytes([prefix, *parts])
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def serialize_code_map(code_map: FileCodeMap) -> bytes:
    """Return canonical UTF-8 JSON suitable for an immutable record."""

    return canonical_json_bytes(code_map.model_dump(mode="json"))


def deserialize_code_map(data: bytes | str) -> FileCodeMap:
    """Strictly load one CodeMap, rejecting duplicate keys and invalid UTF-8."""

    if not isinstance(data, (bytes, str)):
        raise ValueError("CodeMap record must be bytes or text")
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError as exc:
        raise ValueError("CodeMap record is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError as exc:
        raise ValueError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ValueError("CodeMap record is malformed JSON") from exc
    return FileCodeMap.model_validate(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _range_key(value: SourceRange) -> tuple[int, int, int, int]:
    return (
        value.start_line,
        value.start_column,
        value.end_line,
        value.end_column,
    )


def _symbol_order(value: SymbolRecord) -> tuple[object, ...]:
    return (*_range_key(value.declaration_range), value.qualified_name, value.symbol_id)


def _import_order(value: ImportRecord) -> tuple[object, ...]:
    return (
        *_range_key(value.source_range),
        value.module or "",
        value.imported_name or "",
        value.alias or "",
    )


def _export_order(value: ExportRecord) -> tuple[object, ...]:
    return (*_range_key(value.source_range), value.kind, value.name, value.export_id)


def _relationship_order(value: RelationshipRecord) -> tuple[object, ...]:
    return (*_range_key(value.source_range), value.kind, value.relationship_id)


def _diagnostic_order(value: ParserDiagnostic) -> tuple[object, ...]:
    position = (0, 0, 0, 0) if value.range is None else _range_key(value.range)
    return (*position, value.severity, value.code, value.message)


__all__ = [
    "CODEMAP_SCHEMA_VERSION",
    "RESOLVER_VERSION",
    "CallReference",
    "DecoratorRecord",
    "ExportRecord",
    "FileCodeMap",
    "ImportRecord",
    "ParameterRecord",
    "ParserDiagnostic",
    "RelationshipRecord",
    "RelationshipTarget",
    "SourceRange",
    "SymbolKind",
    "SymbolRecord",
    "deserialize_code_map",
    "serialize_code_map",
    "stable_fact_id",
]
