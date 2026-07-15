"""Strict offline loading, validation, and inspection of JSON packages."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import PydanticSerializationError

from contextforge.context.package import (
    CONTEXT_PACKAGE_SCHEMA_VERSION,
    ContextPackage,
)
from contextforge.context.renderers import MAX_JSON_PACKAGE_BYTES

_ROOT_FIELDS = {
    "schema_version",
    "title",
    "project",
    "tree",
    "files",
    "statistics",
}


class ContextInspectionError(ValueError):
    """Base class for portable-package inspection failures."""


class PackageReadError(ContextInspectionError):
    """Raised when package input cannot be bounded or decoded."""


class PackageValidationError(ContextInspectionError):
    """Raised when JSON syntax, shape, or package semantics are invalid."""


class UnsupportedSchemaVersionError(PackageValidationError):
    """Raised when a package requests an unsupported integer schema version."""

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        super().__init__(
            f"unsupported context package schema version: {schema_version}"
        )


class ContextInspection(BaseModel):
    """Concise deterministic information calculated from a valid package."""

    schema_version: Literal[1]
    title: str
    selectable_file_count: int
    selectable_directory_count: int
    selected_file_count: int
    ranged_file_count: int
    included_content_bytes: int
    included_character_count: int
    included_line_count: int
    languages: dict[str, int]

    model_config = ConfigDict(frozen=True, extra="forbid")


class _DuplicateKeyError(ValueError):
    pass


def load_context_package_json(
    data: bytes | str,
    *,
    max_size_bytes: int = MAX_JSON_PACKAGE_BYTES,
) -> ContextPackage:
    """Load and fully validate a package from bounded UTF-8 bytes or text.

    Inspection is self-contained: paths inside the package are validated as
    portable strings and are never opened or resolved against a repository.
    """

    text = _decode_json_input(data, max_size_bytes=max_size_bytes)
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        raise PackageValidationError("context package JSON root must be an object")

    version = parsed.get("schema_version")
    if type(version) is not int:
        raise PackageValidationError("schema_version must be the integer 1")
    if version != CONTEXT_PACKAGE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(version)
    _validate_root_fields(parsed)

    try:
        return ContextPackage.model_validate_json(text, strict=True)
    except ValidationError as exc:
        raise PackageValidationError(_validation_message(exc)) from exc


def validate_context_package(package: ContextPackage) -> ContextPackage:
    """Revalidate a package instance, including all computed invariants."""

    if not isinstance(package, ContextPackage):
        raise PackageValidationError("expected a ContextPackage")
    try:
        payload = json.dumps(
            package.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return ContextPackage.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        raise PackageValidationError(_validation_message(exc)) from exc
    except (TypeError, ValueError, PydanticSerializationError) as exc:
        raise PackageValidationError(
            "context package cannot be validated as JSON"
        ) from exc


def inspect_context_package(package: ContextPackage) -> ContextInspection:
    """Calculate deterministic inspection information from a valid package."""

    validated = validate_context_package(package)
    statistics = validated.statistics
    return ContextInspection(
        schema_version=validated.schema_version,
        title=validated.title,
        selectable_file_count=validated.project.selectable_file_count,
        selectable_directory_count=validated.project.selectable_directory_count,
        selected_file_count=statistics.selected_file_count,
        ranged_file_count=statistics.ranged_file_count,
        included_content_bytes=statistics.included_content_bytes,
        included_character_count=statistics.included_character_count,
        included_line_count=statistics.included_line_count,
        languages=dict(statistics.languages),
    )


def inspect_context_package_json(
    data: bytes | str,
    *,
    max_size_bytes: int = MAX_JSON_PACKAGE_BYTES,
) -> tuple[ContextPackage, ContextInspection]:
    """Load a JSON package and return both its canonical model and summary."""

    package = load_context_package_json(data, max_size_bytes=max_size_bytes)
    return package, inspect_context_package(package)


def render_context_inspection(inspection: ContextInspection) -> str:
    """Render a concise deterministic plain-text inspection summary."""

    if not isinstance(inspection, ContextInspection):
        raise PackageValidationError("expected a ContextInspection")
    languages = (
        ", ".join(
            f"{language}: {count}" for language, count in inspection.languages.items()
        )
        if inspection.languages
        else "none"
    )
    return "\n".join(
        (
            f"Schema version: {inspection.schema_version}",
            f"Title: {inspection.title}",
            f"Selectable files: {inspection.selectable_file_count}",
            f"Selectable directories: {inspection.selectable_directory_count}",
            f"Selected files: {inspection.selected_file_count}",
            f"Ranged files: {inspection.ranged_file_count}",
            f"Included content bytes: {inspection.included_content_bytes}",
            f"Included characters: {inspection.included_character_count}",
            f"Included lines: {inspection.included_line_count}",
            f"Languages: {languages}",
            "",
        )
    )


def _decode_json_input(data: bytes | str, *, max_size_bytes: int) -> str:
    if type(max_size_bytes) is not int or max_size_bytes <= 0:
        raise PackageReadError("max_size_bytes must be a positive integer")
    if isinstance(data, bytes):
        size_bytes = len(data)
        raw = data
        if size_bytes > max_size_bytes:
            raise PackageReadError(
                f"JSON package has {size_bytes} bytes; limit is {max_size_bytes}"
            )
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PackageReadError("JSON package is not valid UTF-8") from exc
    elif isinstance(data, str):
        try:
            size_bytes = len(data.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise PackageReadError(
                "JSON package text contains invalid Unicode"
            ) from exc
        if size_bytes > max_size_bytes:
            raise PackageReadError(
                f"JSON package has {size_bytes} bytes; limit is {max_size_bytes}"
            )
        text = data
    else:
        raise PackageReadError("JSON package input must be bytes or text")
    return text.removeprefix("\ufeff")


def _parse_json(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise PackageValidationError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise PackageValidationError(
            f"malformed JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def _object_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _DuplicateKeyError(f"invalid JSON numeric constant: {value}")


def _validate_root_fields(payload: dict[str, Any]) -> None:
    actual = set(payload)
    missing = sorted(_ROOT_FIELDS - actual)
    extra = sorted(actual - _ROOT_FIELDS)
    if missing:
        raise PackageValidationError(
            f"context package is missing required field: {missing[0]}"
        )
    if extra:
        raise PackageValidationError(
            f"context package contains unknown field: {extra[0]}"
        )


def _validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False, include_context=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "package"
    return f"invalid context package at {location}: {first['msg']}"
