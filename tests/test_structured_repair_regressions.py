from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typer.testing import CliRunner

import contextforge.cli.context_commands as context_cli
import contextforge.models.providers as provider_module
from contextforge.application import suggest_repository_context
from contextforge.cli.main import app
from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryCancelledError,
    DiscoveryMode,
    DiscoveryProtocolError,
    DiscoveryRequest,
    DiscoverySession,
    DiscoverySourceChangedError,
    FinalContextSelection,
)
from contextforge.intelligence import acquire_index_lock, build_structural_index
from contextforge.logging import clear_recent_records, recent_records
from contextforge.models import (
    FakeModelProvider,
    FakeScript,
    InvalidFieldValueIssue,
    ModelRequest,
    ProviderCancelledError,
    ProviderConfiguration,
    StructuredResponseError,
    WrongFieldTypeIssue,
    provider_error_details,
    validate_structured_response,
)
from contextforge.repositories import ProjectSnapshot, scan_repository


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _NestedItem(_Closed):
    candidate_id: str


class _GatewayResult(_Closed):
    schema_version: Literal[1] = 1
    content: str = Field(min_length=1, max_length=100)
    selected_candidates: tuple[_NestedItem, ...] = Field(default=(), max_length=3)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


def _configuration(**updates: object) -> ProviderConfiguration:
    return ProviderConfiguration.model_validate(
        {
            "provider_id": "fake",
            "endpoint": "fake://offline",
            "model_id": "structured-repair-regression",
            "retry_limit": 0,
            "context_window": 98_304,
            **updates,
        }
    )


def _request(*, facts: dict[str, Any] | None = None) -> ModelRequest:
    return ModelRequest(
        operation_id="repair-regression",
        purpose="repair-regression",
        system_instructions="Return the closed regression response.",
        analysis_task="Select bounded context for the test task.",
        trusted_code_map_facts=facts or {},
        untrusted_sources=(),
        response_model=_GatewayResult,
        max_output_tokens=256,
    )


def _valid() -> str:
    return json.dumps({"schema_version": 1, "content": "ok"})


def _invalid(*, padding: int = 0) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "content": [],
            "selected_candidates": [],
            "padding": "x" * padding,
        }
    )


def _indexed_snapshot(root: Path, count: int = 26) -> ProjectSnapshot:
    for index in range(count):
        path = root / "src" / f"candidate_{index:02d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def candidate_{index:02d}():\n    return {index}\n")
    snapshot = scan_repository(root)
    with acquire_index_lock(root, "candidate-regression") as lock:
        build_structural_index(snapshot, lock)
    return snapshot


def _selection_response(request: ModelRequest, *, finalize_only: bool = False) -> str:
    del finalize_only
    records = cast(list[dict[str, Any]], request.trusted_code_map_facts["candidates"])
    return json.dumps(
        {
            "schema_version": 1,
            "candidate_ids": [records[0]["candidate_id"]],
            "summary": "Selected ranked context.",
        }
    )


def test_wrong_field_type_rejects_equal_types() -> None:
    with pytest.raises(ValidationError, match="distinct JSON types"):
        WrongFieldTypeIssue(
            path="/content",
            expected_type="object",
            actual_type="object",
            reason="not a type mismatch",
        )


def test_invalid_field_value_preserves_constraint_not_equal_types() -> None:
    with pytest.raises(StructuredResponseError) as captured:
        validate_structured_response(
            '{"schema_version":1,"content":"ok","confidence":2}',
            request=_request(),
            max_response_bytes=1_000,
        )
    issue = captured.value.issues[0]
    assert isinstance(issue, InvalidFieldValueIssue)
    assert issue.path == "/confidence"
    assert issue.constraint == "less_than_equal"
    assert "maximum=1.0" in issue.expected_constraint
    assert issue.actual_value_kind == "integer"
    assert "expected_type" not in issue.model_dump()


def test_nested_paths_survive_gateway_exception_diagnostic_and_rendering() -> None:
    payload = (
        '{"schema_version":1,"content":"ok",'
        '"selected_candidates":[{"candidate_id":[]}]} '
    )
    with pytest.raises(StructuredResponseError) as captured:
        validate_structured_response(
            payload, request=_request(), max_response_bytes=1_000
        )
    issue = captured.value.issues[0]
    code, rendered = provider_error_details(captured.value)
    assert issue.path == "/selected_candidates/0/candidate_id"
    assert code == "wrong_field_type"
    assert "/selected_candidates/0/candidate_id" in rendered


