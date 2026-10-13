from __future__ import annotations

from contextforge.intelligence import (
    CandidateCard,
    CandidateEvidenceRange,
    CandidateGraphNeighbor,
    RepresentationCosts,
    RoleEvidenceBinding,
    SourceRange,
    TaskEvidenceRoleKind,
    build_coverage_ledger,
)


def _candidate(
    path: str,
    *,
    exact: str = "approximate",
    neighbors: tuple[CandidateGraphNeighbor, ...] = (),
    symbols: tuple[str, ...] = (),
    concepts: tuple[str, ...] = (),
    roles: tuple[TaskEvidenceRoleKind, ...] = (),
) -> CandidateCard:
    return CandidateCard(
        candidate_id=f"candidate:{path}",
        path=path,
        source_sha256="0" * 64,
        synopsis="structural",
        exact_group=exact,  # type: ignore[arg-type]
        score=99.0,
        bm25_score=0.0,
        structural_roles=roles,
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
        direction="both",
    )


def test_ledger_covers_startup_and_multiple_roles_in_one_file() -> None:
    main = _candidate(
        "src/main.py",
        exact="exact_symbol",
        symbols=("startup",),
        roles=("entrypoint", "public_api"),
    )

    ledger = build_coverage_ledger(
        "Review startup implementation and public API.", (main,)
    )

    assert {"entrypoint", "implementation", "public_api"} <= set(
        ledger.covered_role_ids
    )
    assert {binding.candidate_id for binding in ledger.bindings} == {main.candidate_id}


def test_unrelated_file_categories_do_not_close_task_roles() -> None:
    implementation = _candidate("src/service.py")
    test = _candidate("tests/test_service.py")
    config = _candidate("config/settings.toml")
    docs = _candidate("docs/api.md", exact="exact_symbol", roles=("public_api",))

    ledger = build_coverage_ledger(
        (
            "Review implementation, regression tests, configuration, documentation, "
            "and public API."
        ),
        (implementation, test, config, docs),
    )

    assert {
        "test",
        "configuration",
    } <= set(ledger.missing_role_ids)
    assert {"documentation", "public_api"} <= set(ledger.covered_role_ids)


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
    assert server_only.missing_requirement_ids


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


def test_import_and_mixed_provenance_do_not_prove_calls() -> None:
    imported = _candidate(
        "src/client.py", neighbors=(_neighbor("src/server.py", "import"),)
    )
    mixed = _neighbor("src/server.py", "call", "import").model_copy(
        update={
            "verified_relationship_kinds": ("import",),
            "provenance": ("verified", "model-inferred"),
        }
    )
    ledger = build_coverage_ledger(
        "Trace implementation flow",
        (imported, _candidate("src/other.py", neighbors=(mixed,))),
    )
    assert {"caller", "callee"} <= set(ledger.missing_role_ids)


def test_direction_and_source_facts_determine_roles() -> None:
    incoming = _neighbor("src/caller.py", "call").model_copy(
        update={"direction": "incoming"}
    )
    candidate = _candidate(
        "src/service.py", neighbors=(incoming,), roles=("configuration",)
    )
    ledger = build_coverage_ledger("Trace service configuration flow", (candidate,))
    assert {"configuration", "implementation"} <= set(ledger.covered_role_ids)
    assert {"caller", "callee"} <= set(ledger.missing_role_ids)


def test_requirements_remain_frozen_when_unrelated_neighbors_are_added() -> None:
    main = _candidate("src/main.py", exact="exact_symbol", symbols=("start",))
    first = build_coverage_ledger("Review implementation and tests", (main,))
    extra = _candidate(
        "src/index.py", neighbors=(_neighbor("src/random.py", "import"),)
    )
    second = build_coverage_ledger(
        "Review implementation and tests",
        (main, extra),
        requirements=first.requirements,
    )
    assert first.requirements == second.requirements
    assert second.roles == first.roles
    assert not second.missing_graph_endpoints


def test_matched_symbol_alone_does_not_prove_entrypoint_or_api() -> None:
    symbol = _candidate("src/service.py", exact="exact_symbol", symbols=("startup",))
    ledger = build_coverage_ledger("Review startup API", (symbol,))
    assert {"entrypoint", "public_api"} <= set(ledger.missing_role_ids)


def test_each_explicit_implementation_anchor_needs_its_own_test_binding() -> None:
    first = _candidate(
        "src/first.py",
        exact="exact_symbol",
        symbols=("first_job",),
        neighbors=(_neighbor("tests/test_first.py", "call"),),
    )
    second = _candidate(
        "src/second.py",
        exact="exact_symbol",
        symbols=("second_job",),
    )
    first_test = _candidate("tests/test_first.py")
    task = "Review `first_job` and `second_job` implementation and tests"
    incomplete = build_coverage_ledger(task, (first, second, first_test))
    assert "test" in incomplete.missing_role_ids
    assert any(b.role_id == "test" for b in incomplete.bindings)
    second = second.model_copy(
        update={
            "graph_neighbors": (_neighbor("tests/test_second.py", "call"),),
        }
    )
    complete = build_coverage_ledger(
        task,
        (first, second, first_test, _candidate("tests/test_second.py")),
    )
    assert "test" in complete.covered_role_ids


def test_unrelated_call_pair_cannot_close_explicit_anchor_flow() -> None:
    anchor = _candidate("src/jobs.py", exact="exact_symbol", symbols=("execute_job",))
    caller = _candidate("src/other.py", neighbors=(_neighbor("src/leaf.py", "call"),))
    callee = _candidate("src/leaf.py", neighbors=(_neighbor("src/other.py", "call"),))
    ledger = build_coverage_ledger(
        "Trace execute_job implementation flow", (anchor, caller, callee)
    )
    assert "implementation" in ledger.covered_role_ids
    assert {"caller", "callee"} <= set(ledger.missing_role_ids)
