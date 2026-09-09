"""Persisted BM25 and graph-aware retrieval for Index v3."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.intelligence.cards import SemanticCard
from contextforge.intelligence.codemap import FileCodeMap, SourceRange
from contextforge.intelligence.models import (
    IndexManifest,
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)
from contextforge.models import ModelProvider, ModelRequest

RETRIEVAL_SCHEMA_VERSION: Literal[3] = 3
BM25_K1 = 1.2
BM25_B = 0.75
FIELD_WEIGHTS = {
    "path": 4.0,
    "symbols": 3.0,
    "source_identifiers": 2.0,
    "grounded_semantics": 2.0,
}
RepresentationMode = Literal["map", "summary", "slice", "full"]
ExactGroup = Literal[
    "exact_path",
    "exact_qualified_symbol",
    "exact_symbol",
    "exact_source_identifier",
    "approximate",
]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RetrievalField(IndexModel):
    """Canonical term frequencies for one weighted document field."""

    name: Literal["path", "symbols", "source_identifiers", "grounded_semantics"]
    length: NonNegativeInt
    terms: dict[str, NonNegativeInt]

    @field_validator("terms")
    @classmethod
    def validate_terms(
        cls, value: dict[str, NonNegativeInt]
    ) -> dict[str, NonNegativeInt]:
        if tuple(value) != tuple(sorted(value)) or any(
            count <= 0 for count in value.values()
        ):
            raise ValueError("retrieval terms must be positive and canonical")
        return value


class RetrievalDocument(IndexModel):
    """One source-bound persisted BM25 document."""

    path: str
    source_sha256: Sha256
    fields: tuple[RetrievalField, ...]
    symbols: tuple[str, ...] = ()
    qualified_symbols: tuple[str, ...] = ()
    source_identifiers: tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_fields(self) -> RetrievalDocument:
        names = tuple(item.name for item in self.fields)
        if names != tuple(FIELD_WEIGHTS):
            raise ValueError("retrieval fields must use the canonical weighted order")
        for values in (self.symbols, self.qualified_symbols, self.source_identifiers):
            if values != tuple(sorted(set(values), key=str.casefold)):
                raise ValueError("retrieval identifiers must be unique and canonical")
        return self


class RetrievalIndex(IndexModel):
    """Generation-pinned persisted postings inputs."""

    schema_version: Literal[3] = RETRIEVAL_SCHEMA_VERSION
    record_kind: Literal["retrieval_postings"] = "retrieval_postings"
    source_snapshot_digest: Sha256
    document_count: NonNegativeInt
    documents: tuple[RetrievalDocument, ...]
    document_frequencies: dict[str, dict[str, NonNegativeInt]]
    average_field_lengths: dict[str, NonNegativeFloat]

    @model_validator(mode="after")
    def validate_index(self) -> RetrievalIndex:
        paths = tuple(item.path for item in self.documents)
        if paths != tuple(sorted(set(paths))) or self.document_count != len(paths):
            raise ValueError("retrieval documents must be unique and canonical")
        if set(self.document_frequencies) != set(FIELD_WEIGHTS):
            raise ValueError("document frequencies use an invalid field order")
        if set(self.average_field_lengths) != set(FIELD_WEIGHTS):
            raise ValueError("average lengths use an invalid field order")
        return self


class CandidateEvidenceRange(IndexModel):
    path: str
    source_range: SourceRange
    evidence_id: str | None = None
    strength: Literal["verified", "grounded"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class CandidateGraphNeighbor(IndexModel):
    path: str
    distance: Literal[1, 2]
    relationship_kinds: tuple[str, ...]
    provenance: tuple[str, ...]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class RepresentationCosts(IndexModel):
    map: NonNegativeInt
    summary: NonNegativeInt | None = None
    slice: NonNegativeInt | None = None
    full: NonNegativeInt


class CandidateCard(IndexModel):
    """Retrieval result with evidence, graph context, and representation costs."""

    candidate_id: str
    path: str
    source_sha256: Sha256
    synopsis: str
    exact_group: ExactGroup
    score: NonNegativeFloat
    bm25_score: NonNegativeFloat
    matched_concepts: tuple[str, ...] = ()
    matched_symbols: tuple[str, ...] = ()
    evidence_ranges: tuple[CandidateEvidenceRange, ...] = ()
    graph_neighbors: tuple[CandidateGraphNeighbor, ...] = ()
    provenance: tuple[str, ...]
    freshness: Literal["current"] = "current"
    estimated_cost: RepresentationCosts
    suggested_representation: RepresentationMode | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class RetrievalResult(IndexModel):
    """Deterministic candidates plus bounded optional rerank diagnostics."""

    schema_version: Literal[3] = RETRIEVAL_SCHEMA_VERSION
    source_snapshot_digest: Sha256
    generation_id: Sha256
    task: str
    candidates: tuple[CandidateCard, ...]
    reranked: bool = False
    provider_calls: NonNegativeInt = 0
    diagnostics: tuple[str, ...] = ()


class _RerankItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    representation: RepresentationMode | None = None


class _RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    ordered: tuple[_RerankItem, ...]


def build_retrieval_index(
    code_maps: tuple[FileCodeMap, ...],
    cards: tuple[SemanticCard, ...],
    source_snapshot_digest: str,
) -> RetrievalIndex:
    """Build canonical weighted term frequencies from facts and grounded prose."""

    cards_by_path = {item.path: item for item in cards}
    documents: list[RetrievalDocument] = []
    for code_map in sorted(code_maps, key=lambda item: item.path):
        card = cards_by_path.get(code_map.path)
        symbols = tuple(
            sorted({item.name for item in code_map.symbols}, key=str.casefold)
        )
        qualified = tuple(
            sorted({item.qualified_name for item in code_map.symbols}, key=str.casefold)
        )
        identifiers = tuple(
            sorted(
                {
                    *code_map.top_level_constants,
                    *(item.name for item in code_map.exports),
                    *(
                        key
                        for item in code_map.symbols
                        for key in item.configuration_keys
                    ),
                },
                key=str.casefold,
            )
        )
        values = {
            "path": code_map.path,
            "symbols": " ".join((*symbols, *qualified)),
            "source_identifiers": " ".join(identifiers),
            "grounded_semantics": "" if card is None else card.ranking_text(),
        }
        fields = tuple(_retrieval_field(name, values[name]) for name in FIELD_WEIGHTS)
        documents.append(
            RetrievalDocument(
                path=code_map.path,
                source_sha256=code_map.source_sha256,
                fields=fields,
                symbols=symbols,
                qualified_symbols=qualified,
                source_identifiers=identifiers,
            )
        )
    frequencies: dict[str, dict[str, int]] = {}
    averages: dict[str, float] = {}
    for field in FIELD_WEIGHTS:
        field_values = [
            next(item for item in doc.fields if item.name == field) for doc in documents
        ]
        counter: Counter[str] = Counter()
        for value in field_values:
            counter.update(value.terms.keys())
        frequencies[field] = dict(sorted(counter.items()))
        averages[field] = (
            sum(item.length for item in field_values) / len(field_values)
            if field_values
            else 0.0
        )
    return RetrievalIndex(
        source_snapshot_digest=source_snapshot_digest,
        document_count=len(documents),
        documents=tuple(documents),
        document_frequencies=frequencies,
        average_field_lengths=averages,
    )


async def retrieve_context_candidates(
    repository_root: str | Path,
    task: str,
    *,
    manifest: IndexManifest | None = None,
    working_set: tuple[str, ...] = (),
    diff_paths: tuple[str, ...] = (),
    limit: int = 20,
    provider: ModelProvider | None = None,
    rerank: bool = False,
    cancellation: asyncio.Event | None = None,
) -> RetrievalResult:
    """Retrieve generation-pinned candidates; model reranking is optional."""

    if not task.strip() or len(task) > 20_000:
        raise ValueError("retrieval task must be bounded non-empty text")
    if type(limit) is not int or limit <= 0 or limit > 1_000:
        raise ValueError("retrieval limit must be between 1 and 1000")
    from contextforge.intelligence.cards import load_semantic_card
    from contextforge.intelligence.indexer import (
        load_file_code_map,
        load_relationship_graph,
    )
    from contextforge.intelligence.store import (
        IndexStorageError,
        load_generation_record,
        load_manifest,
    )

    active = manifest if manifest is not None else load_manifest(repository_root)
    if active.schema_version != 3:
        raise ValueError("retrieval requires an Index v3 generation")
    reference = (
        active.artifacts.semantic_retrieval or active.artifacts.structural_retrieval
    )
    if reference is None:
        raise ValueError("pinned generation has no persisted retrieval postings")
    index = RetrievalIndex.model_validate_json(
        load_generation_record(repository_root, reference.location, manifest=active)
    )
    if index.source_snapshot_digest != active.build.source_snapshot_digest:
        raise ValueError("retrieval postings are stale for the pinned generation")
    graph = load_relationship_graph(repository_root, manifest=active)
    code_maps = {
        state.path: load_file_code_map(repository_root, state.path, manifest=active)
        for state in active.files
    }
    cards: dict[str, SemanticCard] = {}
    for state in active.files:
        if state.semantic_status not in {"complete", "partial"}:
            continue
        try:
            cards[state.path] = load_semantic_card(
                repository_root, state.path, manifest=active
            )
        except (ValueError, IndexStorageError):
            continue
    candidates = _rank_candidates(
        task,
        index,
        code_maps,
        cards,
        graph,
        working_set=working_set,
        diff_paths=diff_paths,
    )[:limit]
    result = RetrievalResult(
        source_snapshot_digest=active.build.source_snapshot_digest,
        generation_id=active.generation_id,
        task=task,
        candidates=tuple(candidates),
    )
    if not rerank or provider is None or not candidates:
        return result
    return await _rerank_result(provider, result, cancellation)


def _rank_candidates(
    task: str,
    index: RetrievalIndex,
    code_maps: dict[str, FileCodeMap],
    cards: dict[str, SemanticCard],
    graph: object,
    *,
    working_set: tuple[str, ...],
    diff_paths: tuple[str, ...],
) -> list[CandidateCard]:
    from contextforge.intelligence.graph import RelationshipGraph

    if not isinstance(graph, RelationshipGraph):
        raise TypeError("relationship graph is required")
    query_terms = _tokens(task)
    task_folded = task.casefold()
    working = set(working_set)
    diff = set(diff_paths)
    exact_by_path: dict[str, ExactGroup] = {}
    matched_symbols: dict[str, tuple[str, ...]] = {}
    for document in index.documents:
        groups: list[ExactGroup] = []
        if _exact_text(task_folded, document.path.casefold()):
            groups.append("exact_path")
        qualified_matches = tuple(
            value
            for value in document.qualified_symbols
            if _exact_text(task_folded, value.casefold())
        )
        symbol_matches = tuple(
            value
            for value in document.symbols
            if _exact_text(task_folded, value.casefold())
        )
        identifier_matches = tuple(
            value
            for value in document.source_identifiers
            if _exact_text(task_folded, value.casefold())
        )
        if qualified_matches:
            groups.append("exact_qualified_symbol")
        if symbol_matches:
            groups.append("exact_symbol")
        if identifier_matches:
            groups.append("exact_source_identifier")
        exact_by_path[document.path] = (
            min(groups, key=_group_order) if groups else "approximate"
        )
        matched_symbols[document.path] = tuple(
            dict.fromkeys((*qualified_matches, *symbol_matches, *identifier_matches))
        )

    seeds = {
        path for path, group in exact_by_path.items() if group != "approximate"
    } | working
    distances = _graph_distances(graph, seeds)
    metrics = {item.path: item for item in graph.file_metrics}
    neighbors = _candidate_neighbors(graph)
    results: list[CandidateCard] = []
    for document in index.documents:
        bm25 = _bm25(document, query_terms, index)
        distance = distances.get(document.path)
        graph_score = 0.20 if distance == 1 else 0.08 if distance == 2 else 0.0
        centrality = metrics[document.path].normalized_centrality * 0.10
        score = bm25 + graph_score + centrality
        if document.path in diff:
            score += 0.25
        if document.path in working:
            score += 1.0
        card = cards.get(document.path)
        code_map = code_maps[document.path]
        concepts = _matched_concepts(card, query_terms)
        evidence = _candidate_evidence(
            code_map, card, query_terms, matched_symbols[document.path]
        )
        synopsis = (
            card.synopsis.text
            if card is not None
            else f"Structural map for {document.path}."
        )
        provenance = ["verified-structure"]
        if card is not None:
            provenance.append("grounded-semantic-card")
        if distance in {1, 2}:
            provenance.append(f"graph-{distance}-hop")
        results.append(
            CandidateCard(
                candidate_id=_candidate_id(document.path),
                path=document.path,
                source_sha256=document.source_sha256,
                synopsis=synopsis,
                exact_group=exact_by_path[document.path],
                score=max(score, 0.0),
                bm25_score=max(bm25, 0.0),
                matched_concepts=concepts,
                matched_symbols=matched_symbols[document.path],
                evidence_ranges=evidence,
                graph_neighbors=neighbors.get(document.path, ()),
                provenance=tuple(provenance),
                estimated_cost=_representation_costs(code_map, card, evidence),
            )
        )
    results.sort(
        key=lambda item: (_group_order(item.exact_group), -item.score, item.path)
    )
    return results


def _bm25(
    document: RetrievalDocument, query_terms: tuple[str, ...], index: RetrievalIndex
) -> float:
    score = 0.0
    for field in document.fields:
        average = index.average_field_lengths[field.name] or 1.0
        frequencies = index.document_frequencies[field.name]
        for term in query_terms:
            frequency = field.terms.get(term, 0)
            if not frequency:
                continue
            document_frequency = frequencies.get(term, 0)
            inverse = math.log(
                1.0
                + (index.document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            denominator = frequency + BM25_K1 * (
                1.0 - BM25_B + BM25_B * field.length / average
            )
            score += (
                FIELD_WEIGHTS[field.name]
                * inverse
                * frequency
                * (BM25_K1 + 1.0)
                / denominator
            )
    return score


def _graph_distances(graph: object, seeds: set[str]) -> dict[str, int]:
    from contextforge.intelligence.graph import RelationshipGraph

    assert isinstance(graph, RelationshipGraph)
    node_path = {item.node_id: item.path for item in graph.nodes}
    adjacent: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        source = node_path[edge.source_node_id]
        target = node_path[edge.target_node_id]
        if source != target:
            adjacent[source].add(target)
            adjacent[target].add(source)
    distance: dict[str, int] = {path: 0 for path in seeds}
    queue = deque(sorted(seeds))
    while queue:
        source = queue.popleft()
        if distance[source] >= 2:
            continue
        for target in sorted(adjacent[source]):
            if target not in distance:
                distance[target] = distance[source] + 1
                queue.append(target)
    return distance


def _candidate_neighbors(
    graph: object,
) -> dict[str, tuple[CandidateGraphNeighbor, ...]]:
    from contextforge.intelligence.graph import RelationshipGraph, RelationshipGraphEdge

    assert isinstance(graph, RelationshipGraph)
    node_path = {item.node_id: item.path for item in graph.nodes}
    grouped: dict[tuple[str, str], list[RelationshipGraphEdge]] = defaultdict(list)
    for edge in graph.edges:
        source = node_path[edge.source_node_id]
        target = node_path[edge.target_node_id]
        if source != target:
            grouped[(source, target)].append(edge)
            grouped[(target, source)].append(edge)
    by_path: dict[str, tuple[CandidateGraphNeighbor, ...]] = {}
    for source in {item.path for item in graph.file_metrics}:
        values = []
        for (candidate_source, target), edges in sorted(grouped.items()):
            if candidate_source != source:
                continue
            values.append(
                CandidateGraphNeighbor(
                    path=target,
                    distance=1,
                    relationship_kinds=tuple(sorted({edge.kind for edge in edges})),
                    provenance=tuple(sorted({edge.provenance for edge in edges})),
                )
            )
        by_path[source] = tuple(values[:12])
    return by_path


def _candidate_evidence(
    code_map: FileCodeMap,
    card: SemanticCard | None,
    query_terms: tuple[str, ...],
    symbol_matches: tuple[str, ...],
) -> tuple[CandidateEvidenceRange, ...]:
    values: dict[tuple[int, int], CandidateEvidenceRange] = {}
    matched_folded = {item.casefold() for item in symbol_matches}
    for symbol in code_map.symbols:
        if (
            symbol.name.casefold() in matched_folded
            or symbol.qualified_name.casefold() in matched_folded
        ):
            key = (
                symbol.declaration_range.start_line,
                symbol.declaration_range.end_line,
            )
            values[key] = CandidateEvidenceRange(
                path=code_map.path,
                source_range=symbol.declaration_range,
                strength="verified",
            )
    if card is not None:
        known = {item.evidence_id: item for item in card.evidence}
        claims = [
            card.synopsis,
            *card.concepts,
            *card.responsibilities,
            *card.side_effects,
        ]
        for claim in claims:
            if not set(_tokens(claim.text)) & set(query_terms):
                continue
            for evidence_id in claim.evidence_ids:
                evidence = known[evidence_id]
                if evidence.source_range is None:
                    continue
                key = (evidence.source_range.start_line, evidence.source_range.end_line)
                values[key] = CandidateEvidenceRange(
                    path=code_map.path,
                    source_range=evidence.source_range,
                    evidence_id=evidence_id,
                    strength="grounded",
                )
    return tuple(values[key] for key in sorted(values))


def _matched_concepts(
    card: SemanticCard | None, query_terms: tuple[str, ...]
) -> tuple[str, ...]:
    if card is None:
        return ()
    query = set(query_terms)
    return tuple(
        claim.text for claim in card.concepts if set(_tokens(claim.text)) & query
    )


def _representation_costs(
    code_map: FileCodeMap,
    card: SemanticCard | None,
    evidence: tuple[CandidateEvidenceRange, ...],
) -> RepresentationCosts:
    signatures = "\n".join(
        symbol.signature or symbol.qualified_name for symbol in code_map.symbols
    )
    map_cost = _estimate_tokens(signatures or code_map.path)
    summary_cost = None if card is None else _estimate_tokens(card.ranking_text())
    slice_lines = sum(
        item.source_range.end_line - item.source_range.start_line + 11
        for item in evidence
    )
    slice_cost = None if not evidence else max(slice_lines * 8, 1)
    return RepresentationCosts(
        map=map_cost,
        summary=summary_cost,
        slice=slice_cost,
        full=(code_map.source_size_bytes + 2) // 3,
    )


async def _rerank_result(
    provider: ModelProvider,
    result: RetrievalResult,
    cancellation: asyncio.Event | None,
) -> RetrievalResult:
    supplied = {item.candidate_id: item for item in result.candidates}
    request = ModelRequest(
        operation_id="retrieval-rerank-" + result.generation_id[:24],
        purpose="retrieval-rerank",
        system_instructions=(
            "Reorder only supplied candidate IDs. Do not add IDs or source claims. "
            "A representation may be map, summary, slice, or full."
        ),
        analysis_task=result.task,
        trusted_code_map_facts={
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "path": item.path,
                    "synopsis": item.synopsis,
                    "score": item.score,
                    "available_representations": [
                        name
                        for name, cost in item.estimated_cost.model_dump().items()
                        if cost is not None
                    ],
                }
                for item in result.candidates
            ]
        },
        untrusted_sources=(),
        response_model=_RerankResponse,
        max_output_tokens=1_024,
    )
    for attempt in range(2):
        try:
            response = await provider.complete_structured(
                request, cancellation=cancellation
            )
        except Exception:
            if attempt == 0:
                continue
            return result.model_copy(
                update={
                    "provider_calls": 2,
                    "diagnostics": ("rerank_failed_deterministic_fallback",),
                }
            )
        if not isinstance(response.value, _RerankResponse):
            break
        identifiers = tuple(item.candidate_id for item in response.value.ordered)
        if len(identifiers) != len(set(identifiers)) or not set(identifiers) <= set(
            supplied
        ):
            if attempt == 0:
                continue
            break
        ordered = [
            supplied[item.candidate_id].model_copy(
                update={"suggested_representation": item.representation}
            )
            for item in response.value.ordered
        ]
        ordered.extend(
            item for item in result.candidates if item.candidate_id not in identifiers
        )
        return result.model_copy(
            update={
                "candidates": tuple(ordered),
                "reranked": True,
                "provider_calls": attempt + 1,
            }
        )
    return result.model_copy(
        update={
            "provider_calls": 2,
            "diagnostics": ("invalid_rerank_deterministic_fallback",),
        }
    )


def _retrieval_field(name: str, text: str) -> RetrievalField:
    tokens = _tokens(text)
    return RetrievalField(
        name=name,  # type: ignore[arg-type]
        length=len(tokens),
        terms=dict(sorted(Counter(tokens).items())),
    )


def _tokens(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE):
        values.append(raw)
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text):
        values.extend(
            part.casefold()
            for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", raw)
            if part
        )
    return tuple(values)


def _exact_text(task: str, value: str) -> bool:
    if not value:
        return False
    return re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", task) is not None


def _group_order(group: ExactGroup) -> int:
    return {
        "exact_path": 0,
        "exact_qualified_symbol": 1,
        "exact_symbol": 2,
        "exact_source_identifier": 3,
        "approximate": 4,
    }[group]


def _candidate_id(path: str) -> str:
    return "candidate-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


def _estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 2) // 3


__all__ = [
    "BM25_B",
    "BM25_K1",
    "FIELD_WEIGHTS",
    "RETRIEVAL_SCHEMA_VERSION",
    "CandidateCard",
    "CandidateEvidenceRange",
    "CandidateGraphNeighbor",
    "ExactGroup",
    "RepresentationCosts",
    "RetrievalDocument",
    "RetrievalField",
    "RetrievalIndex",
    "RetrievalResult",
    "build_retrieval_index",
    "retrieve_context_candidates",
]