def test_nested_missing_fields_include_their_json_paths() -> None:
    payload = '{"schema_version":1,"content":"ok","selected_candidates":[{}]}'
    with pytest.raises(StructuredResponseError) as captured:
        validate_structured_response(
            payload, request=_request(), max_response_bytes=1_000
        )
    issue = captured.value.issues[0]
    assert issue.code == "missing_required_field"
    assert issue.path == "/selected_candidates/0/candidate_id"
    assert issue.path in provider_error_details(captured.value)[1]


def test_empty_external_issue_details_are_not_invented() -> None:
    def reject(_: BaseModel) -> None:
        raise ValueError("validator supplied no field location")

    request = _request()
    request = ModelRequest(
        operation_id=request.operation_id,
        purpose=request.purpose,
        system_instructions=request.system_instructions,
        analysis_task=request.analysis_task,
        trusted_code_map_facts={},
        untrusted_sources=(),
        response_model=request.response_model,
        response_validator=reject,
    )
    with pytest.raises(StructuredResponseError) as captured:
        validate_structured_response(
            _valid(), request=request, max_response_bytes=1_000
        )
    assert captured.value.issues[0].code == "validation_issue_details_missing"
    assert captured.value.issues[0].path is None
    assert (
        "expected=object actual=object" not in provider_error_details(captured.value)[1]
    )


def test_twenty_six_candidates_are_bounded_serialized_and_stable(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path)
    requests: list[ModelRequest] = []

    def responder(request: ModelRequest, index: int) -> str:
        requests.append(request)
        return _selection_response(request, finalize_only=index > 0)

    clear_recent_records()
    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            FakeModelProvider(_configuration(), responder=responder),
            DiscoveryRequest(task="candidate 17", mode=DiscoveryMode.INDEXED),
        )
    )
    candidates = cast(
        list[dict[str, Any]], requests[0].trusted_code_map_facts["candidates"]
    )
    assert set(requests[0].response_schema["properties"]) == {
        "schema_version",
        "candidate_ids",
        "summary",
    }
    assert set(requests[0].response_schema["required"]) == {
        "schema_version",
        "candidate_ids",
        "summary",
    }
    assert "actions" not in requests[0].response_schema["properties"]
    assert "candidate_ids is required" in requests[0].system_instructions
    assert "required candidate_ids" in requests[0].analysis_task
    assert len(candidates) == 10
    assert len({item["candidate_id"] for item in candidates}) == 10
    assert all(item["candidate_id"].startswith("c-") for item in candidates)
    assembled = next(
        item
        for item in recent_records()
        if item.event == "context_suggestion.request_assembled"
    )
    expected_tokens = (
        len(
            json.dumps(
                candidates,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        )
        + 2
    ) // 3
    assert assembled.data["ranked_candidate_count"] == 26
    assert assembled.data["preselected_candidate_count"] == 10
    assert assembled.data["serialized_candidate_count"] == len(candidates)
    assert assembled.data["estimated_serialized_index_tokens"] == expected_tokens
    assert result.final_selection is not None


def test_observed_path_instead_of_candidate_id_reports_exact_constraint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "contextforge" / "models" / "providers.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "observed-response") as lock:
        build_structural_index(snapshot, lock)
    fixture = (
        Path(__file__).parent / "fixtures" / "observed_invalid_candidate_response.json"
    ).read_text(encoding="utf-8")
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=5), scripts=[fixture] * 6
    )
    clear_recent_records()
    with pytest.raises(DiscoveryProtocolError) as captured:
        asyncio.run(
            suggest_repository_context(
                snapshot,
                provider,
                DiscoveryRequest(
                    task="repair gateway",
                    mode=DiscoveryMode.INDEXED,
                    strict=True,
                ),
            )
        )
    assert provider.call_count == 6
    assert captured.value.run_record.failure_code == "unknown_candidate_id"
    failed = next(
        item
        for item in reversed(recent_records())
        if item.event == "response.validation.failed"
    )
    assert failed.data["validation_issue_paths"] == ["/candidate_ids/0"]
    assert failed.data["failed_constraints"] == ["known_candidate_id"]
    session_failure = next(
        item for item in recent_records() if item.event == "context_suggestion.failed"
    )
    assert session_failure.data["failing_stage"] == "semantic_reference_validation"
    assert session_failure.data["provider_request_dispatched"] is True


