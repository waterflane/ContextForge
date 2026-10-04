"""Source-bound evidence units derived exclusively from immutable CodeMaps."""

from __future__ import annotations

import hashlib
from typing import Literal

from contextforge.intelligence.codemap import FileCodeMap, SourceRange
from contextforge.intelligence.models import IndexModel, Sha256


class SourceEvidenceUnit(IndexModel):
    path: str
    source_sha256: Sha256
    owner_symbol_id: str
    kind: Literal["implementation", "call", "reference", "callback", "decorator"]
    source_range: SourceRange
    evidence_id: str
    basis: Literal["verified-implementation", "observed-syntax"]


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
        kind: Literal["implementation", "call", "reference", "callback", "decorator"],
        identity: str,
        address: SourceRange,
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


def select_source_evidence_units(
    units: tuple[SourceEvidenceUnit, ...], ranges: tuple[SourceRange, ...]
) -> tuple[SourceEvidenceUnit, ...]:
    """Select the smallest owning implementation for each observed address."""
    owners: set[str] = set()
    implementations = tuple(u for u in units if u.kind == "implementation")
    for address in ranges:
        owner = min(
            (
                unit
                for unit in implementations
                if unit.source_range.start_line <= address.start_line
                and address.end_line <= unit.source_range.end_line
            ),
            key=lambda u: (
                u.source_range.end_line - u.source_range.start_line,
                u.evidence_id,
            ),
            default=None,
        )
        if owner is not None:
            owners.add(owner.owner_symbol_id)
    return tuple(u for u in units if u.owner_symbol_id in owners)
