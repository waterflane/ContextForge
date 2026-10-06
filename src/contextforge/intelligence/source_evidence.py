"""Source-bound evidence units derived exclusively from immutable CodeMaps."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

from contextforge.intelligence.codemap import FileCodeMap, SourceRange, SymbolKind
from contextforge.intelligence.file_policy import FILE_POLICY_REGISTRY
from contextforge.intelligence.models import IndexModel, Sha256


class SourceEvidenceUnit(IndexModel):
    path: str
    source_sha256: Sha256
    owner_symbol_id: str
    kind: Literal[
        "implementation",
        "call",
        "reference",
        "callback",
        "decorator",
        "initializer",
        "test-usage",
    ]
    source_range: SourceRange
    evidence_id: str
    basis: Literal["verified-implementation", "observed-syntax"]
    related_symbol_ids: tuple[str, ...] = ()


def source_evidence_capability_version(code_map: FileCodeMap) -> int:
    """Legacy records cannot certify newer execution and test-scope coverage."""
    minimum = {"python-ast": 11, "tree-sitter-polyglot": 16}.get(
        code_map.analyzer.analyzer_id
    )
    version = code_map.analyzer.analyzer_version
    return (
        7
        if minimum is not None and version.isdecimal() and int(version) >= minimum
        else 0
    )


def source_evidence_id(
    path: str, source_sha256: str, fact_identity: str, source_range: SourceRange
) -> str:
    payload = (
        f"{path}\0{source_sha256}\0{fact_identity}\0"
        f"{source_range.start_line}:{source_range.start_column}:"
        f"{source_range.end_line}:{source_range.end_column}"
    )
    return "structural-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def derive_source_evidence_units(
    code_map: FileCodeMap,
) -> tuple[SourceEvidenceUnit, ...]:
    units: dict[str, SourceEvidenceUnit] = {}

    def add(
        owner: str,
        kind: Literal[
            "implementation",
            "call",
            "reference",
            "callback",
            "decorator",
            "initializer",
            "test-usage",
        ],
        identity: str,
        address: SourceRange,
        related: tuple[str, ...] = (),
    ) -> None:
        evidence_id = source_evidence_id(
            code_map.path, code_map.source_sha256, identity, address
        )
        units[evidence_id] = SourceEvidenceUnit(
            path=code_map.path,
            source_sha256=code_map.source_sha256,
            owner_symbol_id=owner,
            kind=kind,
            source_range=address,
            evidence_id=evidence_id,
            basis="verified-implementation"
            if kind == "implementation"
            else "observed-syntax",
            related_symbol_ids=related,
        )

    for symbol in code_map.symbols:
        ending = symbol.body_range or symbol.declaration_range
        implementation = symbol.declaration_range.model_copy(
            update={"end_line": ending.end_line, "end_column": ending.end_column}
        )
        add(
            symbol.symbol_id,
            "implementation",
            f"implementation:{symbol.symbol_id}",
            implementation,
        )
        if symbol.kind in {SymbolKind.VARIABLE, SymbolKind.CONSTANT}:
            readers = tuple(
                sorted(
                    s.symbol_id
                    for s in code_map.symbols
                    if s.parent_symbol_id == symbol.parent_symbol_id
                    and any(
                        r.observed_name == "this." + symbol.name
                        for r in s.direct_references
                    )
                )
            )
            add(
                symbol.symbol_id,
                "initializer",
                f"initializer:{symbol.symbol_id}",
                implementation,
                readers,
            )
        if FILE_POLICY_REGISTRY.is_test(code_map.path) and (
            symbol.direct_calls or symbol.direct_references
        ):
            add(
                symbol.symbol_id,
                "test-usage",
                f"test-usage:{symbol.symbol_id}",
                implementation,
            )
        is_constructor = symbol.kind == SymbolKind.CONSTRUCTOR or (
            symbol.parent_symbol_id is not None
            and (
                (code_map.language == "Python" and symbol.name == "__init__")
                or (
                    code_map.language in {"JavaScript", "TypeScript"}
                    and symbol.name == "constructor"
                )
            )
        )
        if is_constructor:
            receiver = (
                symbol.parameters[0].name
                if code_map.language == "Python" and symbol.parameters
                else "this"
            )
            for initialization in symbol.initializations:
                prefix = receiver + "."
                if not initialization.observed_name.startswith(prefix):
                    continue
                attribute = initialization.observed_name[len(prefix) :]
                readers = tuple(
                    sorted(
                        s.symbol_id
                        for s in code_map.symbols
                        if s.parent_symbol_id == symbol.parent_symbol_id
                        and any(
                            r.observed_name
                            == (
                                (
                                    s.parameters[0].name
                                    if code_map.language == "Python" and s.parameters
                                    else "this"
                                )
                                + "."
                                + attribute
                            )
                            for r in s.direct_references
                        )
                    )
                )
                add(
                    symbol.symbol_id,
                    "initializer",
                    f"initializer:{symbol.symbol_id}:{initialization.observed_name}",
                    initialization.source_range,
                    readers,
                )
        for decorator in symbol.decorators:
            add(
                symbol.symbol_id,
                "decorator",
                f"decorator:{symbol.symbol_id}:{decorator.expression}",
                decorator.source_range,
            )
        for kind, occurrences in (
            ("call", symbol.direct_calls),
            ("reference", symbol.direct_references),
        ):
            for occurrence in occurrences:
                address = occurrence.source_range
                identity = hashlib.sha256(
                    (
                        f"{symbol.symbol_id}:{kind}:{occurrence.observed_name}:"
                        f"{address.start_line}:{address.start_column}:"
                        f"{address.end_line}:{address.end_column}"
                    ).encode()
                ).hexdigest()
                add(symbol.symbol_id, kind, f"{kind}:{identity}", address)  # type: ignore[arg-type]
        for call in symbol.direct_calls:
            for callback in call.callback_arguments:
                add(
                    symbol.symbol_id,
                    "callback",
                    f"callback:{symbol.symbol_id}:{callback.observed_name}",
                    callback.source_range,
                )
    return tuple(
        sorted(
            units.values(),
            key=lambda u: (
                u.source_range.start_line,
                u.source_range.end_line,
                u.evidence_id,
            ),
        )
    )


@dataclass
class _UnitSelectionView:
    units: tuple[SourceEvidenceUnit, ...]
    implementations: tuple[SourceEvidenceUnit, ...]
    owner_indices: dict[str, tuple[int, ...]]
    address_owners: dict[tuple[int, int], str | None] = field(default_factory=dict)


_selection_views: OrderedDict[int, _UnitSelectionView] = OrderedDict()


def _selection_view(units: tuple[SourceEvidenceUnit, ...]) -> _UnitSelectionView:
    key = id(units)
    if key in _selection_views:
        _selection_views.move_to_end(key)
        return _selection_views[key]
    indices: dict[str, list[int]] = {}
    for position, unit in enumerate(units):
        for owner in {unit.owner_symbol_id, *unit.related_symbol_ids}:
            indices.setdefault(owner, []).append(position)
    view = _UnitSelectionView(
        # Retaining the immutable tuple prevents an object ID collision.
        units=units,
        implementations=tuple(
            sorted(
                (u for u in units if u.kind == "implementation"),
                key=lambda u: (
                    u.source_range.end_line - u.source_range.start_line,
                    u.evidence_id,
                ),
            )
        ),
        owner_indices={owner: tuple(values) for owner, values in indices.items()},
    )
    _selection_views[key] = view
    if len(_selection_views) > 32:
        _selection_views.popitem(last=False)
    return view


def select_source_evidence_units(
    units: tuple[SourceEvidenceUnit, ...], ranges: tuple[SourceRange, ...]
) -> tuple[SourceEvidenceUnit, ...]:
    """Select the smallest owning implementation for each observed address."""
    if not ranges or not units:
        return ()
    owners: set[str] = set()
    view = _selection_view(units)
    for address in ranges:
        address_key = (address.start_line, address.end_line)
        if address_key not in view.address_owners:
            owner = next(
                (
                    u.owner_symbol_id
                    for u in view.implementations
                    if u.source_range.start_line <= address.start_line
                    and address.end_line <= u.source_range.end_line
                ),
                None,
            )
            view.address_owners[address_key] = owner
        selected_owner = view.address_owners[address_key]
        if selected_owner is not None:
            owners.add(selected_owner)
    return tuple(
        units[position]
        for position in sorted(
            {
                position
                for owner in owners
                for position in view.owner_indices.get(owner, ())
            }
        )
    )