def test_missing_candidate_ids_exhaustion_finishes_at_response_validation(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=1)
    missing_candidate_ids = json.dumps(
        {"schema_version": 1, "summary": "missing required candidate IDs"}
    )
    clear_recent_records()
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=1),
        scripts=[missing_candidate_ids] * 2,
    )
    with pytest.raises(DiscoveryProtocolError):
        asyncio.run(
            suggest_repository_context(
                snapshot,
                provider,
                DiscoveryRequest(
                    task="confidence",
                    mode=DiscoveryMode.INDEXED,
                    strict=True,
                ),
            )
        )
    assert provider.call_count == 2
    failed = next(
        item for item in recent_records() if item.event == "context_suggestion.failed"
    )
    assert failed.data["failing_stage"] == "response_validation"
    assert failed.data["provider_request_dispatched"] is True
    validation = next(
        item
        for item in reversed(recent_records())
        if item.event == "response.validation.failed"
    )
    assert validation.data["validation_issue_paths"] == ["/candidate_ids"]
    assert validation.data["failed_constraints"] == ["required"]


def test_indexed_compact_selection_succeeds_on_one_repair(tmp_path: Path) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=2)
    selected_id: str | None = None

    def responder(request: ModelRequest, index: int) -> str:
        nonlocal selected_id
        if index == 0:
            records = cast(
                list[dict[str, Any]], request.trusted_code_map_facts["candidates"]
            )
            selected_id = cast(str, records[0]["candidate_id"])
            return json.dumps({"schema_version": 1, "summary": "candidate IDs omitted"})
        assert selected_id is not None
        return json.dumps(
            {
                "schema_version": 1,
                "candidate_ids": [selected_id],
                "summary": "Selected ranked context after one repair.",
            }
        )

    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=1), responder=responder
    )
    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            provider,
            DiscoveryRequest(task="candidate", mode=DiscoveryMode.INDEXED),
        )
    )
    assert provider.call_count == 2
    assert result.final_selection is not None
    assert result.final_selection.provenance == "model"
    assert result.budget_usage.model_calls == 1
    assert result.budget_usage.provider_http_calls == 2


@pytest.mark.parametrize("mode", [DiscoveryMode.INDEXED, DiscoveryMode.HYBRID])
def test_indexed_and_hybrid_share_compact_selection_contract(
    tmp_path: Path, mode: DiscoveryMode
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=2)
    requests: list[ModelRequest] = []

    def responder(request: ModelRequest, _: int) -> str:
        requests.append(request)
        return _selection_response(request)

    provider = FakeModelProvider(_configuration(), responder=responder)
    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            provider,
            DiscoveryRequest(task="candidate", mode=mode),
        )
    )

    assert provider.call_count == 1
    assert result.budget_usage.model_calls == 1
    assert set(requests[0].response_schema["properties"]) == {
        "schema_version",
        "candidate_ids",
        "summary",
    }
    assert "actions" not in requests[0].response_schema["properties"]
    assert "tool_schemas" not in requests[0].trusted_code_map_facts


def test_hybrid_repeated_validation_fingerprint_stops_after_one_generation(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=2)
    invalid = json.dumps({"schema_version": 1, "summary": "candidate IDs omitted"})
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=2), scripts=[invalid] * 10
    )

    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            provider,
            DiscoveryRequest(
                task="candidate",
                mode=DiscoveryMode.HYBRID,
                budget=DiscoveryBudget(max_model_calls=100),
            ),
        )
    )

    assert result.final_selection is not None
    assert result.final_selection.provenance == "deterministic_fallback"
    assert result.budget_usage.model_calls == 1
    assert result.budget_usage.provider_http_calls == 3
    assert provider.call_count == 3


