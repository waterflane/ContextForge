from __future__ import annotations

from contextforge.intelligence import (
    CandidateCard,
    CandidateEvidenceRange,
    CandidateGraphNeighbor,
    RepresentationCosts,
    RoleEvidenceBinding,
    SourceRange,
    build_coverage_ledger,
)


def _candidate(
    path: str,
    *,
    exact: str = "approximate",
    neighbors: tuple[CandidateGraphNeighbor, ...] = (),
    symbols: tuple[str, ...] = (),
    concepts: tuple[str, ...] = (),
) -> CandidateCard:
    return CandidateCard(
        candidate_id=f"candidate:{path}",
        path=path,
        source_sha256="0" * 64,
        synopsis="structural",
        exact_group=exact,  # type: ignore[arg-type]
        score=99.0,
        bm25_score=0.0,
        matched_concepts=concepts,
        matched_symbols=symbols
        or (("publicEndpoint",) if exact != "approximate" else ()),
        evidence_ranges=(
            CandidateEvidenceRange(
                path=path,
                source_range=SourceRange(
                    start_line=1, start_column=0, end_line=2, end_column=0
                ),
                evidence_id=f"evidence:{path}",
                strength="verified",
            ),
        ),
        graph_neighbors=neighbors,
        provenance=("verified-structure",),
        estimated_cost=RepresentationCosts(map=1, full=1),
    )


def _neighbor(path: str, *kinds: str) -> CandidateGraphNeighbor:
    return CandidateGraphNeighbor(
        path=path,
        distance=1,
        relationship_kinds=tuple(sorted(kinds)),
        provenance=("verified",),
    )


def test_ledger_covers_startup_and_multiple_roles_in_one_file() -> None:
    main = _candidate("src/main.py", exact="exact_symbol", symbols=("startup",))

    ledger = build_coverage_ledger(
        "Review startup implementation and public API.", (main,)
    )

    assert {"entrypoint", "implementation", "public_api"} <= set(
        ledger.covered_role_ids
    )
    assert {binding.candidate_id for binding in ledger.bindings} == {main.candidate_id}


def test_ledger_covers_implementation_test_config_docs_and_api() -> None:
    implementation = _candidate("src/service.py")
    test = _candidate("tests/test_service.py")
    config = _candidate("config/settings.toml")
    docs = _candidate("docs/api.md", exact="exact_symbol")

    ledger = build_coverage_ledger(
        (
            "Review implementation, regression tests, configuration, documentation, "
            "and public API."
        ),
        (implementation, test, config, docs),
    )

    assert {
        "implementation",
        "test",
        "configuration",
        "documentation",
        "public_api",
    } <= set(ledger.covered_role_ids)


def test_cross_file_ledger_marks_unmaterialized_client_and_provider_missing() -> None:
    client = _candidate(
        "src/client.js", neighbors=(_neighbor("src/server.js", "call"),)
    )
    server = _candidate(
        "src/server.js",
        neighbors=(
            _neighbor("src/client.js", "call"),
            _neighbor("src/provider.js", "call"),
        ),
    )
    provider = _candidate(
        "src/provider.js", neighbors=(_neighbor("src/server.js", "call"),)
    )
    candidates = (client, server, provider)

    complete = build_coverage_ledger(
        "Trace client server provider request flow.", candidates
    )
    server_only = build_coverage_ledger(
        "Trace client server provider request flow.",
        candidates,
        selected_candidate_ids=(server.candidate_id,),
        stage="materialization",
    )

    assert {"caller", "callee"} <= {role.kind for role in complete.roles}
    assert set(server_only.missing_graph_endpoints) == {
        "src/client.js",
        "src/provider.js",
    }
    assert server_only.missing_role_ids


def test_centrality_without_structural_hint_does_not_cover_entrypoint() -> None:
    central_only = _candidate("src/utility.py")

    ledger = build_coverage_ledger("Review startup.", (central_only,))

    assert "entrypoint" in ledger.missing_role_ids
    assert not ledger.bindings


def test_unknown_planner_binding_is_dropped_and_serialization_is_deterministic() -> (
    None
):
    candidate = _candidate(
        "src/service.py", symbols=("service",), concepts=("request flow",)
    )
    unknown = RoleEvidenceBinding(
        role_id="unknown",
        candidate_id=candidate.candidate_id,
        evidence_ids=("evidence:src/service.py",),
        source="planner",
    )

    first = build_coverage_ledger("service", (candidate,), planner_bindings=(unknown,))
    second = build_coverage_ledger("service", (candidate,), planner_bindings=(unknown,))

    assert first.bindings == ()
    assert first.unique_symbols == ("service",)
    assert first.concepts == ("request flow", "service")
    assert first.ranges
    assert first.model_dump_json() == second.model_dump_json()
