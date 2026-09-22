"""Small, source-free coverage diagnostics shared by public integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.intelligence.retrieval import CoverageLedger, RetrievalResult

if TYPE_CHECKING:
    from contextforge.context.capsule import CompilationSufficiency

CompilationReasonCode = Literal[
    "declared_insufficient",
    "empty_retrieval",
    "empty_task_context",
    "plan_replaced",
    "planned_item_unmaterialized",
    "planned_evidence_unmaterialized",
    "planned_range_unmaterialized",
    "budget_excluded_mandatory_item",
    "mandatory_role_missing",
    "planned_role_lost",
]


class PlannerActionDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_index: int = Field(ge=1)
    new_role_ids: tuple[str, ...] = ()
    new_identifier_count: int = Field(default=0, ge=0)
    new_evidence_count: int = Field(default=0, ge=0)
    new_graph_endpoint_count: int = Field(default=0, ge=0)

    @field_validator("new_role_ids")
    @classmethod
    def validate_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("action role IDs must be unique and canonical")
        return value


class EvidenceCoverageDiagnostics(BaseModel):
    """Closed IDs and reason codes, never model prose or source content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    plan_requested: bool
    plan_validated: bool
    compiler_materialized: bool
    declared_sufficiency: Literal["sufficient", "insufficient"] | None = None
    effective_sufficiency: Literal["sufficient", "insufficient"] | None = None
    covered_role_ids: tuple[str, ...] = ()
    missing_role_ids: tuple[str, ...] = ()
    materialization_downgrade_reason_codes: tuple[CompilationReasonCode, ...] = ()
    materialization_drop_reason_codes: tuple[CompilationReasonCode, ...] = ()
    effective_reason_codes: tuple[CompilationReasonCode, ...] = ()
    planner_action_deltas: tuple[PlannerActionDelta, ...] = ()
    compact_profile: bool | None = None

    @model_validator(mode="after")
    def validate_accounting(self) -> EvidenceCoverageDiagnostics:
        for values in (
            self.covered_role_ids,
            self.missing_role_ids,
            self.materialization_downgrade_reason_codes,
            self.materialization_drop_reason_codes,
            self.effective_reason_codes,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("coverage diagnostics must be unique and canonical")
        if set(self.covered_role_ids) & set(self.missing_role_ids):
            raise ValueError("covered and missing roles must be disjoint")
        if self.plan_validated and not self.plan_requested:
            raise ValueError("validated plan must have been requested")
        if self.compiler_materialized != (self.effective_sufficiency is not None):
            raise ValueError("effective sufficiency requires materialization")
        return self


_DOWNGRADE_CODES = frozenset(
    {
        "plan_replaced",
        "planned_evidence_unmaterialized",
        "planned_range_unmaterialized",
        "planned_role_lost",
        "budget_excluded_mandatory_item",
    }
)
_DROP_CODES = frozenset(
    {
        "empty_retrieval",
        "empty_task_context",
        "planned_item_unmaterialized",
        "mandatory_role_missing",
    }
)


def retrieval_evidence_diagnostics(
    result: RetrievalResult,
) -> EvidenceCoverageDiagnostics:
    """Summarize retrieval and validated planning without leaking planner text."""

    ledger = result.coverage_ledger
    history = result.coverage_history
    action_deltas: list[PlannerActionDelta] = []
    previous: CoverageLedger | None = None
    for current in history:
        if current.stage == "action" and previous is not None:
            old_evidence = {
                item.evidence_id for item in previous.ranges if item.evidence_id
            }
            new_evidence = {
                item.evidence_id for item in current.ranges if item.evidence_id
            }
            action_deltas.append(
                PlannerActionDelta(
                    action_index=len(action_deltas) + 1,
                    new_role_ids=tuple(
                        sorted(
                            set(current.covered_role_ids)
                            - set(previous.covered_role_ids)
                        )
                    ),
                    new_identifier_count=len(
                        set(current.unique_symbols) - set(previous.unique_symbols)
                    ),
                    new_evidence_count=len(new_evidence - old_evidence),
                    new_graph_endpoint_count=len(
                        set(current.covered_graph_endpoints)
                        - set(previous.covered_graph_endpoints)
                    ),
                )
            )
        previous = current
    return EvidenceCoverageDiagnostics(
        plan_requested=bool(
            result.plan_requested
            or result.evidence_plan is not None
            or result.planning_diagnostics is not None
        ),
        plan_validated=result.evidence_plan is not None,
        compiler_materialized=False,
        declared_sufficiency=(
            None if result.evidence_plan is None else result.evidence_plan.sufficiency
        ),
        covered_role_ids=() if ledger is None else ledger.covered_role_ids,
        missing_role_ids=() if ledger is None else ledger.missing_role_ids,
        planner_action_deltas=tuple(action_deltas),
    )


def compiled_evidence_diagnostics(
    result: RetrievalResult,
    ledger: CoverageLedger,
    sufficiency: CompilationSufficiency,
    *,
    compact_profile: bool,
) -> EvidenceCoverageDiagnostics:
    """Replace provisional coverage with compiler-verified coverage."""

    retrieval = retrieval_evidence_diagnostics(result)
    codes = sufficiency.reason_codes
    return retrieval.model_copy(
        update={
            "compiler_materialized": True,
            "declared_sufficiency": sufficiency.declared_status,
            "effective_sufficiency": sufficiency.effective_status,
            "covered_role_ids": ledger.covered_role_ids,
            "missing_role_ids": ledger.missing_role_ids,
            "materialization_downgrade_reason_codes": tuple(
                code for code in codes if code in _DOWNGRADE_CODES
            ),
            "materialization_drop_reason_codes": tuple(
                code for code in codes if code in _DROP_CODES
            ),
            "effective_reason_codes": codes,
            "compact_profile": compact_profile,
        }
    )