def test_hybrid_reuses_selected_source_read_during_deterministic_review(
    tmp_path: Path,
) -> None:
    _indexed_snapshot(tmp_path, count=2)
    changed = tmp_path / "src" / "candidate_01.py"
    changed.write_text("CHANGED = True\n", encoding="utf-8")
    current = scan_repository(tmp_path)
    clear_recent_records()

    result = asyncio.run(
        suggest_repository_context(
            current,
            FakeModelProvider(
                _configuration(),
                responder=lambda request, _: _selection_response(request),
            ),
            DiscoveryRequest(task="candidate 00", mode=DiscoveryMode.HYBRID),
        )
    )

    verification = [
        item
        for item in recent_records()
        if item.event == "context_suggestion.source_verification_completed"
    ]
    assert len(verification) == 2
    assert result.final_selection is not None
    assert result.budget_usage.files_read == 1 + len(result.final_selection.selected)
    assert all(
        item.data["read_file_count"] == len(result.final_selection.selected)
        for item in verification
    )


def test_hybrid_provider_cancellation_is_reported_as_cancelled(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=1)
    cancellation = asyncio.Event()
    provider = FakeModelProvider(
        _configuration(),
        scripts=[
            FakeScript(
                ProviderCancelledError("cancelled"),
                delay_seconds=0.01,
            )
        ],
    )
    clear_recent_records()

    session = DiscoverySession(
        snapshot,
        provider,
        DiscoveryRequest(task="candidate", mode=DiscoveryMode.HYBRID),
        cancellation=cancellation,
    )
    with pytest.raises(DiscoveryCancelledError) as captured:
        asyncio.run(session.run())

    assert captured.value.run_record.status == "cancelled"
    failure = next(
        item for item in recent_records() if item.event == "context_suggestion.failed"
    )
    assert failure.status == "cancelled"
    assert failure.phase_id == "cancelled"
    assert failure.error is not None
    assert failure.error.code == "cancelled"


def test_final_verification_resolves_source_read_warning_and_confidence(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=3)
    clear_recent_records()
    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            FakeModelProvider(
                _configuration(),
                responder=lambda request, _: _selection_response(request),
            ),
            DiscoveryRequest(task="candidate", mode=DiscoveryMode.INDEXED),
        )
    )
    assert result.final_selection is not None
    selection = result.final_selection
    assert "indexed-source-not-read" not in {
        item.code for item in selection.completeness_warnings
    }
    selected_confidences = [
        item.confidence for item in selection.selected if item.confidence is not None
    ]
    assert selection.confidence == min(selected_confidences)
    assert selection.confidence > 0.9
    verification = [
        item
        for item in recent_records()
        if item.event == "context_suggestion.source_verification_completed"
    ][-1]
    assert verification.data["selected_file_count"] == len(selection.selected)
    assert verification.data["read_file_count"] == len(selection.selected)
    assert verification.data["verified_file_count"] == len(selection.selected)
    assert verification.data["stale_file_count"] == 0
    assert verification.data["missing_file_count"] == 0
    assert verification.data["failed_file_count"] == 0


def test_failed_final_source_read_reports_safe_counters(tmp_path: Path) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=1)
    source = tmp_path / "src" / "candidate_00.py"

    def responder(request: ModelRequest, _: int) -> str:
        response = _selection_response(request)
        source.write_text("CHANGED = True\n", encoding="utf-8")
        return response

    clear_recent_records()
    with pytest.raises(DiscoverySourceChangedError):
        asyncio.run(
            suggest_repository_context(
                snapshot,
                FakeModelProvider(_configuration(), responder=responder),
                DiscoveryRequest(
                    task="candidate", mode=DiscoveryMode.INDEXED, strict=True
                ),
            )
        )
    verification = next(
        item
        for item in recent_records()
        if item.event == "context_suggestion.source_verification_completed"
    )
    assert verification.data["selected_file_count"] == 1
    assert verification.data["verified_file_count"] == 0
    assert verification.data["missing_file_count"] == 0
    assert verification.data["failed_file_count"] == 1


