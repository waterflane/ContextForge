"""Source-bound function descriptions and a local symbol discovery catalogue."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from contextforge.filesystem import FileTooLargeError, read_file_stably
from contextforge.intelligence.codemap import SymbolRecord
from contextforge.intelligence.graph import _structural_provenance
from contextforge.intelligence.indexer import load_file_code_map
from contextforge.intelligence.models import IndexManifest

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "does",
        "find",
        "for",
        "from",
        "function",
        "how",
        "in",
        "is",
        "it",
        "method",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "where",
        "which",
        "with",
    ]
)


def terms(text: str) -> tuple[str, ...]:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return tuple(
        word.casefold().removesuffix("ly") if len(word) > 6 else word.casefold()
        for word in re.findall(r"[^\W\d_]+", words)
        if word.casefold() not in _STOP
    )


def _inflections(word: str) -> frozenset[str]:
    forms = {word}
    for suffix in ("ions", "ion", "ing", "ed", "es", "s"):
        if word.endswith(suffix):
            stem = word.removesuffix(suffix)
            if len(stem) >= 3:
                forms.add(stem)
                if suffix in {"ing", "ed"} and stem[-1] == stem[-2]:
                    forms.add(stem[:-1])
    return frozenset(forms)


def describe_function(symbol: SymbolRecord) -> str:
    """Describe declared facts; an annotation is not a runtime guarantee."""
    parts = [symbol.docstring.strip()] if symbol.docstring else []
    parameters = [
        p.name + (f": {p.annotation}" if p.annotation else "")
        for p in symbol.parameters
    ]
    if parameters:
        parts.append("Declared parameters: " + ", ".join(parameters) + ".")
    if symbol.return_annotation:
        parts.append(f"Declared return type: {symbol.return_annotation}.")
    calls = list(dict.fromkeys(c.observed_name for c in symbol.direct_calls))
    if calls:
        parts.append("Calls observed in the source: " + ", ".join(calls[:6]) + ".")
    raised = list(dict.fromkeys(symbol.raised_exceptions))
    if raised:
        parts.append("Raises observed in the source: " + ", ".join(raised[:4]) + ".")
    return " ".join(parts) or f"Implementation declared by {symbol.signature}"


@dataclass(frozen=True)
class FunctionEntry:
    key: str
    path: str
    sha256: str
    symbol: SymbolRecord
    text: str
    counts: Counter[str]
    module_description: str = ""
    callers: tuple[str, ...] = ()
    callees: tuple[str, ...] = ()

    def card(self, query: str = "") -> dict[str, object]:
        span = self.symbol.declaration_range
        topical = set(terms(query))
        callers = sorted(
            self.callers, key=lambda name: (-len(topical & set(terms(name))), name)
        )
        return {
            "candidate_id": self.key,
            "path": self.path,
            "name": self.symbol.qualified_name,
            "start_line": span.start_line,
            "end_line": span.end_line,
            "description": describe_function(self.symbol)[:900],
            "module_description": self.module_description[:200],
            "verified_callers_within_two_hops": callers[:4],
            "verified_callees": self.callees[:6],
            "source_preview": "\n".join(self.text.splitlines()[:12])[:1000],
        }


class FunctionCatalogue:
    def __init__(self, source: Path, manifest: IndexManifest) -> None:
        self.entries: dict[str, FunctionEntry] = {}
        call_pairs: list[tuple[tuple[str, str], tuple[str, str]]] = []
        for state in manifest.files:
            if state.record_location is None:
                continue
            code_map = load_file_code_map(source, state.path, manifest=manifest)
            if code_map.source_sha256 != state.source_sha256:
                raise ValueError("Function catalogue source identity mismatch")
            for relationship in code_map.relationships:
                if (
                    relationship.kind == "call"
                    and relationship.target.resolution == "internal"
                    and relationship.source_symbol_id is not None
                    and relationship.target.symbol_id is not None
                    and _structural_provenance(relationship.detection_method)
                    == "verified"
                ):
                    call_pairs.append(
                        (
                            (state.path, relationship.source_symbol_id),
                            (
                                relationship.target.file_path or state.path,
                                relationship.target.symbol_id,
                            ),
                        )
                    )
            symbols = [
                s
                for s in code_map.symbols
                if s.kind.value
                in {"function", "async_function", "method", "constructor"}
                and not s.is_anonymous
            ]
            if not symbols:
                continue
            source_path = source / state.path
            if not source_path.resolve().is_relative_to(source.resolve()):
                raise ValueError("Function catalogue source is outside the repository")
            try:
                read = read_file_stably(
                    source_path,
                    max_size_bytes=max(state.source_size_bytes, 1),
                    expected_size_bytes=state.source_size_bytes,
                )
            except (OSError, FileTooLargeError) as exc:
                raise ValueError("Function catalogue source changed") from exc
            raw = read.content
            if read.sha256 != state.source_sha256:
                raise ValueError("Function catalogue source changed")
            lines = raw.decode("utf-8-sig").splitlines()
            by_id = {s.symbol_id: s for s in code_map.symbols}
            for symbol in symbols:
                span = symbol.declaration_range
                end = (symbol.body_range or span).end_line
                text = "\n".join(lines[span.start_line - 1 : end])
                counts = Counter(terms(text))
                counts.update(terms(symbol.qualified_name) * 3)
                counts.update(terms(symbol.docstring or "") * 3)
                counts.update(terms(code_map.module_docstring or ""))
                # Constants and directly called helper descriptions contribute
                # only through verified local references, not guessed aliases.
                references = [
                    (r.target_symbol_id, r.resolution) for r in symbol.direct_references
                ]
                references.extend(
                    (c.target_symbol_id, c.resolution) for c in symbol.direct_calls
                )
                for target_id, resolution in references:
                    dependency = by_id.get(target_id or "")
                    if dependency is None or resolution != "internal":
                        continue
                    counts.update(terms(dependency.docstring or ""))
                    if dependency.kind.value == "constant":
                        target = dependency.declaration_range
                        counts.update(
                            terms(
                                "\n".join(
                                    lines[target.start_line - 1 : target.end_line]
                                )
                            )
                        )
                key = state.path + "#" + symbol.symbol_id
                self.entries[key] = FunctionEntry(
                    key,
                    state.path,
                    state.source_sha256,
                    symbol,
                    text,
                    counts,
                    code_map.module_docstring or "",
                )
        by_symbol = {(e.path, e.symbol.symbol_id): e for e in self.entries.values()}
        incoming: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        outgoing: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for caller, callee in call_pairs:
            if caller in by_symbol and callee in by_symbol:
                incoming[callee].add(caller)
                outgoing[caller].add(callee)
        for entry in self.entries.values():
            for call in entry.symbol.direct_calls:
                owner = (entry.path, entry.symbol.symbol_id)
                target_identity = (
                    call.target_file_path or entry.path,
                    call.target_symbol_id or "",
                )
                if call.resolution == "internal" and target_identity in by_symbol:
                    incoming[target_identity].add(owner)
                    outgoing[owner].add(target_identity)
        self.links: dict[str, set[str]] = {}
        for key, entry in tuple(self.entries.items()):
            owner = (entry.path, entry.symbol.symbol_id)
            first = incoming[owner]
            callers = first | {
                caller for parent in first for caller in incoming[parent]
            }
            self.links[key] = {
                by_symbol[related].key for related in first | outgoing[owner]
            }
            self.entries[key] = replace(
                entry,
                callers=tuple(
                    sorted(by_symbol[sid].symbol.qualified_name for sid in callers)
                ),
                callees=tuple(
                    sorted(
                        by_symbol[sid].symbol.qualified_name for sid in outgoing[owner]
                    )
                ),
            )
        self.frequency: Counter[str] = Counter()
        for entry in self.entries.values():
            self.frequency.update(entry.counts.keys())
        self.inflections: dict[str, set[str]] = defaultdict(set)
        for word in self.frequency:
            for form in _inflections(word):
                self.inflections[form].add(word)
        self.average_length = sum(
            sum(e.counts.values()) for e in self.entries.values()
        ) / max(len(self.entries), 1)

    def search(self, query: str, limit: int = 12) -> tuple[FunctionEntry, ...]:
        query_terms = set(terms(query))
        # Resolve names only on identifier-component boundaries, never by a fuzzy
        # substring. A search rewrite is discovery, not a new user anchor.
        explicit = query.strip("` \n")
        exact = [
            e
            for e in self.entries.values()
            if e.symbol.qualified_name == explicit
            or e.symbol.qualified_name.endswith("." + explicit)
        ]
        if exact:
            return tuple(sorted(exact, key=lambda e: e.key)[:limit])
        expanded = set(query_terms)
        for word in query_terms:
            for form in _inflections(word):
                expanded.update(self.inflections.get(form, ()))
        ranked = []
        for entry in self.entries.values():
            score = 0.0
            length = sum(entry.counts.values())
            for term in expanded & entry.counts.keys():
                idf = math.log(
                    1
                    + (len(self.entries) - self.frequency[term] + 0.5)
                    / (self.frequency[term] + 0.5)
                )
                frequency = entry.counts[term]
                score += (
                    idf
                    * frequency
                    * 2.2
                    / (
                        frequency
                        + 1.2 * (0.25 + 0.75 * length / max(self.average_length, 1))
                    )
                )
            if score:
                ranked.append((score, entry))
        ordered = [
            e for _, e in sorted(ranked, key=lambda item: (-item[0], item[1].key))
        ]
        # Discovery neighbors remain optional until the planner binds a verified
        # implementation. Preserve lexical leaders and reserve a bounded part of
        # the advertised pool for relevant call endpoints before truncation.
        seen = {e.key for e in ordered[:4]}
        frontier = set(seen)
        for _ in range(2):
            discovered = {
                neighbor for key in sorted(frontier) for neighbor in self.links[key]
            } - seen
            frontier = set(sorted(discovered)[: max(64 - len(seen), 0)])
            seen.update(frontier)
        kept = ordered[: max(limit - 4, 0)] if limit > 4 else ordered[:limit]
        keys = {e.key for e in kept}
        complementary = [e for e in ordered if e.key in seen and e.key not in keys]
        kept.extend(complementary[: min(4, limit - len(kept))])
        keys = {e.key for e in kept}
        kept.extend(e for e in ordered if e.key not in keys)
        return tuple(kept[:limit])
