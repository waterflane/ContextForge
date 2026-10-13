"""Evaluator-only reviewed support checks shared by material and answer audits."""

from contextforge.benchmarks.models import (
    BenchmarkAssertionSupport,
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
)

MaterialEvidence = tuple[tuple[BenchmarkSourceRange, tuple[str, ...]], ...]


def range_is_materialized(
    required: BenchmarkSourceRange, material: MaterialEvidence
) -> bool:
    intervals = sorted(
        (
            max(required.start_line, source.start_line),
            min(required.end_line, source.end_line),
        )
        for source, _ in material
        if source.path == required.path
        and source.start_line <= required.end_line
        and source.end_line >= required.start_line
    )
    next_line = required.start_line
    for start, end in intervals:
        if start > next_line:
            return False
        next_line = max(next_line, end + 1)
    return next_line > required.end_line


def support_is_materialized(
    support: BenchmarkAssertionSupport, material: MaterialEvidence
) -> bool:
    required = support.citation
    visible_ids = {
        identity
        for source, identities in material
        if source.path == required.path
        and source.start_line <= required.end_line
        and source.end_line >= required.start_line
        for identity in identities
    }
    return (
        range_is_materialized(required, material)
        and set(support.material_evidence_ids) <= visible_ids
    )


def assertion_support_is_materialized(
    assertion: BenchmarkExpectedAssertion, material: MaterialEvidence
) -> bool:
    return all(
        support_is_materialized(support, material) for support in assertion.support
    )


def public_assertions(
    assertions: tuple[BenchmarkExpectedAssertion, ...],
) -> list[dict[str, str]]:
    return [
        {"assertion_id": item.assertion_id, "description": item.description}
        for item in assertions
    ]