def test_indexed_selection_enriches_task_matched_implementation_dependency(
    tmp_path: Path,
) -> None:
    files = {
        "public/app.js": "export function search() { return mediaSource(); }\n",
        "public/media-source.js": "export function mediaSource() { return []; }\n",
        "public/unrelated.js": "export const billing = true;\n",
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "dependency-enrichment") as lock:
        build_structural_index(snapshot, lock)

    def responder(request: ModelRequest, _: int) -> str:
        records = cast(
            list[dict[str, Any]], request.trusted_code_map_facts["candidates"]
        )
        app_id = next(
            item["candidate_id"] for item in records if item["path"] == "public/app.js"
        )
        return json.dumps(
            {
                "schema_version": 1,
                "candidate_ids": [app_id],
                "summary": "Selected the media search entry implementation.",
            }
        )

    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            FakeModelProvider(_configuration(), responder=responder),
            DiscoveryRequest(
                task="explain how media search works",
                mode=DiscoveryMode.INDEXED,
                budget=DiscoveryBudget(max_preselected_candidates=3),
            ),
        )
    )
    assert result.final_selection is not None
    selected = {item.path for item in result.final_selection.selected}
    assert selected == {"public/app.js", "public/media-source.js"}


def test_small_previous_response_is_private_and_repair_strategies_progress() -> None:
    requests: list[ModelRequest] = []
    secret_marker = "PRIVATE_INVALID_RESPONSE_MARKER"
    invalid = json.dumps(
        {
            "schema_version": 1,
            "content": [],
            "selected_candidates": [],
            "note": secret_marker + "x" * 350,
        }
    )

    def responder(request: ModelRequest, index: int) -> str:
        requests.append(request)
        return _valid() if index == 2 else invalid

    clear_recent_records()
    provider = FakeModelProvider(_configuration(), responder=responder)
    response = asyncio.run(provider.complete_structured(_request()))
    assert response.diagnostic is not None
    assert provider.call_count == 3
    assert requests[1].untrusted_contexts == ()
    assert requests[2].untrusted_contexts[0].label == "previous-invalid-response"
    encoded_logs = json.dumps([item.to_dict() for item in recent_records()])
    assert secret_marker not in encoded_logs
    scheduled = [
        item for item in recent_records() if item.event == "response.repair.scheduled"
    ]
    assert scheduled[0].data["previous_response_included"] is False
    assert scheduled[1].data["previous_response_included"] is True
    assert scheduled[1].data["previous_response_bytes"] >= 300


def test_oversized_previous_response_is_omitted() -> None:
    oversized = _invalid(padding=5_000)
    scripts = [_invalid(), oversized, _valid()]
    clear_recent_records()
    provider = FakeModelProvider(_configuration(), scripts=scripts)
    asyncio.run(provider.complete_structured(_request()))
    scheduled = [
        item for item in recent_records() if item.event == "response.repair.scheduled"
    ]
    assert scheduled[1].data["previous_response_included"] is False
    assert scheduled[1].data["omission_reason"] == "byte_limit_exceeded"
    assert scheduled[1].data["previous_response_bytes"] > 4_096


def test_five_repairs_have_distinct_strategies_fingerprints_and_repetition() -> None:
    clear_recent_records()
    provider = FakeModelProvider(_configuration(), scripts=[_invalid()] * 6)
    with pytest.raises(StructuredResponseError) as captured:
        asyncio.run(provider.complete_structured(_request()))
    assert provider.call_count == 6
    assert captured.value.diagnostic is not None
    scheduled = [
        item for item in recent_records() if item.event == "response.repair.scheduled"
    ]
    assert [item.data["repair_strategy"] for item in scheduled] == [
        "exact_paths_and_schema",
        "correct_previous_full_object",
        "allowed_ids_required_template",
        "plain_json_local_validation",
        "smallest_task_valid_object",
    ]
    assert len({item.data["repair_prompt_fingerprint"] for item in scheduled}) == 5
    assert scheduled[3].data["schema_mode"] == "plain_json"
    assert any(item.data["repeated_failure_detected"] is True for item in scheduled[1:])


