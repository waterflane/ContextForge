import asyncio
import json
from pathlib import Path
from typing import Any

from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryMode,
    DiscoveryRequest,
    discover_repository,
)
from contextforge.logging import LogLevel, clear_recent_records, recent_records
from contextforge.models import (
    FakeModelProvider,
    FakeScript,
    InvalidFieldValueIssue,
    MissingRequiredFieldIssue,
    ModelRequest,
    ProviderConfiguration,
    ProviderTransportResponse,
    structured_validation_fingerprint,
)
from contextforge.repositories import ProjectSnapshot, scan_repository


def _snapshot(root: Path) -> ProjectSnapshot:
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8", newline="")
    return scan_repository(root)


def _configuration(**updates: Any) -> ProviderConfiguration:
    return ProviderConfiguration.model_validate(
        {
            "provider_id": "fake",
            "endpoint": "fake://offline",
            "model_id": "fresh-action-circuit",
            "retry_limit": 0,
            "max_json_repair_attempts": 5,
            "context_window": 98_304,
            **updates,
        }
    )


def _call(
    action_id: str, tool_name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "action_id": action_id,
        "kind": "call_tool",
        "tool_name": tool_name,
        "arguments": arguments or {},
    }


def _finalize() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "action_id": "finish",
        "kind": "finalize",
        "arguments": {
            "summary": "Selected the complete fresh context.",
            "unknowns": [],
            "confidence": 0.9,
        },
    }


def _batch(*actions: dict[str, Any]) -> str:
    return json.dumps({"schema_version": 1, "actions": list(actions)})


def _complete_batch() -> str:
    return _batch(
        _call(
            "select-main",
            "add_to_context",
            {"path": "main.py", "reason": "task implementation"},
        ),
        _finalize(),
    )


def _request(
    snapshot: ProjectSnapshot, provider: FakeModelProvider, **budget: Any
) -> Any:
    return asyncio.run(
        discover_repository(
            snapshot,
            provider,
            DiscoveryRequest(
                task="Find VALUE",
                mode=DiscoveryMode.FRESH,
                budget=DiscoveryBudget(**budget),
            ),
        )
    )


