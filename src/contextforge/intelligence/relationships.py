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
    ReferenceOccurrence,
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
        if code_map.parse_status != "parsed":
            resolved.append(code_map)
            continue
        if code_map.language == "Python":
            imports = tuple(
                _resolve_import(item, code_map.path, module_paths)
                for item in code_map.imports
            )
            symbols = tuple(
                _resolve_imported_calls(symbol, imports, code_map.symbols, by_path)
                for symbol in code_map.symbols
            )
        else:
            imports = tuple(
                _resolve_polyglot_import(item, code_map.path, ordered)
                for item in code_map.imports
            )
            symbols = tuple(
                _resolve_polyglot_symbol(
                    symbol, imports, code_map.symbols, by_path, code_map.path
                )
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
                ),
                "direct_references": tuple(
                    _clear_reference_resolution(reference, code_map.path)
                    for reference in symbol.direct_references
                ),
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
    method = (
        "polyglot_ast_call"
        if call.detection_method.startswith("polyglot_")
        else "python_ast_call"
    )
    return call.model_copy(
        update={
            "resolution": "unresolved",
            "target_symbol_id": None,
            "target_file_path": None,
            "detection_method": method,
        }
    )


def _clear_reference_resolution(
    reference: ReferenceOccurrence, source_path: str
) -> ReferenceOccurrence:
    if reference.detection_method == "python_shadowed_reference":
        return reference
    if (
        reference.resolution == "internal"
        and reference.target_file_path == source_path
        and reference.detection_method == "python_lexical_reference"
    ):
        return reference
    method = (
        "polyglot_ast_reference"
        if reference.detection_method.startswith("polyglot_")
        else "python_ast_reference"
    )
    return reference.model_copy(
        update={
            "resolution": "unresolved",
            "target_symbol_id": None,
            "target_file_path": None,
            "detection_method": method,
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


def _resolve_polyglot_import(
    item: ImportRecord,
    source_path: str,
    code_maps: tuple[FileCodeMap, ...],
) -> ImportRecord:
    source_language = next(
        (item.language for item in code_maps if item.path == source_path), None
    )
    candidates = _polyglot_import_candidates(
        item, source_path, code_maps, source_language
    )
    if len(candidates) == 1:
        return item.model_copy(
            update={
                "resolution": "internal",
                "target_file_path": next(iter(candidates)),
            }
        )
    relative = (item.module or "").startswith((".", "crate::", "self::"))
    return item.model_copy(
        update={"resolution": "unresolved" if relative or candidates else "external"}
    )


def _polyglot_import_candidates(
    item: ImportRecord,
    source_path: str,
    code_maps: tuple[FileCodeMap, ...],
    source_language: str | None,
) -> set[str]:
    module = (item.module or "").strip()
    if not module:
        return set()
    source_parent = PurePosixPath(source_path).parent
    relative = module.startswith((".", "crate::", "self::"))
    normalized = module.replace("::", "/").replace("\\", "/")
    normalized = normalized.removeprefix("crate/").removeprefix("self/")
    if source_language in {"Java", "C#"}:
        normalized = normalized.replace(".", "/")
    normalized = normalized.strip("/")
    bases = [normalized]
    if item.imported_name:
        bases.append(f"{normalized}/{item.imported_name}".strip("/"))
    candidates: set[str] = set()
    exact: set[str] = set()
    for code_map in code_maps:
        if code_map.path == source_path:
            continue
        pure = PurePosixPath(code_map.path)
        without_suffix = pure.with_suffix("").as_posix()
        variants = {pure.as_posix(), without_suffix}
        if pure.stem in {"index", "mod", "lib"}:
            variants.add(pure.parent.as_posix())
        for base in bases:
            target = (source_parent / base).as_posix() if relative else base
            target = _normalize_posix(target)
            if target in variants:
                exact.add(code_map.path)
            elif not relative and any(
                value == target or value.endswith(f"/{target}") for value in variants
            ):
                candidates.add(code_map.path)
    return exact or candidates


def _normalize_posix(value: str) -> str:
    parts: list[str] = []
    for part in PurePosixPath(value).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _resolve_polyglot_symbol(
    symbol: SymbolRecord,
    imports: tuple[ImportRecord, ...],
    symbols: tuple[SymbolRecord, ...],
    maps_by_path: dict[str, FileCodeMap],
    source_path: str,
) -> SymbolRecord:
    calls = tuple(
        _resolve_polyglot_occurrence(
            call, imports, symbols, maps_by_path, source_path, is_call=True
        )
        for call in symbol.direct_calls
    )
    references = tuple(
        _resolve_polyglot_occurrence(
            reference,
            imports,
            symbols,
            maps_by_path,
            source_path,
            is_call=False,
        )
        for reference in symbol.direct_references
    )
    return symbol.model_copy(
        update={"direct_calls": calls, "direct_references": references}
    )


def _resolve_polyglot_occurrence(
    occurrence: CallReference | ReferenceOccurrence,
    imports: tuple[ImportRecord, ...],
    symbols: tuple[SymbolRecord, ...],
    maps_by_path: dict[str, FileCodeMap],
    source_path: str,
    *,
    is_call: bool,
) -> CallReference | ReferenceOccurrence:
    observed = occurrence.observed_name
    final_name = _final_observed_name(observed)
    local = [
        item
        for item in symbols
        if item.parent_symbol_id is None and item.name == final_name
    ]
    if len(local) == 1:
        return occurrence.model_copy(
            update={
                "resolution": "internal",
                "target_file_path": source_path,
                "target_symbol_id": local[0].symbol_id,
                "detection_method": (
                    "polyglot_local_call" if is_call else "polyglot_local_reference"
                ),
            }
        )
    targets: set[tuple[str, str, bool]] = set()
    for item in imports:
        if item.resolution != "internal" or item.target_file_path is None:
            continue
        target_names = _polyglot_target_names(observed, item)
        if not target_names:
            continue
        target_map = maps_by_path[item.target_file_path]
        matches = [
            candidate
            for candidate in target_map.symbols
            if candidate.name in target_names
        ]
        if len(matches) == 1:
            package = not _is_exact_polyglot_import(
                item, maps_by_path[source_path].language
            )
            targets.add((item.target_file_path, matches[0].symbol_id, package))
    identities = {(path, symbol_id) for path, symbol_id, _ in targets}
    if len(identities) != 1:
        return occurrence
    target_path, target_id = next(iter(identities))
    package = any(value[2] for value in targets)
    return occurrence.model_copy(
        update={
            "resolution": "internal",
            "target_file_path": target_path,
            "target_symbol_id": target_id,
            "detection_method": (
                "polyglot_package_resolution"
                if package
                else (
                    "polyglot_unambiguous_import_call"
                    if is_call
                    else "polyglot_unambiguous_import_reference"
                )
            ),
        }
    )


def _polyglot_target_names(observed_name: str, item: ImportRecord) -> set[str]:
    parts = [
        part.lstrip("$")
        for part in observed_name.replace("::", ".").replace("->", ".").split(".")
        if part
    ]
    if not parts:
        return set()
    if item.imported_name is None:
        return {parts[-1]}
    binding = item.alias or item.imported_name
    if len(parts) == 1:
        return {parts[0]}
    if parts[0] != binding:
        return set()
    return {parts[-1] if len(parts) > 1 else item.imported_name}


def _final_observed_name(value: str) -> str:
    cleaned = value.replace("::", ".").replace("->", ".")
    return cleaned.rsplit(".", 1)[-1].lstrip("$")


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
    references = _resolve_imported_references(symbol, imports, symbols, maps_by_path)
    return symbol.model_copy(
        update={
            "direct_calls": tuple(calls),
            "direct_references": references,
        }
    )


def _resolve_imported_references(
    symbol: SymbolRecord,
    imports: tuple[ImportRecord, ...],
    symbols: tuple[SymbolRecord, ...],
    maps_by_path: dict[str, FileCodeMap],
) -> tuple[ReferenceOccurrence, ...]:
    references: list[ReferenceOccurrence] = []
    parameter_names = {parameter.name for parameter in symbol.parameters}
    for reference in symbol.direct_references:
        if reference.resolution == "internal":
            references.append(reference)
            continue
        if reference.detection_method == "python_shadowed_reference":
            references.append(reference)
            continue
        if reference.observed_name.split(".")[0] in parameter_names:
            references.append(reference)
            continue
        targets: set[tuple[str, str]] = set()
        for item in imports:
            if item.resolution != "internal" or item.target_file_path is None:
                continue
            containing_symbol = _containing_symbol(symbols, item.source_range)
            if containing_symbol not in {None, symbol.symbol_id}:
                continue
            target_map = maps_by_path.get(item.target_file_path)
            if target_map is None:
                continue
            target_name = _call_target_from_import(reference.observed_name, item)
            if target_name is None:
                continue
            matches = [
                candidate
                for candidate in target_map.symbols
                if candidate.parent_symbol_id is None and candidate.name == target_name
            ]
            if len(matches) == 1:
                targets.add((item.target_file_path, matches[0].symbol_id))
        if len(targets) == 1:
            target_path, target_id = next(iter(targets))
            references.append(
                reference.model_copy(
                    update={
                        "resolution": "internal",
                        "target_file_path": target_path,
                        "target_symbol_id": target_id,
                        "detection_method": "python_unambiguous_import_reference",
                    }
                )
            )
        else:
            references.append(reference)
    return tuple(references)


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
    prefix = "python" if code_map.language == "Python" else "polyglot"
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
                    method=f"{prefix}_lexical_parent",
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
                method=f"{prefix}_{export.kind}_export",
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
                    if prefix == "python" and item.resolution == "internal"
                    else "python_ast_import"
                    if prefix == "python"
                    else "polyglot_snapshot_path_resolution"
                    if item.resolution == "internal"
                    and _is_exact_polyglot_import(item, code_map.language)
                    else "polyglot_package_resolution"
                    if item.resolution == "internal"
                    else "polyglot_ast_import"
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
        for reference in symbol.direct_references:
            relationships.append(
                _relationship(
                    kind="reference",
                    source_path=code_map.path,
                    source_symbol_id=symbol.symbol_id,
                    source_range=reference.source_range,
                    observed_text=reference.observed_name,
                    target=RelationshipTarget(
                        resolution=reference.resolution,
                        file_path=reference.target_file_path,
                        symbol_id=reference.target_symbol_id,
                        observed_name=reference.observed_name,
                    ),
                    method=reference.detection_method,
                )
            )
    return tuple(sorted(relationships, key=_relationship_key))


def _is_exact_polyglot_import(item: ImportRecord, language: str | None) -> bool:
    module = item.module or ""
    if module.startswith((".", "crate::", "self::", "super::")):
        return True
    if language == "Rust" and item.observed_text.lstrip().startswith("mod "):
        return True
    return language in {"C", "C++"} and '"' in item.observed_text


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
        "import",
        "contains",
        "call",
        "reference",
        "export",
        "tests",
        "tested_by",
        "test_reference",
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