def test_nonstrict_exhaustion_falls_back_and_strict_remains_controlled(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=3)
    malformed = "{"
    nonstrict_provider = FakeModelProvider(_configuration(), scripts=[malformed] * 6)
    clear_recent_records()
    degraded = asyncio.run(
        suggest_repository_context(
            snapshot,
            nonstrict_provider,
            DiscoveryRequest(task="candidate", mode=DiscoveryMode.INDEXED),
        )
    )
    assert nonstrict_provider.call_count == 6
    assert degraded.final_selection is not None
    assert degraded.final_selection.provenance == "deterministic_fallback"
    assert (
        FinalContextSelection.model_validate_json(
            degraded.final_selection.model_dump_json()
        )
        == degraded.final_selection
    )
    fallback_event = next(
        item
        for item in recent_records()
        if item.event == "context_suggestion.fallback_selected"
    )
    assert fallback_event.data["final_outcome"] == "degraded_success"
    assert fallback_event.data["fallback_units"] == 1
    assert fallback_event.data["succeeded_units"] == 1
    assert fallback_event.data["failed_units"] == 0

    strict_provider = FakeModelProvider(_configuration(), scripts=[malformed] * 6)
    with pytest.raises(DiscoveryProtocolError):
        asyncio.run(
            suggest_repository_context(
                snapshot,
                strict_provider,
                DiscoveryRequest(
                    task="candidate", mode=DiscoveryMode.INDEXED, strict=True
                ),
            )
        )
    assert strict_provider.call_count == 6


def test_operation_correlation_has_one_top_level_terminal_event(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=2)
    provider = FakeModelProvider(_configuration(), scripts=["{"] * 6)
    clear_recent_records()
    asyncio.run(
        suggest_repository_context(
            snapshot,
            provider,
            DiscoveryRequest(task="candidate", mode=DiscoveryMode.INDEXED),
        )
    )
    records = recent_records()
    starts = [item for item in records if item.event == "operation.started"]
    root = next(item for item in starts if item.parent_operation_id is None)
    root_terminals = [
        item
        for item in records
        if item.operation_id == root.operation_id
        and item.event
        in {
            "operation.completed",
            "operation.failed",
            "operation.cancelled",
        }
    ]
    assert len(root_terminals) == 1
    assert root_terminals[0].event == "operation.completed"
    provider_events = [
        item for item in records if item.event == "provider.request.started"
    ]
    assert provider_events
    assert all(item.parent_operation_id is not None for item in provider_events)
    assert all(
        item.top_level_operation_id == root.operation_id for item in provider_events
    )
    assert all(
        item.request_id is not None and item.phase_id is not None
        for item in provider_events
    )
    empty_failures = [
        item
        for item in records
        if item.event == "operation.failed"
        and item.data.get("safe_error_code") is None
        and item.data.get("safe_error_message") is None
    ]
    assert not empty_failures


def test_cli_fallback_keeps_json_stdout_parseable_and_logs_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _indexed_snapshot(tmp_path, count=2)
    repair_limits: list[int] = []

    def provider_factory(configuration: ProviderConfiguration) -> FakeModelProvider:
        repair_limits.append(configuration.max_json_repair_attempts)
        return FakeModelProvider(configuration, scripts=["{"] * 2)

    monkeypatch.setattr(context_cli, "create_model_provider", provider_factory)
    result = CliRunner().invoke(
        app,
        [
            "context",
            "suggest",
            str(tmp_path),
            "--task",
            "candidate",
            "--discovery",
            "indexed",
            "--provider",
            "fake",
            "--format",
            "json",
            "--progress",
            "always",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["provenance"] == "deterministic_fallback"
    assert payload["budget_usage"]["model_calls"] == 1
    assert payload["budget_usage"]["provider_http_calls"] == 2
    assert repair_limits == [1]
    assert "fallback" in result.stderr.casefold()
    assert "Traceback" not in result.stderr


def test_startup_media_fallback_penalizes_incidental_metadata_and_tests(
    tmp_path: Path,
) -> None:
    files = {
        "src/app/startup.py": "def start_application():\n    return search_media()\n",
        "src/media/search.py": "def search_media():\n    return []\n",
        "tests/test_media_search.py": "def test_search_media():\n    assert True\n",
        "tests/test_unrelated_billing.py": "def test_invoice():\n    assert True\n",
        ".env.example": "MEDIA_URL=\n",
        ".gitignore": ".env\n",
        "LICENSE": "Example license text\n",
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "ranking-regression") as lock:
        build_structural_index(snapshot, lock)
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=1), scripts=["{", "{"]
    )
    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            provider,
            DiscoveryRequest(
                task="application startup and media search",
                mode=DiscoveryMode.INDEXED,
                budget=DiscoveryBudget(max_preselected_candidates=4),
            ),
        )
    )
    assert result.final_selection is not None
    selected = {item.path for item in result.final_selection.selected}
    assert {"src/app/startup.py", "src/media/search.py"} <= selected
    assert not selected & {
        ".env.example",
        ".gitignore",
        "LICENSE",
        "tests/test_unrelated_billing.py",
    }
    assert len(selected) <= 4
    assert result.budget_usage.model_calls == 1
    assert result.budget_usage.provider_http_calls == 2


