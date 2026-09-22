"""Token-aware Context Capsule v2 compiler for pinned Index v3 generations."""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.context.evidence_diagnostics import (
    CompilationReasonCode,
    EvidenceCoverageDiagnostics,
    compiled_evidence_diagnostics,
)
from contextforge.context.reader import ReaderLimits, read_selected_text_file
from contextforge.intelligence.cards import SemanticCard, load_semantic_card
from contextforge.intelligence.codemap import FileCodeMap, SourceRange
from contextforge.intelligence.graph import OrientationMap
from contextforge.intelligence.indexer import (
    load_file_code_map,
    load_orientation_map,
)
from contextforge.intelligence.models import IndexManifest, Sha256
from contextforge.intelligence.retrieval import (
    CandidateCard,
    CandidateEvidenceRange,
    CoverageLedger,
    ExactGroup,
    PlannedEvidence,
    RetrievalResult,
    build_coverage_ledger,
)
from contextforge.intelligence.store import IndexStorageError, load_manifest
from contextforge.repositories import ProjectFile, ProjectSnapshot, scan_repository

CONTEXT_CAPSULE_SCHEMA_VERSION: Literal[2] = 2
SLICE_CONTEXT_LINES = 5
SLICE_MERGE_GAP = 3
AUTOMATIC_FULL_FILE_MAX_LINES = 200
AUTOMATIC_FULL_UPGRADE_MAX_TOKENS = 512
AUTOMATIC_CONTEXT_SOFT_RATIO = 0.30
AUTOMATIC_SLICE_MAX_RANGES = 3
AUTOMATIC_MAP_MAX_SYMBOLS = 12
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class RepresentationMode(StrEnum):
    """Available source representations in increasing materialization detail."""

    MAP = "map"
    SUMMARY = "summary"
    SLICE = "slice"
    FULL = "full"


class TokenEstimator(Protocol):
    """Pluggable token counter used for every compiler budget decision."""

    @property
    def estimator_id(self) -> str: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class ConservativeTokenEstimator:
    """Stable default preserving the conservative UTF-8 bytes / 3 estimate."""

    estimator_id: str = "utf8-bytes-ceil-div-3-v1"

    def count(self, text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / 3)


class CapsuleModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextBudget(CapsuleModel):
    """Caller-owned context-window deductions and compiler allocation inputs."""

    context_window_tokens: PositiveInt
    history_tokens: NonNegativeInt = 0
    response_tokens: NonNegativeInt = 0
    safety_margin_tokens: NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_remaining_space(self) -> ContextBudget:
        if self.available_tokens <= 0:
            raise ValueError("context budget deductions leave no available tokens")
        return self

    @property
    def available_tokens(self) -> int:
        return self.context_window_tokens - (
            self.history_tokens + self.response_tokens + self.safety_margin_tokens
        )

    def initial_allocations(self, payload_tokens: int) -> dict[str, int]:
        if payload_tokens < 0:
            raise ValueError("payload token budget cannot be negative")
        orientation = payload_tokens * 20 // 100
        working = payload_tokens * 15 // 100
        evidence = payload_tokens * 55 // 100
        metadata = payload_tokens - orientation - working - evidence
        return {
            "orientation": orientation,
            "working_set": working,
            "task_evidence": evidence,
            "diff_metadata": metadata,
        }


class CapsuleRange(CapsuleModel):
    start_line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def validate_order(self) -> CapsuleRange:
        if self.end_line < self.start_line:
            raise ValueError("capsule range end must not precede its start")
        return self


