"""Public diagnostics contain verified accounting, not planner prose."""

import pytest
from pydantic import ValidationError

from contextforge.context.evidence_diagnostics import EvidenceCoverageDiagnostics
from contextforge.intelligence.retrieval import (
    CoverageLedger,
    PlanningDiagnostics,
    RetrievalResult,
)


def test_retrieval_action_deltas_are_deterministic_and_source_free() -> None:
    before = CoverageLedger(stage="retrieval", roles=())
    after = CoverageLedger(stage="action", roles=(), unique_symbols=("Run",))
    result = RetrievalResult(
        source_snapshot_digest="0" * 64,
        generation_id="1" * 64,
        task="change Run",
        candidates=(),
        diagnostics=("arbitrary provider text SECRET",),
        planning_diagnostics=PlanningDiagnostics(
            mode="auto",
            status="fallback",
            messages=("arbitrary provider text SECRET",),
        ),
        coverage_ledger=after,
        coverage_history=(before, after),
        plan_requested=True,
    )

    diagnostics = result.evidence_diagnostics
    assert diagnostics.plan_requested
    assert not diagnostics.plan_validated
    assert not diagnostics.compiler_materialized
    assert diagnostics.effective_sufficiency is None
    assert diagnostics.planner_action_deltas[0].new_identifier_count == 1
    assert diagnostics.planner_action_deltas[0].new_role_ids == ()
    assert "Run" not in diagnostics.model_dump_json()
    assert "SECRET" not in diagnostics.model_dump_json()
    assert (
        result.evidence_diagnostics.model_dump_json() == diagnostics.model_dump_json()
    )


def test_portable_diagnostics_reject_free_text_reason_codes() -> None:
    with pytest.raises(ValidationError):
        EvidenceCoverageDiagnostics.model_validate(
            {
                "plan_requested": False,
                "plan_validated": False,
                "compiler_materialized": True,
                "effective_sufficiency": "insufficient",
                "effective_reason_codes": ["provider says SECRET"],
            }
        )