def test_cli_context_suggest_retains_explicit_five_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _indexed_snapshot(tmp_path, count=1)
    repair_limits: list[int] = []

    def provider_factory(configuration: ProviderConfiguration) -> FakeModelProvider:
        repair_limits.append(configuration.max_json_repair_attempts)
        return FakeModelProvider(configuration, scripts=["{"] * 6)

    monkeypatch.setattr(context_cli, "create_model_provider", provider_factory)
    result = CliRunner().invoke(
        app,
        [
            "context",
            "suggest",
            str(tmp_path),
            "--task",
            "candidate",
            "--discovery",
            "indexed",
            "--provider",
            "fake",
            "--json-repair-attempts",
            "5",
            "--format",
            "json",
            "--progress",
            "never",
        ],
    )
    assert result.exit_code == 0, result.output
    assert repair_limits == [5]
    assert json.loads(result.stdout)["budget_usage"]["provider_http_calls"] == 6


def test_fallback_preserves_pinned_metadata_with_candidate_limit(
    tmp_path: Path,
) -> None:
    snapshot = _indexed_snapshot(tmp_path, count=2)
    pinned = tmp_path / ".env.example"
    pinned.write_text("EXAMPLE=1\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "pinned-ranking") as lock:
        build_structural_index(snapshot, lock)
    result = asyncio.run(
        suggest_repository_context(
            snapshot,
            FakeModelProvider(
                _configuration(max_json_repair_attempts=1), scripts=["{", "{"]
            ),
            DiscoveryRequest(
                task="candidate",
                mode=DiscoveryMode.INDEXED,
                pinned_paths=(".env.example",),
                budget=DiscoveryBudget(max_preselected_candidates=2),
            ),
        )
    )
    assert result.final_selection is not None
    assert ".env.example" in {item.path for item in result.final_selection.selected}
    assert len(result.final_selection.selected) <= 2


def test_private_previous_response_policy_covers_safe_omission_boundaries() -> None:
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            None, include=True
        ).omission_reason
        == "response_unavailable"
    )
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            b"\xff", include=True
        ).omission_reason
        == "invalid_utf8"
    )
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            "{}", include=False
        ).omission_reason
        == "strategy_omits_response"
    )
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            json.dumps({"value": "x" * 3_200}), include=True
        ).omission_reason
        == "token_limit_exceeded"
    )
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            "{", include=True
        ).omission_reason
        == "malformed_json"
    )
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            "[]", include=True
        ).omission_reason
        == "irrelevant_response_shape"
    )
    assert (
        provider_module._previous_response_policy(  # noqa: SLF001
            '{"value":"<ANALYSIS_TASK>"}', include=True
        ).omission_reason
        == "unsafe_provider_material"
    )
    included = provider_module._previous_response_policy(  # noqa: SLF001
        '{"api_key":"secret","nested":{"value":1}}', include=True
    )
    assert included.included is True
    assert included.text is not None
    assert "secret" not in included.text
    assert "[REDACTED]" in included.text


def test_safe_shape_and_issue_helpers_cover_nonlogging_fingerprint_inputs() -> None:
    equal_types = provider_module._validation_issue(  # noqa: SLF001
        "wrong_field_type",
        "/value",
        expected="object",
        actual="object",
        reason="failed a value constraint",
    )
    assert isinstance(equal_types, InvalidFieldValueIssue)
    assert provider_module._normalized_response_shape(  # noqa: SLF001
        b'{"items":[1,true,null]}'
    ) == {"items": ["integer", "boolean", "null"]}
    assert (
        provider_module._normalized_response_shape(b"\xff")  # noqa: SLF001
        == "malformed"
    )
    compact = provider_module._compact_response_schema(  # noqa: SLF001
        {
            "title": "annotation",
            "type": "object",
            "properties": {"value": {"type": "string", "description": "x"}},
        }
    )
    assert compact == {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