class CapsuleMaterial(CapsuleModel):
    path: str
    source_sha256: Sha256
    representation: RepresentationMode
    content: str
    ranges: tuple[CapsuleRange, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    relevance: float = Field(ge=0.0, allow_inf_nan=False)
    provenance: tuple[str, ...]
    token_count: NonNegativeInt

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        from contextforge.core.validation import validate_portable_relative_path

        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_ranges(self) -> CapsuleMaterial:
        if (self.representation == RepresentationMode.SLICE) != bool(self.ranges):
            raise ValueError("only slice material contains source ranges")
        previous_end = 0
        for item in self.ranges:
            if item.start_line <= previous_end:
                raise ValueError("capsule ranges must be sorted and disjoint")
            previous_end = item.end_line
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("material evidence IDs must be unique and canonical")
        return self


class CapsuleSnapshot(CapsuleModel):
    generation_id: Sha256
    source_snapshot_digest: Sha256
    generation_kind: Literal["structural", "enriched"]
    index_schema_version: Literal[3]


class ContextCapsule(CapsuleModel):
    """Portable, generation-pinned, fully budgeted context artifact."""

    schema_version: Literal[2] = CONTEXT_CAPSULE_SCHEMA_VERSION
    task: str = Field(min_length=1, max_length=20_000)
    snapshot: CapsuleSnapshot
    repository_map: str
    working_set: tuple[CapsuleMaterial, ...] = ()
    task_context: tuple[CapsuleMaterial, ...] = ()
    git_context: str = ""
    interpretations: tuple[str, ...] = ()
    compact_profile: bool = False
    evidence_diagnostics: EvidenceCoverageDiagnostics | None = None
    allocations: dict[str, NonNegativeInt]
    estimator_id: str = Field(min_length=1, max_length=200)
    token_count: NonNegativeInt

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        task = value.strip()
        if not task or "\x00" in task:
            raise ValueError("capsule task must be bounded non-empty text")
        return task

    @model_validator(mode="after")
    def validate_materials(self) -> ContextCapsule:
        keys = [item.path for item in (*self.working_set, *self.task_context)]
        if len(keys) != len(set(keys)):
            raise ValueError("capsule material identities must be unique")
        if tuple(self.allocations) != tuple(sorted(self.allocations)):
            raise ValueError("capsule allocations must be canonical")
        return self


class CompiledContextCapsule(CapsuleModel):
    capsule: ContextCapsule
    prompt: str
    token_count: NonNegativeInt
    estimator_id: str
    coverage_ledger: CoverageLedger | None = None
    compilation_sufficiency: CompilationSufficiency | None = None

    @property
    def evidence_diagnostics(self) -> EvidenceCoverageDiagnostics | None:
        return self.capsule.evidence_diagnostics

    @model_validator(mode="after")
    def validate_metadata(self) -> CompiledContextCapsule:
        if (
            self.token_count != self.capsule.token_count
            or self.estimator_id != self.capsule.estimator_id
        ):
            raise ValueError("compiled capsule metadata is inconsistent")
        return self


class CompilationSufficiency(CapsuleModel):
    """Verified effective status after the compiler's real materialization."""

    schema_version: Literal[1] = 1
    declared_status: Literal["sufficient", "insufficient"]
    effective_status: Literal["sufficient", "insufficient"]
    reason_codes: tuple[CompilationReasonCode, ...] = ()
    planned_item_ids: tuple[str, ...] = ()
    materialized_item_ids: tuple[str, ...] = ()
    missing_planned_item_ids: tuple[str, ...] = ()
    planned_evidence_ids: tuple[str, ...] = ()
    materialized_evidence_ids: tuple[str, ...] = ()
    planned_range_count: NonNegativeInt = 0
    materialized_range_count: NonNegativeInt = 0
    planned_role_ids: tuple[str, ...] = ()
    materialized_role_ids: tuple[str, ...] = ()
    missing_mandatory_role_ids: tuple[str, ...] = ()
    replacement_used: bool = False

    @model_validator(mode="after")
    def validate_sufficiency(self) -> CompilationSufficiency:
        for values, label in (
            (self.reason_codes, "reason codes"),
            (self.planned_item_ids, "planned item IDs"),
            (self.materialized_item_ids, "materialized item IDs"),
            (self.missing_planned_item_ids, "missing planned item IDs"),
            (self.planned_evidence_ids, "planned evidence IDs"),
            (self.materialized_evidence_ids, "materialized evidence IDs"),
            (self.planned_role_ids, "planned role IDs"),
            (self.materialized_role_ids, "materialized role IDs"),
            (self.missing_mandatory_role_ids, "missing mandatory role IDs"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"compilation {label} must be unique and canonical")
        if self.missing_planned_item_ids != tuple(
            item
            for item in self.planned_item_ids
            if item not in self.materialized_item_ids
        ):
            raise ValueError("compilation missing items must match materialization")
        if (
            self.declared_status == "insufficient"
            and self.effective_status != "insufficient"
        ):
            raise ValueError("insufficient plan cannot become effectively sufficient")
        if self.effective_status == "sufficient" and self.reason_codes:
            raise ValueError(
                "sufficient compilation cannot carry insufficiency reasons"
            )
        return self


class ContextCompilerError(RuntimeError):
    """Base expected compiler failure."""


class ContextBudgetError(ContextCompilerError):
    """Raised when even the indivisible capsule envelope cannot fit."""


class ContextFreshnessError(ContextCompilerError):
    """Raised before stale source or grounded prose can be materialized."""


@dataclass(slots=True)
class _CompilerState:
    root: Path
    manifest: IndexManifest
    snapshot: ProjectSnapshot
    files: dict[str, ProjectFile]
    estimator: TokenEstimator
    code_maps: dict[str, FileCodeMap]
    cards: dict[str, SemanticCard | None]
    sources: dict[str, tuple[str, int]]
    pinned_full: set[str]


def compile_context_capsule(
    repository_root: str | Path,
    task: str,
    retrieval: RetrievalResult,
    *,
    budget: ContextBudget,
    manifest: IndexManifest | None = None,
    working_files: tuple[str, ...] = (),
    working_lines: dict[str, tuple[SourceRange, ...]] | None = None,
    pinned_full_files: tuple[str, ...] = (),
    git_diff: str | object | None = None,
    estimator: TokenEstimator | None = None,
) -> CompiledContextCapsule:
    """Compile deterministic retrieval evidence into a hard-budgeted v2 prompt."""

    if not isinstance(retrieval, RetrievalResult):
        raise TypeError("compiler requires a RetrievalResult")
    active = manifest if manifest is not None else load_manifest(repository_root)
    if active.schema_version != 3:
        raise ContextCompilerError("Context Capsule v2 requires Index v3")
    if (
        retrieval.generation_id != active.generation_id
        or retrieval.source_snapshot_digest != active.build.source_snapshot_digest
    ):
        raise ContextFreshnessError("retrieval result is not pinned to the generation")
    selected_estimator = estimator or ConservativeTokenEstimator()
    if not selected_estimator.estimator_id.strip():
        raise ValueError("token estimator requires a stable estimator_id")
    requested_working = _canonical_paths(working_files, "working files")
    pinned = set(_canonical_paths(pinned_full_files, "pinned full files"))
    working = tuple(sorted({*requested_working, *pinned}))
    lines = {} if working_lines is None else dict(working_lines)
    if not set(lines) <= set(working):
        raise ValueError("working line ranges require a matching working file")

    snapshot = scan_repository(repository_root)
    state = _CompilerState(
        root=Path(repository_root).resolve(),
        manifest=active,
        snapshot=snapshot,
        files={item.path: item for item in snapshot.files},
        estimator=selected_estimator,
        code_maps={},
        cards={},
        sources={},
        pinned_full=pinned,
    )
    known_paths = {item.path for item in active.files}
    if not set(working) | pinned <= known_paths:
        raise ValueError("working and pinned files must belong to the generation")

    snapshot_model = CapsuleSnapshot(
        generation_id=active.generation_id,
        source_snapshot_digest=active.build.source_snapshot_digest,
        generation_kind=active.generation_kind,
        index_schema_version=active.schema_version,
    )
    allocations = budget.initial_allocations(0)
    capsule = ContextCapsule(
        task=task,
        snapshot=snapshot_model,
        repository_map="",
        allocations=dict(sorted(allocations.items())),
        estimator_id=selected_estimator.estimator_id,
        token_count=0,
    )
    envelope_tokens = selected_estimator.count(_render_capsule(capsule))
    if envelope_tokens > budget.available_tokens:
        raise ContextBudgetError("context budget is smaller than the capsule envelope")
    explicit_material = bool(working or lines or git_diff is not None)
    automatic_limit = (
        budget.available_tokens
        if explicit_material
        else max(
            envelope_tokens,
            int(budget.available_tokens * AUTOMATIC_CONTEXT_SOFT_RATIO),
        )
    )
    allocations = budget.initial_allocations(max(automatic_limit - envelope_tokens, 0))
    for _ in range(4):
        capsule = capsule.model_copy(
            update={"allocations": dict(sorted(allocations.items()))}
        )
        envelope_tokens = selected_estimator.count(_render_capsule(capsule))
        if not explicit_material and envelope_tokens > automatic_limit:
            automatic_limit = envelope_tokens
        adjusted = budget.initial_allocations(max(automatic_limit - envelope_tokens, 0))
        if adjusted == allocations:
            break
        allocations = adjusted
    allow_indivisible_automatic_upgrade = (
        not explicit_material
        and int(budget.available_tokens * AUTOMATIC_CONTEXT_SOFT_RATIO)
        <= envelope_tokens
    )

    orientation = load_orientation_map(repository_root, manifest=active)
    repository_map = _render_orientation(
        orientation,
        allocations["orientation"],
        selected_estimator,
        full=explicit_material,
    )
    git_text = _git_text(git_diff)
    interpretations: list[str] = []
    if selected_estimator.count(git_text) > allocations["diff_metadata"]:
        git_text = ""
        if git_diff is not None:
            interpretations.append(
                "Git diff omitted because its complete section exceeded budget."
            )

    capsule = capsule.model_copy(
        update={
            "repository_map": repository_map,
            "git_context": git_text,
            "allocations": dict(sorted(allocations.items())),
            "interpretations": tuple(interpretations),
        }
    )
    if selected_estimator.count(_render_capsule(capsule)) > budget.available_tokens:
        capsule = capsule.model_copy(update={"repository_map": "", "git_context": ""})
        repository_map = ""
        git_text = ""

    candidate_by_path = {item.path: item for item in retrieval.candidates}
    working_material: list[CapsuleMaterial] = []
    for path in working:
        candidate = candidate_by_path.get(path)
        mode = (
            RepresentationMode.FULL
            if path in pinned
            else RepresentationMode.SLICE
            if lines.get(path)
            else RepresentationMode.MAP
        )
        material = _materialize(state, path, mode, candidate, lines.get(path, ()))
        if material is not None:
            proposed = capsule.model_copy(
                update={"working_set": tuple((*working_material, material))}
            )
            if _fits(proposed, budget, selected_estimator):
                working_material.append(material)
                continue
        fallback = _materialize(state, path, RepresentationMode.MAP, candidate, ())
        if fallback is not None and _fits(
            capsule.model_copy(
                update={"working_set": tuple((*working_material, fallback))}
            ),
            budget,
            selected_estimator,
        ):
            working_material.append(fallback)
    capsule = capsule.model_copy(update={"working_set": tuple(working_material)})

    evidence_material: list[CapsuleMaterial] = []
    evidence_limit = (
        allocations["task_evidence"]
        + max(allocations["orientation"] - selected_estimator.count(repository_map), 0)
        + max(
            allocations["working_set"]
            - sum(item.token_count for item in working_material),
            0,
        )
        + max(allocations["diff_metadata"] - selected_estimator.count(git_text), 0)
    )
    evidence_tokens = 0
    plan_fallback = False
    if retrieval.evidence_plan is not None:
        planned_material = _materialize_validated_plan(
            state,
            capsule,
            retrieval.evidence_plan.items,
            {item.candidate_id: item for item in retrieval.candidates},
            {item.path for item in working_material},
            budget,
            selected_estimator,
            token_limit=automatic_limit,
            evidence_limit=evidence_limit,
        )
        if planned_material is None:
            plan_fallback = True
            interpretations.append(
                "Evidence plan was not fully materializable; deterministic "
                "complementary selection replaced the entire plan."
            )
        else:
            evidence_material.extend(planned_material)
            evidence_tokens = sum(item.token_count for item in evidence_material)
            capsule = capsule.model_copy(
                update={"task_context": tuple(evidence_material)}
            )

    eligible_candidates = [
        candidate
        for candidate in retrieval.candidates
        if candidate.path not in set(working) and _is_automatic_candidate(candidate)
    ][:8]
    if retrieval.evidence_plan is None or plan_fallback:
        evidence_material, evidence_tokens, capsule = _select_automatic_evidence(
            state,
            task,
            capsule,
            retrieval.candidates,
            eligible_candidates,
            evidence_material,
            evidence_tokens,
            budget,
            selected_estimator,
            token_limit=automatic_limit,
        )
    capsule = capsule.model_copy(update={"task_context": tuple(evidence_material)})

    if not explicit_material:
        selected_paths = tuple(item.path for item in evidence_material)
        compact_map = _render_orientation(
            orientation,
            allocations["orientation"],
            selected_estimator,
            selected_paths=selected_paths,
            full=False,
        )
        capsule = capsule.model_copy(update={"repository_map": compact_map})

    if retrieval.evidence_plan is None or plan_fallback:
        capsule = _apply_greedy_upgrades(
            state,
            task,
            capsule,
            retrieval.candidates,
            lines,
            budget,
            selected_estimator,
            token_limit=automatic_limit,
            allow_indivisible_upgrade=allow_indivisible_automatic_upgrade,
        )
    rationales = list(dict.fromkeys((*capsule.interpretations, *interpretations)))
    if retrieval.evidence_plan is not None and not plan_fallback:
        if retrieval.evidence_plan.interpretation:
            rationales.append(
                "Evidence planner interpretation: "
                + retrieval.evidence_plan.interpretation
            )
        if retrieval.evidence_plan.sufficiency == "insufficient":
            rationales.append(
                "Evidence planner marked supplied candidates insufficient."
            )
    if not evidence_material and not working_material:
        rationales.append(
            "Task context is insufficient because retrieval produced no "
            "materializable task evidence."
        )
    for candidate in retrieval.candidates:
        if candidate.suggested_representation is not None:
            rationales.append(
                "Model rerank representation suggestion for "
                f"{candidate.path}: {candidate.suggested_representation} "
                "(interpretation)."
            )
    capsule = capsule.model_copy(update={"interpretations": tuple(rationales)})
    if not explicit_material:
        compact = _compact_profile(capsule, orientation, retrieval.candidates)
        if compact is not None and selected_estimator.count(
            _render_capsule(compact)
        ) < selected_estimator.count(_render_capsule(capsule)):
            capsule = compact
    prompt = _render_capsule(capsule)
    token_count = selected_estimator.count(prompt)
    if (
        not explicit_material
        and token_count > automatic_limit
        and capsule.interpretations
    ):
        capsule = capsule.model_copy(update={"interpretations": ()})
        prompt = _render_capsule(capsule)
        token_count = selected_estimator.count(prompt)
    if token_count > budget.available_tokens:
        capsule = capsule.model_copy(update={"interpretations": ()})
        prompt = _render_capsule(capsule)
        token_count = selected_estimator.count(prompt)
    if token_count > budget.available_tokens:
        raise ContextBudgetError("indivisible selected context exceeds the hard budget")
    capsule = capsule.model_copy(update={"token_count": token_count})
    materialized_paths = {
        item.path for item in (*capsule.working_set, *capsule.task_context)
    }
    materialized_ids = tuple(
        item.candidate_id
        for item in retrieval.candidates
        if item.path in materialized_paths
    )
    planner_bindings = (
        () if retrieval.evidence_plan is None else retrieval.evidence_plan.role_bindings
    )
    materialization_ledger = build_coverage_ledger(
        task,
        retrieval.candidates,
        selected_candidate_ids=materialized_ids,
        stage="materialization",
        planner_bindings=planner_bindings,
    )
    sufficiency = _compilation_sufficiency(
        retrieval,
        capsule,
        materialization_ledger,
        plan_replaced=plan_fallback,
    )
    capsule = capsule.model_copy(
        update={
            "evidence_diagnostics": compiled_evidence_diagnostics(
                retrieval,
                materialization_ledger,
                sufficiency,
                compact_profile=capsule.compact_profile,
            )
        }
    )
    return CompiledContextCapsule(
        capsule=capsule,
        prompt=prompt,
        token_count=token_count,
        estimator_id=selected_estimator.estimator_id,
        coverage_ledger=materialization_ledger,
        compilation_sufficiency=sufficiency,
    )


def _compilation_sufficiency(
    retrieval: RetrievalResult,
    capsule: ContextCapsule,
    materialization_ledger: CoverageLedger,
    *,
    plan_replaced: bool,
) -> CompilationSufficiency:
    """Derive effective status from actual materials without mutating retrieval."""

    plan = retrieval.evidence_plan
    declared_status = "insufficient" if plan is None else plan.sufficiency
    candidates_by_id = {item.candidate_id: item for item in retrieval.candidates}
    materials_by_path = {
        item.path: item for item in (*capsule.working_set, *capsule.task_context)
    }
    planned_items = () if plan is None else plan.items
    planned_item_ids = tuple(sorted(item.candidate_id for item in planned_items))
    materialized_item_ids = tuple(
        sorted(
            item.candidate_id
            for item in planned_items
            if (
                (material := materials_by_path.get(item.path)) is not None
                and material.source_sha256 == item.source_sha256
            )
        )
    )
    missing_item_ids = tuple(
        item for item in planned_item_ids if item not in set(materialized_item_ids)
    )
    planned_evidence_ids = tuple(
        sorted(
            {evidence_id for item in planned_items for evidence_id in item.evidence_ids}
        )
    )
    materialized_evidence_ids = tuple(
        sorted(
            {
                evidence_id
                for item in materials_by_path.values()
                for evidence_id in item.evidence_ids
            }
        )
    )
    planned_range_count = 0
    for item in planned_items:
        candidate = candidates_by_id.get(item.candidate_id)
        if candidate is None:
            continue
        known_evidence_ids = {
            evidence.evidence_id
            for evidence in candidate.evidence_ranges
            if evidence.evidence_id is not None
        }
        planned_range_count += len(set(item.evidence_ids) & known_evidence_ids)
    materialized_range_count = sum(
        len(item.ranges) for item in materials_by_path.values()
    )
    selected_plan_ids = tuple(item.candidate_id for item in planned_items)
    planned_ledger = build_coverage_ledger(
        retrieval.task,
        retrieval.candidates,
        selected_candidate_ids=selected_plan_ids,
        stage="plan",
        planner_bindings=() if plan is None else plan.role_bindings,
    )
    planned_role_ids = planned_ledger.covered_role_ids
    materialized_role_ids = materialization_ledger.covered_role_ids
    materialized_kinds = {
        item.role_id: item.kind for item in materialization_ledger.roles
    }
    missing_mandatory_role_ids = tuple(
        role_id
        for role_id in materialization_ledger.missing_role_ids
        if materialized_kinds[role_id] != "unknown"
    )
    reasons: set[CompilationReasonCode] = set()
    if declared_status == "insufficient":
        reasons.add("declared_insufficient")
    if not retrieval.candidates:
        reasons.add("empty_retrieval")
    if not capsule.task_context:
        reasons.add("empty_task_context")
    if plan_replaced:
        reasons.add("plan_replaced")
    if missing_item_ids:
        reasons.add("planned_item_unmaterialized")
        if plan_replaced:
            reasons.add("budget_excluded_mandatory_item")
    for item in planned_items:
        candidate = candidates_by_id.get(item.candidate_id)
        material = materials_by_path.get(item.path)
        if candidate is None or material is None or item.evidence_ids == ():
            continue
        if not set(item.evidence_ids) <= set(material.evidence_ids):
            reasons.add("planned_evidence_unmaterialized")
            continue
        if not _material_covers_planned_item(candidate, item, material):
            reasons.add("planned_range_unmaterialized")
    if set(planned_role_ids) - set(materialized_role_ids):
        reasons.add("planned_role_lost")
    if missing_mandatory_role_ids:
        reasons.add("mandatory_role_missing")
    effective_status: Literal["sufficient", "insufficient"] = (
        "sufficient"
        if declared_status == "sufficient" and capsule.task_context and not reasons
        else "insufficient"
    )
    return CompilationSufficiency(
        declared_status=declared_status,
        effective_status=effective_status,
        reason_codes=tuple(sorted(reasons)),
        planned_item_ids=planned_item_ids,
        materialized_item_ids=materialized_item_ids,
        missing_planned_item_ids=missing_item_ids,
        planned_evidence_ids=planned_evidence_ids,
        materialized_evidence_ids=materialized_evidence_ids,
        planned_range_count=planned_range_count,
        materialized_range_count=materialized_range_count,
        planned_role_ids=planned_role_ids,
        materialized_role_ids=materialized_role_ids,
        missing_mandatory_role_ids=missing_mandatory_role_ids,
        replacement_used=plan_replaced,
    )


def _capsule_ledger(
    task: str,
    candidates: tuple[CandidateCard, ...],
    capsule: ContextCapsule,
) -> CoverageLedger:
    """Build coverage from actual material identities, never from ranking order."""

    paths = {item.path for item in (*capsule.working_set, *capsule.task_context)}
    return build_coverage_ledger(
        task,
        candidates,
        selected_candidate_ids=tuple(
            item.candidate_id for item in candidates if item.path in paths
        ),
        stage="materialization",
    )


def _mandatory_role_ids(ledger: CoverageLedger) -> tuple[str, ...]:
    kinds = {item.role_id: item.kind for item in ledger.roles}
    return tuple(
        role_id for role_id in ledger.missing_role_ids if kinds[role_id] != "unknown"
    )


def _automatic_material_options(
    state: _CompilerState, candidate: CandidateCard
) -> tuple[CapsuleMaterial, ...]:
    """Return the cheapest verified map/slice choices for one candidate."""

    options = [
        material
        for material in (
            _materialize(state, candidate.path, RepresentationMode.MAP, candidate, ()),
            _materialize(
                state,
                candidate.path,
                RepresentationMode.SLICE,
                candidate,
                _automatic_slice_ranges(state, candidate),
            ),
        )
        if material is not None
    ]
    return tuple(
        sorted(
            options,
            key=lambda item: (item.token_count, _mode_rank(item.representation)),
        )
    )


def _supplemental_coverage_keys(candidate: CandidateCard) -> set[str]:
    """Only independent concepts and ranges belong to the second greedy pass."""

    return {
        *(f"concept:{value.casefold()}" for value in candidate.matched_concepts),
        *(f"symbol:{value.casefold()}" for value in candidate.matched_symbols),
        *(
            (f"exact:{candidate.exact_group}",)
            if candidate.exact_group != "approximate"
            else ()
        ),
        *(
            "range:"
            f"{item.path}:{item.source_range.start_line}:{item.source_range.end_line}:"
            f"{item.evidence_id or ''}"
            for item in candidate.evidence_ranges
        ),
        *(
            f"provenance:{value}"
            for value in candidate.provenance
            if value in {"current-diff", "working-set"}
        ),
        *(f"graph-endpoint:{item.path}" for item in candidate.graph_neighbors),
        *(
            f"graph-flow:{item.distance}:{kind}:{provenance}"
            for item in candidate.graph_neighbors
            for kind in item.relationship_kinds
            for provenance in item.provenance
        ),
    }


def _select_automatic_evidence(
    state: _CompilerState,
    task: str,
    capsule: ContextCapsule,
    candidates: tuple[CandidateCard, ...],
    eligible: list[CandidateCard],
    selected: list[CapsuleMaterial],
    selected_tokens: int,
    budget: ContextBudget,
    estimator: TokenEstimator,
    *,
    token_limit: int,
) -> tuple[list[CapsuleMaterial], int, ContextCapsule]:
    """Select coverage first, then independent detail, without evicting evidence.

    The two phases deliberately use ledgers instead of score-only gains: a map is
    selected for every still-coverable requested role and graph endpoint before
    duplicate concepts, ranges, or source-detail upgrades compete for budget.
    """

    all_ledger = build_coverage_ledger(task, candidates, stage="retrieval")
    remaining = list(eligible)

    def current_ledger() -> CoverageLedger:
        return _capsule_ledger(task, candidates, capsule)

    def append_for(predicate: object) -> bool:
        nonlocal capsule, selected_tokens
        before = current_ledger()
        choices: list[
            tuple[int, int, int, float, str, CandidateCard, CapsuleMaterial]
        ] = []
        for candidate in remaining:
            options = _automatic_material_options(state, candidate)
            prospective = (
                capsule.model_copy(
                    update={"task_context": tuple((*selected, options[0]))}
                )
                if options
                else None
            )
            if prospective is None:
                continue
            after = _capsule_ledger(task, candidates, prospective)
            if not callable(predicate) or not predicate(before, after, candidate):
                continue
            for material in options:
                proposed = capsule.model_copy(
                    update={"task_context": tuple((*selected, material))}
                )
                fits_limit = _fits(proposed, budget, estimator, token_limit=token_limit)
                fits_first_indivisible = not selected and _fits(
                    proposed, budget, estimator
                )
                if not fits_limit and not fits_first_indivisible:
                    continue
                choices.append(
                    (
                        material.token_count,
                        _mode_rank(material.representation),
                        _exact_group_rank(candidate.exact_group),
                        -candidate.score,
                        candidate.path,
                        candidate,
                        material,
                    )
                )
        if not choices:
            return False
        _, _, _, _, _, candidate, material = min(choices)
        selected.append(material)
        selected_tokens += material.token_count
        remaining.remove(candidate)
        capsule = capsule.model_copy(update={"task_context": tuple(selected)})
        return True

    # Mandatory roles and structural endpoints are unambiguously the first pass.
    for role_id in _mandatory_role_ids(all_ledger):
        while role_id not in set(current_ledger().covered_role_ids):
            if not append_for(
                lambda before, after, _candidate, expected=role_id: (
                    expected
                    in set(after.covered_role_ids) - set(before.covered_role_ids)
                )
            ):
                break
    for endpoint in all_ledger.covered_graph_endpoints:
        while endpoint not in set(current_ledger().covered_graph_endpoints):
            if not append_for(
                lambda before, after, _candidate, expected=endpoint: (
                    expected
                    in set(after.covered_graph_endpoints)
                    - set(before.covered_graph_endpoints)
                )
            ):
                break

    # Preserve one verified structural counterpart for a single-file exact
    # result.  This is deliberately a graph relation, not a filename heuristic;
    # mandatory endpoint coverage above remains responsible for wider flows.
    selected_paths = {item.path for item in selected}
    related_paths = {
        neighbor.path
        for candidate in candidates
        if candidate.path in selected_paths
        for neighbor in candidate.graph_neighbors
    } | {
        candidate.path
        for candidate in candidates
        if any(
            neighbor.path in selected_paths for neighbor in candidate.graph_neighbors
        )
    }
    if related_paths:
        append_for(lambda _before, _after, candidate: candidate.path in related_paths)
    if len(selected) == 1:
        append_for(
            lambda _before, _after, candidate: any(
                value.startswith("graph-") for value in candidate.provenance
            )
        )

    # Only after coverage is protected may unique concepts and ranges use space.
    covered_supplemental = {
        key
        for item in selected
        for candidate in candidates
        if candidate.path == item.path
        for key in _supplemental_coverage_keys(candidate)
    }
    while remaining:

        def adds_detail(
            _before: CoverageLedger, _after: CoverageLedger, candidate: CandidateCard
        ) -> bool:
            keys = _supplemental_coverage_keys(candidate)
            identity = {key for key in keys if key.startswith(("concept:", "symbol:"))}
            exact = {key for key in keys if key.startswith("exact:")}
            covered_identity = {
                key
                for key in covered_supplemental
                if key.startswith(("concept:", "symbol:"))
            }
            covered_exact = {
                key for key in covered_supplemental if key.startswith("exact:")
            }
            # A range is supplemental only for a file without duplicate task
            # identifiers. This makes range diversity real rather than a path
            # based way to repeat the same evidence across callers.
            return (
                bool(identity - covered_identity)
                or bool(exact - covered_exact)
                or (not identity and bool(keys - covered_supplemental))
            )

        if not append_for(adds_detail):
            break
        latest = selected[-1]
        selected_candidate = next(
            candidate for candidate in candidates if candidate.path == latest.path
        )
        covered_supplemental.update(_supplemental_coverage_keys(selected_candidate))
    return selected, selected_tokens, capsule


def _apply_greedy_upgrades(
    state: _CompilerState,
    task: str,
    capsule: ContextCapsule,
    candidates: tuple[CandidateCard, ...],
    working_lines: dict[str, tuple[SourceRange, ...]],
    budget: ContextBudget,
    estimator: TokenEstimator,
    *,
    token_limit: int,
    allow_indivisible_upgrade: bool,
) -> ContextCapsule:
    by_path = {item.path: item for item in candidates}
    current = {("working", item.path): item for item in capsule.working_set} | {
        ("task", item.path): item for item in capsule.task_context
    }
    current_tokens = estimator.count(_render_capsule(capsule))
    upgrade_limit = (
        budget.available_tokens
        if allow_indivisible_upgrade
        and current_tokens >= token_limit
        and not capsule.working_set
        and len(capsule.task_context) == 1
        else token_limit
    )
    while True:
        upgrades: list[tuple[float, str, str, RepresentationMode, CapsuleMaterial]] = []
        for (section, path), material in current.items():
            candidate = by_path.get(path)
            selected_others = tuple(
                by_path[other_path]
                for (other_section, other_path) in current
                if (other_section, other_path) != (section, path)
                and other_path in by_path
            )
            for mode in (
                RepresentationMode.SUMMARY,
                RepresentationMode.SLICE,
                RepresentationMode.FULL,
            ):
                if _mode_rank(mode) <= _mode_rank(material.representation):
                    continue
                if _representation_gain(candidate, material.representation, mode) <= 0:
                    continue
                ranges = (
                    working_lines.get(path, ())
                    if section == "working"
                    else _automatic_slice_ranges(state, candidate)
                )
                upgraded = _materialize(state, path, mode, candidate, ranges)
                if upgraded is None:
                    continue
                utility = _utility(candidate, mode, selected_others) - _utility(
                    candidate, material.representation, selected_others
                )
                ratio = utility / max(upgraded.token_count - material.token_count, 1)
                upgrades.append((ratio, section, path, mode, upgraded))
        applied = False
        for _, section, path, mode, upgraded in sorted(
            upgrades, key=lambda item: (-item[0], item[2], item[3].value)
        ):
            key = (section, path)
            existing = current[key]
            if _mode_rank(mode) <= _mode_rank(existing.representation):
                continue
            proposed = dict(current)
            proposed[key] = upgraded
            candidate_capsule = capsule.model_copy(
                update={
                    "working_set": tuple(
                        value
                        for (kind, _), value in sorted(proposed.items())
                        if kind == "working"
                    ),
                    "task_context": tuple(
                        value
                        for (kind, _), value in sorted(proposed.items())
                        if kind == "task"
                    ),
                }
            )
            # An upgrade must be monotonic for verified coverage.  It normally
            # replaces one material in place, but calculate from the ledger so
            # later representation changes cannot silently evict last coverage.
            before = _capsule_ledger(task, candidates, capsule)
            after = _capsule_ledger(task, candidates, candidate_capsule)
            if set(before.covered_role_ids) - set(after.covered_role_ids) or set(
                before.covered_graph_endpoints
            ) - set(after.covered_graph_endpoints):
                continue
            if _fits(
                candidate_capsule,
                budget,
                estimator,
                token_limit=upgrade_limit,
            ):
                current = proposed
                capsule = candidate_capsule
                applied = True
                break
        if not applied:
            break
    return capsule


def _representation_gain(
    candidate: CandidateCard | None,
    current: RepresentationMode,
    proposed: RepresentationMode,
) -> float:
    if candidate is None:
        return 1.0
    if proposed == RepresentationMode.SUMMARY:
        return 1.0 if candidate.matched_concepts else 0.0
    if proposed == RepresentationMode.SLICE:
        return 1.0 if candidate.evidence_ranges else 0.0
    if proposed == RepresentationMode.FULL:
        slice_cost = candidate.estimated_cost.slice
        compact_full = (
            candidate.estimated_cost.full <= AUTOMATIC_FULL_UPGRADE_MAX_TOKENS
            and (slice_cost is None or candidate.estimated_cost.full <= slice_cost)
        )
        return 1.0 if compact_full and current != RepresentationMode.FULL else 0.0
    return 0.0


def _planned_materialize(
    state: _CompilerState,
    candidate: CandidateCard,
    plan: PlannedEvidence | None,
) -> CapsuleMaterial | None:
    if plan is None:
        return _materialize(
            state, candidate.path, RepresentationMode.MAP, candidate, ()
        )
    options = _planned_materializations(state, candidate, plan)
    return options[0] if options else None


def _materialize_validated_plan(
    state: _CompilerState,
    capsule: ContextCapsule,
    plan: tuple[PlannedEvidence, ...],
    candidates: dict[str, CandidateCard],
    materialized_working_paths: set[str],
    budget: ContextBudget,
    estimator: TokenEstimator,
    *,
    token_limit: int,
    evidence_limit: int,
) -> tuple[CapsuleMaterial, ...] | None:
    """Materialize every planner item in order or reject the entire plan."""

    if not plan:
        return () if not candidates else None
    selected: list[CapsuleMaterial] = []
    selected_tokens = 0
    for item in plan:
        candidate = candidates.get(item.candidate_id)
        if (
            candidate is None
            or candidate.path != item.path
            or candidate.source_sha256 != item.source_sha256
            or not _is_automatic_candidate(candidate)
        ):
            return None
        if candidate.path in materialized_working_paths:
            continue
        accepted = None
        for material in _planned_materializations(state, candidate, item):
            if not _material_covers_planned_item(candidate, item, material):
                continue
            if selected_tokens + material.token_count > evidence_limit:
                continue
            proposed = capsule.model_copy(
                update={"task_context": tuple((*selected, material))}
            )
            if _fits(proposed, budget, estimator, token_limit=token_limit):
                accepted = material
                break
        if accepted is None:
            return None
        selected.append(accepted)
        selected_tokens += accepted.token_count
    return tuple(selected)


def _material_covers_planned_item(
    candidate: CandidateCard,
    item: PlannedEvidence,
    material: CapsuleMaterial,
) -> bool:
    if material.source_sha256 != item.source_sha256:
        return False
    if not item.evidence_ids:
        return True
    if not set(item.evidence_ids) <= set(material.evidence_ids):
        return False
    if material.representation == RepresentationMode.FULL:
        return True
    if material.representation != RepresentationMode.SLICE:
        return False
    expected_ranges = {
        evidence.evidence_id: evidence.source_range
        for evidence in candidate.evidence_ranges
        if evidence.evidence_id in set(item.evidence_ids)
    }
    return all(
        any(
            actual.start_line <= expected.start_line
            and actual.end_line >= expected.end_line
            for actual in material.ranges
        )
        for expected in expected_ranges.values()
    )


def _planned_materializations(
    state: _CompilerState,
    candidate: CandidateCard,
    plan: PlannedEvidence,
) -> tuple[CapsuleMaterial, ...]:
    """Return valid requested representation followed by safe downgrades."""

    selected = {
        item.evidence_id: item
        for item in candidate.evidence_ranges
        if item.evidence_id is not None
    }
    if any(evidence_id not in selected for evidence_id in plan.evidence_ids):
        return ()
    evidence_ids = tuple(
        evidence_id for evidence_id in plan.evidence_ids if evidence_id in selected
    )
    ranges = tuple(selected[evidence_id].source_range for evidence_id in evidence_ids)
    requested_mode = RepresentationMode(plan.representation)
    fallback_modes = {
        RepresentationMode.FULL: (
            RepresentationMode.FULL,
            RepresentationMode.SLICE,
            RepresentationMode.MAP,
        ),
        RepresentationMode.SLICE: (RepresentationMode.SLICE, RepresentationMode.MAP),
        RepresentationMode.SUMMARY: (
            RepresentationMode.SUMMARY,
            RepresentationMode.MAP,
        ),
        RepresentationMode.MAP: (RepresentationMode.MAP,),
    }[requested_mode]
    materials: list[CapsuleMaterial] = []
    for mode in fallback_modes:
        if mode == RepresentationMode.SLICE and not ranges:
            continue
        material = _materialize(
            state,
            candidate.path,
            mode,
            candidate,
            ranges if mode == RepresentationMode.SLICE else (),
            evidence_ids,
        )
        if material is not None and all(
            existing.representation != material.representation for existing in materials
        ):
            materials.append(material)
    return tuple(materials)


def _coverage_keys(candidate: CandidateCard) -> set[str]:
    keys = {
        *(f"symbol:{value.casefold()}" for value in candidate.matched_symbols),
        *(f"concept:{value.casefold()}" for value in candidate.matched_concepts),
        *(f"evidence:{item.strength}" for item in candidate.evidence_ranges),
        *(
            f"flow:{item.distance}:{kind}:{provenance}"
            for item in candidate.graph_neighbors
            for kind in item.relationship_kinds
            for provenance in item.provenance
        ),
    }
    if candidate.exact_group != "approximate":
        keys.add(f"exact:{candidate.exact_group}")
    if "current-diff" in candidate.provenance:
        keys.add(f"diff:{candidate.path}")
    if "working-set" in candidate.provenance:
        keys.add(f"working:{candidate.path}")
    return keys


def _automatic_slice_ranges(
    state: _CompilerState, candidate: CandidateCard | None
) -> tuple[SourceRange, ...]:
    if candidate is None:
        return ()
    ordered = sorted(
        candidate.evidence_ranges,
        key=lambda item: (
            -(item.source_range.end_line - item.source_range.start_line),
            item.source_range.start_line,
            item.source_range.end_line,
            item.evidence_id or "",
        ),
    )
    if not ordered:
        return ()
    primary = ordered[0]
    code_map = _code_map(state, candidate.path)
    declaration = next(
        (
            (
                symbol.declaration_range.start_line,
                (symbol.body_range or symbol.declaration_range).end_line,
            )
            for symbol in code_map.symbols
            if primary.source_range.start_line <= symbol.declaration_range.start_line
            and primary.source_range.end_line >= symbol.declaration_range.end_line
        ),
        None,
    )
    if declaration is None or declaration[1] - declaration[0] < 80:
        return tuple(item.source_range for item in ordered[:AUTOMATIC_SLICE_MAX_RANGES])
    matched = {value.casefold() for value in candidate.matched_symbols}

    def local_key(item: CandidateEvidenceRange) -> tuple[int, int, int, str]:
        source_range = item.source_range
        anchors_matched = any(
            relationship.kind in {"call", "reference", "import"}
            and relationship.target.observed_name is not None
            and relationship.target.observed_name.casefold() in matched
            and relationship.source_range.start_line <= source_range.end_line
            and relationship.source_range.end_line >= source_range.start_line
            for relationship in code_map.relationships
        )
        return (
            0 if anchors_matched else 1,
            source_range.start_line,
            source_range.end_line,
            item.evidence_id or "",
        )

    local = [
        item
        for item in sorted(ordered[1:], key=local_key)
        if declaration[0] <= item.source_range.start_line
        and item.source_range.end_line <= declaration[1]
    ]
    selected = (primary, *local[: AUTOMATIC_SLICE_MAX_RANGES - 1])
    return tuple(item.source_range for item in selected)


def _materialize(
    state: _CompilerState,
    path: str,
    mode: RepresentationMode,
    candidate: CandidateCard | None,
    requested_ranges: tuple[SourceRange, ...],
    requested_evidence_ids: tuple[str, ...] = (),
) -> CapsuleMaterial | None:
    code_map = _code_map(state, path)
    expected_sha = code_map.source_sha256
    if candidate is not None and candidate.source_sha256 != expected_sha:
        raise ContextFreshnessError(f"candidate source identity is stale: {path}")
    _assert_fresh(state, path, expected_sha)
    ranges: tuple[CapsuleRange, ...] = ()
    provenance = ["verified-structure"]
    if mode == RepresentationMode.MAP:
        content = _map_content(code_map, candidate)
    elif mode == RepresentationMode.SUMMARY:
        card = _card(state, path)
        if card is None:
            return None
        content = _summary_content(card)
        provenance.append("grounded-semantic-card")
    elif mode == RepresentationMode.SLICE:
        source, line_count = _source(state, path, expected_sha)
        evidence_ranges = requested_ranges or tuple(
            item.source_range
            for item in (() if candidate is None else candidate.evidence_ranges)
        )
        if not evidence_ranges:
            return None
        ranges = _slice_ranges(evidence_ranges, code_map, line_count)
        content = _slice_content(path, source, ranges)
        provenance.append("verified-source-ranges")
    else:
        source, line_count = _source(state, path, expected_sha)
        if path not in state.pinned_full and line_count > AUTOMATIC_FULL_FILE_MAX_LINES:
            return None
        content = source
        provenance.append("verified-full-source")
    relevance = 1.0 if candidate is None else candidate.score
    material_evidence_ids = requested_evidence_ids
    if not material_evidence_ids and candidate is not None:
        material_evidence_ids = tuple(
            item.evidence_id
            for item in candidate.evidence_ranges
            if item.evidence_id is not None
            and (
                mode != RepresentationMode.SLICE
                or any(
                    item.source_range.start_line <= value.end_line
                    and item.source_range.end_line >= value.start_line
                    for value in ranges
                )
            )
        )
    return CapsuleMaterial(
        path=path,
        source_sha256=expected_sha,
        representation=mode,
        content=content,
        ranges=ranges,
        evidence_ids=tuple(sorted(set(material_evidence_ids))),
        relevance=relevance,
        provenance=tuple(provenance),
        token_count=state.estimator.count(content),
    )


def _code_map(state: _CompilerState, path: str) -> FileCodeMap:
    if path not in state.code_maps:
        state.code_maps[path] = load_file_code_map(
            state.root, path, manifest=state.manifest
        )
    return state.code_maps[path]


def _card(state: _CompilerState, path: str) -> SemanticCard | None:
    if path not in state.cards:
        try:
            state.cards[path] = load_semantic_card(
                state.root, path, manifest=state.manifest
            )
        except (ValueError, IndexStorageError):
            state.cards[path] = None
    return state.cards[path]


def _assert_fresh(state: _CompilerState, path: str, expected_sha: str) -> ProjectFile:
    project_file = state.files.get(path)
    if project_file is None or project_file.sha256 != expected_sha:
        raise ContextFreshnessError(f"source changed after retrieval: {path}")
    return project_file


def _source(state: _CompilerState, path: str, expected_sha: str) -> tuple[str, int]:
    if path not in state.sources:
        project_file = _assert_fresh(state, path, expected_sha)
        selected = read_selected_text_file(
            state.snapshot,
            project_file,
            limits=ReaderLimits(
                max_files=1,
                max_source_bytes=max(project_file.size_bytes, 1),
                max_content_bytes=max(project_file.size_bytes * 2 + 4, 1),
            ),
        )
        state.sources[path] = (
            "".join(block.text for block in selected.blocks),
            selected.source_line_count,
        )
    return state.sources[path]


def _map_content(code_map: FileCodeMap, candidate: CandidateCard | None) -> str:
    lines = [f"{code_map.path} [{code_map.parse_status}]"]
    matched = (
        set()
        if candidate is None
        else {value.casefold() for value in candidate.matched_symbols}
    )
    evidence_ranges = (
        ()
        if candidate is None
        else tuple(item.source_range for item in candidate.evidence_ranges)
    )
    matched_symbols = [
        symbol
        for symbol in code_map.symbols
        if symbol.name.casefold() in matched
        or symbol.qualified_name.casefold() in matched
    ]
    enclosing_symbols = [
        symbol
        for symbol in code_map.symbols
        if any(
            evidence.start_line <= symbol.declaration_range.end_line
            and evidence.end_line >= symbol.declaration_range.start_line
            for evidence in evidence_ranges
        )
    ]
    selected = list(
        dict.fromkeys(
            (*matched_symbols, *enclosing_symbols[:AUTOMATIC_MAP_MAX_SYMBOLS])
        )
    )[:AUTOMATIC_MAP_MAX_SYMBOLS]
    if candidate is None:
        selected = list(code_map.symbols[:12])
    for symbol in selected:
        signature = symbol.signature or symbol.qualified_name
        lines.append(
            f"{symbol.kind.value} {symbol.qualified_name} :: {signature} "
            f"@ {symbol.declaration_range.start_line}-"
            f"{symbol.declaration_range.end_line}"
        )
    selected_ids = {item.symbol_id for item in selected}
    endpoints = []
    for relationship in code_map.relationships:
        if relationship.source_symbol_id not in selected_ids:
            continue
        target = relationship.target.file_path or relationship.target.observed_name
        if target:
            endpoints.append(
                f"{relationship.kind} -> {target} @ "
                f"{relationship.source_range.start_line}"
            )
    lines.extend(dict.fromkeys(endpoints[:8]))
    omitted = max(len(code_map.symbols) - len(selected), 0)
    if omitted:
        lines.append(f"declarations omitted={omitted}")
    return "\n".join(lines)


def _summary_content(card: SemanticCard) -> str:
    lines = [f"synopsis: {card.synopsis.text}"]
    for label, claims in (
        ("concept", card.concepts),
        ("responsibility", card.responsibilities),
        ("side-effect", card.side_effects),
    ):
        lines.extend(f"{label}: {claim.text}" for claim in claims)
    for symbol in card.key_symbols:
        if symbol.summary is not None:
            lines.append(f"key-symbol {symbol.qualified_name}: {symbol.summary}")
    for key, claims in card.profile_facts.items():
        lines.extend(f"{key}: {claim.text}" for claim in claims)
    return "\n".join(lines)


def _slice_ranges(
    evidence_ranges: tuple[SourceRange, ...],
    code_map: FileCodeMap,
    line_count: int,
) -> tuple[CapsuleRange, ...]:
    expanded: list[tuple[int, int]] = []
    for evidence in evidence_ranges:
        overlapping = []
        for symbol in code_map.symbols:
            declaration_start = symbol.declaration_range.start_line
            declaration_end = (symbol.body_range or symbol.declaration_range).end_line
            if (
                evidence.start_line <= declaration_end
                and evidence.end_line >= declaration_start
            ):
                overlapping.append((declaration_end - declaration_start, symbol))
        enclosing_symbol = (
            min(overlapping, key=lambda item: (item[0], item[1].symbol_id))[1]
            if overlapping
            else None
        )
        if enclosing_symbol is None:
            expanded.append(
                (
                    max(1, evidence.start_line - SLICE_CONTEXT_LINES),
                    min(line_count, evidence.end_line + SLICE_CONTEXT_LINES),
                )
            )
            continue
        declaration_start = enclosing_symbol.declaration_range.start_line
        declaration_end = (
            enclosing_symbol.body_range or enclosing_symbol.declaration_range
        ).end_line
        declaration_lines = declaration_end - declaration_start + 1
        if declaration_lines <= 80:
            expanded.append(
                (
                    max(1, declaration_start - SLICE_CONTEXT_LINES),
                    min(line_count, declaration_end + SLICE_CONTEXT_LINES),
                )
            )
            continue
        header_end = min(
            declaration_end,
            (
                enclosing_symbol.body_range.start_line
                if enclosing_symbol.body_range is not None
                else declaration_start
            )
            + 2,
        )
        expanded.append((declaration_start, header_end))
        evidence_lines = evidence.end_line - evidence.start_line + 1
        if evidence_lines <= 80:
            expanded.append(
                (
                    max(
                        declaration_start,
                        evidence.start_line - SLICE_CONTEXT_LINES,
                    ),
                    min(
                        declaration_end,
                        evidence.end_line + SLICE_CONTEXT_LINES,
                    ),
                )
            )
    merged: list[list[int]] = []
    for start, end in sorted(set(expanded)):
        if merged and start <= merged[-1][1] + SLICE_MERGE_GAP + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple(CapsuleRange(start_line=start, end_line=end) for start, end in merged)


def _slice_content(path: str, source: str, ranges: tuple[CapsuleRange, ...]) -> str:
    lines = source.splitlines(keepends=True)
    blocks = []
    for item in ranges:
        text = "".join(lines[item.start_line - 1 : item.end_line])
        blocks.append(f"{path}:{item.start_line}-{item.end_line}\n{text}")
    return "\n".join(blocks)


def _render_orientation(
    orientation: OrientationMap,
    token_limit: int,
    estimator: TokenEstimator,
    *,
    selected_paths: tuple[str, ...] = (),
    full: bool = True,
) -> str:
    file_lines = [
        f"{item.path} | module={item.module} | language={item.language or 'unknown'} | "
        f"lines={item.line_count} | symbols={item.symbol_count} | "
        f"centrality={item.centrality:.6f}"
        for item in orientation.files
    ]
    full_listing = "\n".join(file_lines)
    if full and estimator.count(full_listing) <= token_limit:
        return full_listing
    detailed_modules = [
        f"module {item.module} | files={len(item.files)} | "
        f"centrality={item.centrality:.6f}"
        for item in orientation.modules
    ]
    if estimator.count("\n".join(detailed_modules)) <= token_limit:
        lines = detailed_modules
    else:
        lines = []
        compact_modules = sorted(
            orientation.modules, key=lambda item: (-item.centrality, item.module)
        )
        for module_entry in compact_modules:
            line = f"module {module_entry.module} | files={len(module_entry.files)}"
            candidate = "\n".join((*lines, line))
            if estimator.count(candidate) <= token_limit:
                lines.append(line)
    central = sorted(orientation.files, key=lambda item: (-item.centrality, item.path))
    selected = set(selected_paths)
    for file_entry in sorted(
        (item for item in orientation.files if item.path in selected),
        key=lambda item: item.path,
    ):
        candidate = "\n".join((*lines, f"selected-file {file_entry.path}"))
        if estimator.count(candidate) <= token_limit:
            lines.append(f"selected-file {file_entry.path}")
    for file_entry in central:
        if not full or file_entry.path in selected:
            continue
        candidate = "\n".join((*lines, f"central-file {file_entry.path}"))
        if estimator.count(candidate) <= token_limit:
            lines.append(f"central-file {file_entry.path}")
    return "\n".join(lines)


def _compact_repository_map(orientation: OrientationMap, path: str) -> str:
    """Minimal verified envelope for a short exact-file capsule."""

    selected = next((item for item in orientation.files if item.path == path), None)
    if selected is None:
        return f"selected-file {path}"
    return (
        f"selected-file {selected.path} | module={selected.module} | "
        f"language={selected.language or 'unknown'} | lines={selected.line_count} | "
        f"symbols={selected.symbol_count}"
    )


def _compact_profile(
    capsule: ContextCapsule,
    orientation: OrientationMap,
    candidates: tuple[CandidateCard, ...],
) -> ContextCapsule | None:
    """Make a smaller exact-file envelope without weakening evidence rules."""

    if (
        capsule.working_set
        or capsule.git_context
        or len(capsule.task_context) != 1
        or capsule.interpretations
    ):
        return None
    material = capsule.task_context[0]
    candidate = next((item for item in candidates if item.path == material.path), None)
    if (
        candidate is None
        or candidate.exact_group == "approximate"
        or material.representation
        not in {RepresentationMode.SLICE, RepresentationMode.FULL}
    ):
        return None
    return capsule.model_copy(
        update={
            "repository_map": _compact_repository_map(orientation, material.path),
            "compact_profile": True,
        }
    )


def _git_text(value: str | object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        raise TypeError("git_diff must be text or a GitDiffContext-like value")
    return text


def _canonical_paths(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    from contextforge.core.validation import validate_portable_relative_path

    paths = tuple(validate_portable_relative_path(item) for item in values)
    if paths != tuple(sorted(set(paths))):
        raise ValueError(f"{label} must be unique and canonical")
    return paths


def _utility(
    candidate: CandidateCard | None,
    mode: RepresentationMode,
    selected: tuple[CandidateCard, ...] = (),
) -> float:
    relevance = 1.0 if candidate is None else max(candidate.score, 0.01)
    if candidate is None:
        evidence = facets = graph = exact = explicit = 0.0
    else:
        covered_concepts = {
            value.casefold() for item in selected for value in item.matched_concepts
        }
        covered_symbols = {
            value.casefold() for item in selected for value in item.matched_symbols
        }
        covered_ranges = {
            (
                value.path,
                value.source_range.start_line,
                value.source_range.end_line,
            )
            for item in selected
            for value in item.evidence_ranges
        }
        covered_neighbors = {
            value.path for item in selected for value in item.graph_neighbors
        }
        concepts = {value.casefold() for value in candidate.matched_concepts}
        symbols = {value.casefold() for value in candidate.matched_symbols}
        ranges = {
            (
                value.path,
                value.source_range.start_line,
                value.source_range.end_line,
            )
            for value in candidate.evidence_ranges
        }
        neighbors = {value.path for value in candidate.graph_neighbors}
        evidence = 0.30 * len(ranges - covered_ranges) + 0.05 * len(
            ranges & covered_ranges
        )
        facets = (
            0.20 * len(concepts - covered_concepts)
            + 0.03 * len(concepts & covered_concepts)
            + 0.12 * len(symbols - covered_symbols)
            + 0.02 * len(symbols & covered_symbols)
        )
        graph = 0.05 * len(neighbors - covered_neighbors) + 0.01 * len(
            neighbors & covered_neighbors
        )
        exact = {
            "exact_path": 0.50,
            "exact_qualified_symbol": 0.45,
            "exact_symbol": 0.40,
            "exact_source_identifier": 0.30,
            "approximate": 0.0,
        }[candidate.exact_group]
        explicit = 0.25 * ("current-diff" in candidate.provenance) + 1.0 * (
            "working-set" in candidate.provenance
        )
    multiplier = {
        RepresentationMode.MAP: 1.0,
        RepresentationMode.SUMMARY: 1.25,
        RepresentationMode.SLICE: 1.80,
        RepresentationMode.FULL: 2.0,
    }[mode]
    suggestion_bonus = (
        1.10
        if candidate is not None and candidate.suggested_representation == mode.value
        else 1.0
    )
    return (
        (relevance + evidence + facets + graph + exact + explicit)
        * multiplier
        * suggestion_bonus
    )


def _exact_group_rank(group: ExactGroup) -> int:
    return {
        "exact_path": 0,
        "exact_qualified_symbol": 1,
        "exact_symbol": 2,
        "exact_source_identifier": 3,
        "approximate": 4,
    }[group]


def _is_automatic_candidate(candidate: CandidateCard) -> bool:
    return (
        candidate.exact_group != "approximate"
        or candidate.bm25_score > 0
        or bool(
            candidate.matched_concepts
            or candidate.matched_symbols
            or candidate.evidence_ranges
        )
        or any(value.startswith("graph-") for value in candidate.provenance)
        or "current-diff" in candidate.provenance
        or "working-set" in candidate.provenance
    )


def _mode_rank(mode: RepresentationMode) -> int:
    return {
        RepresentationMode.MAP: 0,
        RepresentationMode.SUMMARY: 1,
        RepresentationMode.SLICE: 2,
        RepresentationMode.FULL: 3,
    }[mode]


def _fits(
    capsule: ContextCapsule,
    budget: ContextBudget,
    estimator: TokenEstimator,
    *,
    token_limit: int | None = None,
) -> bool:
    limit = (
        budget.available_tokens
        if token_limit is None
        else min(token_limit, budget.available_tokens)
    )
    return estimator.count(_render_capsule(capsule)) <= limit


def _render_capsule(capsule: ContextCapsule) -> str:
    escape = html.escape
    usage_rules = (
        (
            "    <rule>Repository maps verify indexed structure; quote or cite "
            "source only from exact lines in materialized SLICE or FULL "
            "sections.</rule>",
            "    <rule>Grounded summaries are evidence-linked interpretation, "
            "not source guarantees; report it as unknown when evidence does not "
            "establish a claim.</rule>",
        )
        if capsule.compact_profile
        else (
            (
                "    <rule>Repository maps verify indexed structure, not source "
                "contents, behavior, or guarantees.</rule>"
            ),
            (
                "    <rule>Quote or cite source only when its exact lines are present "
                "in a materialized SLICE or FULL section.</rule>"
            ),
            (
                "    <rule>Grounded summaries are evidence-linked interpretation, "
                "not source text or a guarantee.</rule>"
            ),
            (
                "    <rule>Interpretations are unverified selection rationale and "
                "are separate from verified facts and source.</rule>"
            ),
            (
                "    <rule>When supplied evidence does not establish a claim, "
                "report it as unknown.</rule>"
            ),
        )
    )
    lines = [
        '<contextforge schema_version="2">',
        (
            f'  <snapshot generation_id="{capsule.snapshot.generation_id}" '
            f'generation_kind="{capsule.snapshot.generation_kind}" '
            f'index_schema_version="{capsule.snapshot.index_schema_version}" '
            f'source_snapshot_digest="{capsule.snapshot.source_snapshot_digest}" />'
        ),
        f"  <task>{escape(capsule.task)}</task>",
        '  <usage_rules provenance="contextforge-verified">',
        *usage_rules,
        "  </usage_rules>",
        "  <verified_repository_map>",
        escape(capsule.repository_map),
        "  </verified_repository_map>",
        "  <working_set>",
    ]
    lines.extend(_render_material(item, "    ") for item in capsule.working_set)
    lines.extend(("  </working_set>", "  <task_context>"))
    lines.extend(_render_material(item, "    ") for item in capsule.task_context)
    lines.extend(
        (
            "  </task_context>",
            "  <git>",
            escape(capsule.git_context),
            "  </git>",
            "  <interpretations>",
        )
    )
    lines.extend(
        f"    <interpretation>{escape(item)}</interpretation>"
        for item in capsule.interpretations
    )
    lines.extend(("  </interpretations>", "</contextforge>"))
    return "\n".join(lines) + "\n"


def _render_material(item: CapsuleMaterial, indent: str) -> str:
    ranges = ",".join(f"{value.start_line}-{value.end_line}" for value in item.ranges)
    evidence_ids = ",".join(item.evidence_ids)
    return (
        f'{indent}<material path="{html.escape(item.path, quote=True)}" '
        f'representation="{item.representation.value}" ranges="{ranges}" '
        f'evidence_ids="{html.escape(evidence_ids, quote=True)}" '
        f'source_sha256="{item.source_sha256}">{html.escape(item.content)}'
        f"</material>"
    )


__all__ = [
    "AUTOMATIC_FULL_FILE_MAX_LINES",
    "AUTOMATIC_CONTEXT_SOFT_RATIO",
    "CONTEXT_CAPSULE_SCHEMA_VERSION",
    "SLICE_CONTEXT_LINES",
    "SLICE_MERGE_GAP",
    "CapsuleMaterial",
    "CapsuleRange",
    "CapsuleSnapshot",
    "CompiledContextCapsule",
    "ConservativeTokenEstimator",
    "ContextBudget",
    "ContextBudgetError",
    "ContextCapsule",
    "ContextCompilerError",
    "ContextFreshnessError",
    "RepresentationMode",
    "TokenEstimator",
    "compile_context_capsule",
]
