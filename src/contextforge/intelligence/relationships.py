"""Conservative cross-file resolution for verified CodeMap syntax facts."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Literal

from contextforge.intelligence.codemap import (
    RESOLVER_VERSION,
    CallReference,
    FileCodeMap,
    ImportRecord,
    RelationshipRecord,
    RelationshipTarget,
    SourceRange,
    SymbolRecord,
    stable_fact_id,
)


def resolve_relationships(
    code_maps: tuple[FileCodeMap, ...],
    *,
    repository_paths: Iterable[str] | None = None,
) -> tuple[FileCodeMap, ...]:
    """Resolve only unambiguous snapshot modules, names, and test associations."""

    ordered = tuple(
        sorted(
            (_clear_repository_resolution(item) for item in code_maps),
            key=lambda item: item.path,
        )
    )
    if len({item.path for item in ordered}) != len(ordered):
        raise ValueError("CodeMap paths must be unique")
    module_paths = _module_path_index(ordered, repository_paths=repository_paths)
    by_path = {item.path: item for item in ordered}

    resolved: list[FileCodeMap] = []
    for code_map in ordered:
        if code_map.parse_status != "parsed" or code_map.language != "Python":
            resolved.append(code_map)
            continue
        imports = tuple(
            _resolve_import(item, code_map.path, module_paths)
            for item in code_map.imports
        )
        symbols = tuple(
            _resolve_imported_calls(symbol, imports, code_map.symbols, by_path)
            for symbol in code_map.symbols
        )
        relationships = _rebuild_resolved_relationships(code_map, imports, symbols)
        resolved.append(
            code_map.model_copy(
                update={
                    "imports": imports,
                    "symbols": symbols,
                    "relationships": relationships,
                }
            )
        )

    return _add_test_relationships(tuple(resolved))


def _clear_repository_resolution(code_map: FileCodeMap) -> FileCodeMap:
    if code_map.parse_status != "parsed":
        return code_map
    imports = tuple(
        item.model_copy(
            update={
                "resolution": "unresolved",
                "target_file_path": None,
            }
        )
        for item in code_map.imports
    )
    symbols = tuple(
        symbol.model_copy(
            update={
                "direct_calls": tuple(
                    _clear_call_resolution(call, code_map.path)
                    for call in symbol.direct_calls
                )
            }
        )
        for symbol in code_map.symbols
    )
    return code_map.model_copy(
        update={
            "imports": imports,
            "symbols": symbols,
            "relationships": (),
        }
    )


def _clear_call_resolution(call: CallReference, source_path: str) -> CallReference:
    if call.detection_method == "python_shadowed_name":
        return call
    if (
        call.resolution == "internal"
        and call.target_file_path == source_path
        and call.detection_method == "python_lexical_name"
    ):
        return call
    return call.model_copy(
        update={
            "resolution": "unresolved",
            "target_symbol_id": None,
            "target_file_path": None,
            "detection_method": "python_ast_call",
        }
    )


def _module_path_index(
    code_maps: tuple[FileCodeMap, ...],
    *,
    repository_paths: Iterable[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    values: dict[str, set[str]] = {}
    paths = {item.path for item in code_maps if item.language == "Python"}
    if repository_paths is not None:
        paths.update(path for path in repository_paths if path.endswith(".py"))
    for path in sorted(paths):
        for module in _module_names_for_path(path):
            values.setdefault(module, set()).add(path)
    return {module: tuple(sorted(paths)) for module, paths in values.items()}


def _module_names_for_path(path: str) -> tuple[str, ...]:
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return ()
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    names: set[str] = set()
    if parts:
        names.add(".".join(parts))
        if parts[0] in {"src", "lib"} and len(parts) > 1:
            names.add(".".join(parts[1:]))
    return tuple(sorted(names))


def _resolve_import(
    item: ImportRecord,
    source_path: str,
    module_paths: dict[str, tuple[str, ...]],
) -> ImportRecord:
    candidates: set[str] = set()
    bases = _absolute_import_modules(item, source_path)
    for base in bases:
        module_candidates = [base]
        if item.imported_name not in {None, "*"}:
            imported_name = item.imported_name
            assert imported_name is not None
            module_candidates.insert(
                0, f"{base}.{imported_name}" if base else imported_name
            )
        for module in module_candidates:
            candidates.update(module_paths.get(module, ()))
    if len(candidates) == 1:
        return item.model_copy(
            update={
                "resolution": "internal",
                "target_file_path": next(iter(candidates)),
            }
        )
    if item.level == 0 and not candidates:
        return item.model_copy(update={"resolution": "external"})
    return item.model_copy(update={"resolution": "unresolved"})


def _absolute_import_modules(item: ImportRecord, source_path: str) -> tuple[str, ...]:
    if item.level == 0:
        return (item.module or "",)
    results: set[str] = set()
    for source_module in _module_names_for_path(source_path):
        source_parts = source_module.split(".")
        if (
            not source_path.endswith("/__init__.py")
            and PurePosixPath(source_path).name != "__init__.py"
        ):
            source_parts = source_parts[:-1]
        remove = item.level - 1
        if remove > len(source_parts):
            continue
        prefix = source_parts[: len(source_parts) - remove]
        suffix = item.module.split(".") if item.module else []
        absolute = ".".join((*prefix, *suffix))
        results.add(absolute)
    return tuple(sorted(results))


def _resolve_imported_calls(
    symbol: SymbolRecord,
    imports: tuple[ImportRecord, ...],
    symbols: tuple[SymbolRecord, ...],
    maps_by_path: dict[str, FileCodeMap],
) -> SymbolRecord:
    calls: list[CallReference] = []
    for call in symbol.direct_calls:
        if call.resolution == "internal":
            calls.append(call)
            continue
        if call.detection_method == "python_shadowed_name":
            calls.append(call)
            continue
        if call.observed_name.split(".")[0] in {
            parameter.name for parameter in symbol.parameters
        }:
            calls.append(call)
            continue
        targets: list[tuple[str, str]] = []
        for item in imports:
            if item.resolution != "internal" or item.target_file_path is None:
                continue
            containing_symbol = _containing_symbol(symbols, item.source_range)
            if containing_symbol not in {None, symbol.symbol_id}:
                continue
            target_map = maps_by_path.get(item.target_file_path)
            if target_map is None:
                continue
            target_name = _call_target_from_import(call.observed_name, item)
            if target_name is None:
                continue
            matches = [
                candidate
                for candidate in target_map.symbols
                if candidate.parent_symbol_id is None and candidate.name == target_name
            ]
            if len(matches) == 1:
                targets.append((item.target_file_path, matches[0].symbol_id))
        if len(set(targets)) == 1:
            target_path, target_id = targets[0]
            calls.append(
                call.model_copy(
                    update={
                        "resolution": "internal",
                        "target_file_path": target_path,
                        "target_symbol_id": target_id,
                        "detection_method": "python_unambiguous_import_alias",
                    }
                )
            )
        else:
            calls.append(call)
    return symbol.model_copy(update={"direct_calls": tuple(calls)})


def _call_target_from_import(observed_name: str, item: ImportRecord) -> str | None:
    parts = observed_name.split(".")
    if item.imported_name is not None:
        binding = item.alias or item.imported_name
        if parts != [binding]:
            return None
        return item.imported_name
    if item.module is None:
        return None
    if item.alias is not None:
        return parts[1] if len(parts) == 2 and parts[0] == item.alias else None
    module_parts = item.module.split(".")
    if parts[:-1] != module_parts:
        return None
    return parts[-1] if len(parts) == len(module_parts) + 1 else None


def _rebuild_resolved_relationships(
    code_map: FileCodeMap,
    imports: tuple[ImportRecord, ...],
    symbols: tuple[SymbolRecord, ...],
) -> tuple[RelationshipRecord, ...]:
    relationships: list[RelationshipRecord] = []
    for symbol in symbols:
        if symbol.parent_symbol_id is not None:
            relationships.append(
                _relationship(
                    kind="contains",
                    source_path=code_map.path,
                    source_symbol_id=symbol.parent_symbol_id,
                    source_range=symbol.declaration_range,
                    observed_text=symbol.name,
                    target=RelationshipTarget(
                        resolution="internal",
                        file_path=code_map.path,
                        symbol_id=symbol.symbol_id,
                    ),
                    method="python_lexical_parent",
                )
            )
    for export in code_map.exports:
        relationships.append(
            _relationship(
                kind="export",
                source_path=code_map.path,
                source_symbol_id=None,
                source_range=export.source_range,
                observed_text=export.name,
                target=RelationshipTarget(
                    resolution=(
                        "internal"
                        if export.target_symbol_id is not None
                        else "unresolved"
                    ),
                    file_path=(
                        code_map.path if export.target_symbol_id is not None else None
                    ),
                    symbol_id=export.target_symbol_id,
                    observed_name=export.name,
                ),
                method=f"python_{export.kind}_export",
            )
        )
    for item in imports:
        module_name = "." * item.level + (item.module or "")
        relationships.append(
            _relationship(
                kind="import",
                source_path=code_map.path,
                source_symbol_id=_containing_symbol(symbols, item.source_range),
                source_range=item.source_range,
                observed_text=item.observed_text,
                target=RelationshipTarget(
                    resolution=item.resolution,
                    file_path=item.target_file_path,
                    module_name=module_name,
                    observed_name=item.imported_name or item.module,
                ),
                method=(
                    "python_snapshot_module_resolution"
                    if item.resolution == "internal"
                    else "python_ast_import"
                ),
            )
        )
    for symbol in symbols:
        for call in symbol.direct_calls:
            relationships.append(
                _relationship(
                    kind="call",
                    source_path=code_map.path,
                    source_symbol_id=symbol.symbol_id,
                    source_range=call.source_range,
                    observed_text=call.observed_name,
                    target=RelationshipTarget(
                        resolution=call.resolution,
                        file_path=call.target_file_path,
                        symbol_id=call.target_symbol_id,
                        observed_name=call.observed_name,
                    ),
                    method=call.detection_method,
                )
            )
    return tuple(sorted(relationships, key=_relationship_key))


def _add_test_relationships(
    code_maps: tuple[FileCodeMap, ...],
) -> tuple[FileCodeMap, ...]:
    by_path = {item.path: item for item in code_maps}
    additions: dict[str, list[RelationshipRecord]] = {path: [] for path in by_path}
    implementation_by_basename: dict[str, list[str]] = {}
    for code_map in code_maps:
        if not _is_test_path(code_map.path):
            implementation_by_basename.setdefault(
                PurePosixPath(code_map.path).name, []
            ).append(code_map.path)

    for test_map in code_maps:
        if test_map.parse_status != "parsed" or not _is_test_path(test_map.path):
            continue
        links: dict[str, tuple[SourceRange, str]] = {}
        for item in test_map.imports:
            if (
                item.resolution == "internal"
                and item.target_file_path is not None
                and not _is_test_path(item.target_file_path)
                and by_path[item.target_file_path].parse_status == "parsed"
            ):
                links[item.target_file_path] = (
                    item.source_range,
                    "python_unambiguous_test_import",
                )
        conventional = _conventional_implementation_name(test_map.path)
        candidates = (
            implementation_by_basename.get(conventional, []) if conventional else []
        )
        if len(candidates) == 1 and by_path[candidates[0]].parse_status == "parsed":
            links.setdefault(
                candidates[0],
                (
                    SourceRange(start_line=1, start_column=0, end_line=1, end_column=0),
                    "python_test_path_convention",
                ),
            )
        for implementation_path, (source_range, method) in sorted(links.items()):
            additions[test_map.path].append(
                _relationship(
                    kind="tests",
                    source_path=test_map.path,
                    source_symbol_id=None,
                    source_range=source_range,
                    observed_text=implementation_path,
                    target=RelationshipTarget(
                        resolution="internal", file_path=implementation_path
                    ),
                    method=method,
                )
            )
            additions[implementation_path].append(
                _relationship(
                    kind="tested_by",
                    source_path=implementation_path,
                    source_symbol_id=None,
                    source_range=SourceRange(
                        start_line=1, start_column=0, end_line=1, end_column=0
                    ),
                    observed_text=test_map.path,
                    target=RelationshipTarget(
                        resolution="internal", file_path=test_map.path
                    ),
                    method=method,
                )
            )
        for relationship in test_map.relationships:
            if (
                relationship.kind != "call"
                or relationship.target.resolution != "internal"
            ):
                continue
            target_path = relationship.target.file_path
            if target_path is None or _is_test_path(target_path):
                continue
            additions[test_map.path].append(
                _relationship(
                    kind="test_reference",
                    source_path=test_map.path,
                    source_symbol_id=relationship.source_symbol_id,
                    source_range=relationship.source_range,
                    observed_text=relationship.observed_text,
                    target=relationship.target,
                    method="python_resolved_test_call",
                )
            )

    results: list[FileCodeMap] = []
    for code_map in code_maps:
        combined = {
            item.relationship_id: item
            for item in (*code_map.relationships, *additions[code_map.path])
        }
        relationships = tuple(sorted(combined.values(), key=_relationship_key))
        results.append(code_map.model_copy(update={"relationships": relationships}))
    return tuple(results)


def _relationship(
    *,
    kind: Literal[
        "import", "contains", "call", "export", "tests", "tested_by", "test_reference"
    ],
    source_path: str,
    source_symbol_id: str | None,
    source_range: SourceRange,
    observed_text: str,
    target: RelationshipTarget,
    method: str,
) -> RelationshipRecord:
    relationship_id = stable_fact_id(
        "relationship",
        kind,
        source_path,
        source_symbol_id,
        _range_key(source_range),
        target.model_dump(mode="json"),
        method,
        RESOLVER_VERSION,
    )
    return RelationshipRecord(
        relationship_id=relationship_id,
        kind=kind,
        source_file_path=source_path,
        source_symbol_id=source_symbol_id,
        source_range=source_range,
        observed_text=observed_text,
        target=target,
        detection_method=method,
    )


def _containing_symbol(
    symbols: tuple[SymbolRecord, ...], source_range: SourceRange
) -> str | None:
    candidates = [
        symbol
        for symbol in symbols
        if (symbol.declaration_range.start_line, symbol.declaration_range.start_column)
        <= (source_range.start_line, source_range.start_column)
        and (source_range.end_line, source_range.end_column)
        <= (symbol.declaration_range.end_line, symbol.declaration_range.end_column)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda symbol: (
            symbol.declaration_range.start_line,
            symbol.declaration_range.start_column,
        ),
    ).symbol_id


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        "tests" in pure.parts
        or pure.name.startswith("test_")
        or pure.stem.endswith("_test")
    )


def _conventional_implementation_name(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name.startswith("test_"):
        return name[len("test_") :]
    if name.endswith("_test.py"):
        return f"{name[: -len('_test.py')]}.py"
    return None


def _range_key(value: SourceRange) -> tuple[int, int, int, int]:
    return (value.start_line, value.start_column, value.end_line, value.end_column)


def _relationship_key(value: RelationshipRecord) -> tuple[object, ...]:
    return (*_range_key(value.source_range), value.kind, value.relationship_id)


__all__ = ["resolve_relationships"]