def test_initial_missing_actions_is_repaired_and_prompt_has_minimal_example(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    requests: list[ModelRequest] = []

    def responder(request: ModelRequest, index: int) -> str:
        requests.append(request)
        return '{"schema_version":1}' if index == 0 else _complete_batch()

    provider = FakeModelProvider(_configuration(), responder=responder)
    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "model"
    assert set(requests[0].response_schema["required"]) == {
        "schema_version",
        "actions",
    }
    assert requests[0].response_schema["properties"]["actions"]["minItems"] == 1
    assert "required non-empty actions array" in requests[0].system_instructions
    assert '"actions":[{' in requests[0].system_instructions
    assert '"actions":[]' not in requests[1].analysis_task
    assert "Minimal valid response:" in requests[1].analysis_task
    assert result.budget_usage.model_calls == 1
    assert result.budget_usage.model_generations == 1
    assert result.budget_usage.repair_generations == 1
    assert result.budget_usage.total_provider_http_calls == 2


def test_repeated_missing_actions_triggers_fallback_at_three_equivalent_failures(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(), scripts=['{"schema_version":1}'] * 10
    )

    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "deterministic_fallback"
    assert provider.call_count == 3
    assert result.budget_usage.model_calls == 1
    assert result.budget_usage.model_generations == 1
    assert result.budget_usage.repair_generations == 2
    assert result.budget_usage.transport_attempts == 3
    assert result.budget_usage.total_provider_http_calls == 3


def test_repeated_actions_too_short_uses_the_same_bound(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(), scripts=['{"schema_version":1,"actions":[]}'] * 10
    )

    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "deterministic_fallback"
    assert provider.call_count == 3
    failed = [
        item for item in recent_records() if item.event == "response.validation.failed"
    ][-1]
    assert failed.data["validation_issue_paths"] == ["/actions"]
    assert failed.data["failed_constraints"] == ["too_short"]


def test_equivalent_failure_prose_has_one_stable_fingerprint() -> None:
    first = MissingRequiredFieldIssue(
        path="/actions",
        constraint="required",
        expected_constraint="required array",
        actual_value_kind="missing",
        reason="actions are missing",
    )
    second = MissingRequiredFieldIssue(
        path="/actions",
        constraint="required",
        expected_constraint="a differently worded requirement",
        actual_value_kind="absent",
        reason="completely different provider prose",
    )

    assert structured_validation_fingerprint((first,)) == (
        structured_validation_fingerprint((second,))
    )


def test_distinct_failure_paths_and_constraints_have_distinct_fingerprints() -> None:
    baseline = InvalidFieldValueIssue(
        path="/actions",
        constraint="too_short",
        expected_constraint="minItems=1",
        actual_value_kind="array",
        reason="empty",
    )
    distinct_path = baseline.model_copy(update={"path": "/candidate_ids"})
    distinct_constraint = baseline.model_copy(update={"constraint": "too_long"})

    fingerprints = {
        structured_validation_fingerprint((item,))
        for item in (baseline, distinct_path, distinct_constraint)
    }
    assert len(fingerprints) == 3


def test_distinct_failures_do_not_share_the_repetition_count(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(),
        scripts=[
            '{"schema_version":1}',
            '{"schema_version":1,"actions":[]}',
            '{"schema_version":1}',
            '{"schema_version":1,"actions":[]}',
            '{"schema_version":1}',
        ],
    )

    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "deterministic_fallback"
    assert provider.call_count == 5
    assert result.budget_usage.repair_generations == 4


def test_equivalent_failures_are_tracked_across_fresh_action_rounds(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    requests: list[ModelRequest] = []
    invalid = '{"schema_version":1}'
    no_progress = _batch(_call("budget", "get_context_budget"))

    def responder(request: ModelRequest, index: int) -> str:
        requests.append(request)
        return (invalid, no_progress, invalid, no_progress, invalid)[index]

    provider = FakeModelProvider(_configuration(), responder=responder)
    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "deterministic_fallback"
    assert provider.call_count == 5
    assert result.budget_usage.model_calls == 3
    assert result.budget_usage.model_generations == 3
    assert result.budget_usage.repair_generations == 2
    assert result.budget_usage.total_provider_http_calls == 5
    observation_counts = [
        len(json.loads(request.untrusted_contexts[0].text))
        for request in requests
        if request.untrusted_contexts
        and request.untrusted_contexts[0].label == "discovery-observations"
    ]
    assert observation_counts == [1, 2]
    assert (
        max(
            sum(len(message.content.encode("utf-8")) for message in request.messages())
            for request in requests
        )
        < 128 * 1024
    )


def test_meaningful_tool_progress_resets_the_active_failure_count(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(),
        scripts=[
            '{"schema_version":1}',
            _batch(_call("read-main", "read_file", {"path": "main.py"})),
            '{"schema_version":1}',
            '{"schema_version":1}',
            _complete_batch(),
        ],
    )

    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "model"
    assert provider.call_count == 5
    assert result.budget_usage.model_calls == 2
    assert result.budget_usage.model_generations == 2
    assert result.budget_usage.repair_generations == 3


def test_repair_and_provider_http_accounting_remains_exact_and_bounded(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    invalid = '{"schema_version":1}'
    scripts = [
        ProviderTransportResponse(
            text=invalid,
            provider_discovery_calls=1,
            provider_capability_calls=2,
            transport_attempts=3,
            provider_http_calls=4,
        )
    ] * 10
    provider = FakeModelProvider(_configuration(), scripts=scripts)

    result = _request(snapshot, provider)
    usage = result.budget_usage

    assert usage.model_generations == 1
    assert usage.repair_generations == 2
    assert usage.provider_discovery_calls == 3
    assert usage.provider_capability_calls == 6
    assert usage.transport_attempts == 9
    assert usage.provider_http_calls == 12
    assert usage.total_provider_http_calls == 12


def test_valid_response_before_third_equivalent_failure_is_accepted(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(),
        scripts=['{"schema_version":1}', '{"schema_version":1}', _complete_batch()],
    )

    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "model"
    assert provider.call_count == 3
    assert result.budget_usage.model_generations == 1
    assert result.budget_usage.repair_generations == 2


def test_valid_multi_step_fresh_flow_is_unchanged(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(),
        scripts=[
            _batch(_call("read-main", "read_file", {"path": "main.py"})),
            _complete_batch(),
        ],
    )

    result = _request(snapshot, provider)

    assert result.final_selection is not None
    assert result.final_selection.provenance == "model"
    assert provider.call_count == 2
    assert result.budget_usage.model_calls == 2
    assert result.budget_usage.model_generations == 2
    assert result.budget_usage.repair_generations == 0
    assert result.budget_usage.total_provider_http_calls == 2


def test_fallback_is_complete_with_exact_nonprovider_counters(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(_configuration(), scripts=['{"schema_version":1}'] * 3)

    result = _request(snapshot, provider)

    assert result.status == "complete"
    assert result.failure_code is None
    assert result.final_selection is not None
    assert tuple(item.path for item in result.final_selection.selected) == ("main.py",)
    assert result.budget_usage.files_read == 2
    assert result.budget_usage.source_bytes == 2 * len(b"VALUE = 1\n")
    assert result.budget_usage.tool_result_bytes == 0
    assert result.budget_usage.context_bytes == len(b"VALUE = 1\n")
    assert result.budget_usage.context_files == 1


def test_bounded_failures_do_not_become_context_window_or_total_timeout(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    provider = FakeModelProvider(
        _configuration(context_window=32_768),
        scripts=[FakeScript('{"schema_version":1}', delay_seconds=0.01)] * 3,
    )

    result = _request(snapshot, provider, timeout_seconds=1.0)

    assert result.status == "complete"
    assert result.failure_code is None
    assert result.final_selection is not None
    assert result.final_selection.provenance == "deterministic_fallback"


def test_bounded_fallback_surfaces_one_concise_diagnostic_warning(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    clear_recent_records()
    provider = FakeModelProvider(_configuration(), scripts=['{"schema_version":1}'] * 3)

    result = _request(snapshot, provider)

    assert result.status == "complete"
    warnings = [item for item in recent_records() if item.level is LogLevel.WARNING]
    fallback_warnings = [
        item
        for item in warnings
        if item.event == "context_suggestion.fallback_selected"
    ]
    assert len(fallback_warnings) == 1
    assert "structured-action validation failures" in fallback_warnings[0].message
    assert not [
        item
        for item in warnings
        if item.event
        in {
            "response.validation.failed",
            "response.repair.scheduled",
            "provider.retry.scheduled",
        }
    ]
