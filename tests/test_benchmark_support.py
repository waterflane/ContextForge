import pytest

from contextforge.benchmarks.models import (
    BenchmarkAssertionSupport,
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
)
from contextforge.benchmarks.support import assertion_support_is_materialized


@pytest.mark.parametrize(
    "damage", ["none", "first", "second", "partial", "foreign-range", "stale-id"]
)
def test_every_reviewed_support_needs_actual_ranges_and_ids(damage: str) -> None:
    first = BenchmarkSourceRange(path="flow.py", start_line=2, end_line=4)
    second = BenchmarkSourceRange(path="flow.py", start_line=20, end_line=22)
    assertion = BenchmarkExpectedAssertion(
        assertion_id="flow",
        description="Both source operations are present",
        support=(
            BenchmarkAssertionSupport(citation=first, material_evidence_ids=("first",)),
            BenchmarkAssertionSupport(
                citation=second, material_evidence_ids=("second",)
            ),
        ),
    )
    material: list[tuple[BenchmarkSourceRange, tuple[str, ...]]] = [
        (first, ("first",)),
        (second, ("second",)),
    ]
    if damage == "first":
        material.pop(0)
    elif damage == "second":
        material.pop()
    elif damage == "partial":
        material[1] = (second.model_copy(update={"end_line": 21}), ("second",))
    elif damage == "foreign-range":
        material = [(first, ("first", "second")), (second, ())]
    elif damage == "stale-id":
        material[1] = (second, ("old-sha-second",))
    assert assertion_support_is_materialized(assertion, tuple(material)) == (
        damage == "none"
    )


def test_adjacent_source_blocks_cover_support_but_a_gap_does_not() -> None:
    required = BenchmarkSourceRange(path="flow.py", start_line=2, end_line=4)
    assertion = BenchmarkExpectedAssertion(
        assertion_id="flow",
        description="The complete source operation is present",
        support=(
            BenchmarkAssertionSupport(
                citation=required, material_evidence_ids=("body",)
            ),
        ),
    )
    first = required.model_copy(update={"end_line": 2})
    second = required.model_copy(update={"start_line": 3})
    assert assertion_support_is_materialized(
        assertion, ((first, ("body",)), (second, ("body",)))
    )
    assert not assertion_support_is_materialized(
        assertion,
        ((first, ("body",)), (second.model_copy(update={"start_line": 4}), ("body",))),
    )
