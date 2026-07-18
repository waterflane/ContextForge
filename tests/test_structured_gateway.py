from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from contextforge.models import (
    ContextWindowExceededError,
    FakeModelProvider,
    ModelRequest,
    ProviderConfiguration,
    ProviderRequestError,
    ProviderTimeoutError,
    ProviderTransportResponse,
    StructuredResponseError,
    validate_structured_response,
)
from contextforge.progress import ProgressEvent, ProgressStatus
from contextforge.project_config import (
    load_project_configuration,
    resolve_provider_configuration,
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Item(_Closed):
    candidate_id: str


class _Result(_Closed):
    schema_version: Literal[1] = 1
    content: str = Field(min_length=1, max_length=100)
    selected_candidates: tuple[_Item, ...] = Field(default=(), max_length=3)
    relevance: float | None = Field(default=None, ge=0, le=1)


def _request(*, progress=None) -> ModelRequest:
    return ModelRequest(
        operation_id="gateway-test",
        purpose="gateway-test",
        system_instructions="Return a bounded test result.",
        analysis_task="Return the requested compact result.",
        trusted_code_map_facts={},
        untrusted_sources=(),
        response_model=_Result,
        max_output_tokens=128,
        progress=progress,
    )


def _configuration(**updates: object) -> ProviderConfiguration:
    values = {
        "provider_id": "fake",
        "endpoint": "fake://offline",
        "model_id": "gateway-fixture",
        "retry_limit": 0,
        **updates,
    }
    return ProviderConfiguration.model_validate(values)


def _valid(content: str = "ok") -> str:
    return json.dumps({"schema_version": 1, "content": content})


def test_valid_initial_and_fenced_normalization_use_the_same_gateway() -> None:
    initial = validate_structured_response(
        _valid(), request=_request(), max_response_bytes=1_000
    )
    fenced = validate_structured_response(
        f"```json\n{_valid()}\n```", request=_request(), max_response_bytes=1_000
    )

    assert initial.value == fenced.value
    assert fenced.normalization_actions == ("remove_surrounding_json_fence",)


def test_gateway_normalization_and_transport_edge_layers() -> None:
    missing_version = validate_structured_response(
        b'  {"content":"ok"}\n',
        request=_request(),
        max_response_bytes=1_000,
    )
    assert missing_version.normalization_actions == (
        "trim_surrounding_whitespace",
        "insert_constant_schema_version",
    )
    numeric = validate_structured_response(
        '{"schema_version":1,"content":"ok","relevance":1}',
        request=_request(),
        max_response_bytes=1_000,
    )
    assert numeric.normalization_actions == (
        "normalize_integer_to_float:/relevance",
    )

    for payload, limit, code in (
        (b"\xff", 1_000, "malformed_json"),
        (object(), 1_000, "wrong_field_type"),
        (_valid(), 2, "string_limit_exceeded"),
        ("```python\n{}\n```", 1_000, "malformed_json"),
    ):
        with pytest.raises(StructuredResponseError) as captured:
            validate_structured_response(
                payload,  # type: ignore[arg-type]
                request=_request(),
                max_response_bytes=limit,
            )
        assert captured.value.issues[0].code == code

    request = ModelRequest(
        operation_id="no-fence",
        purpose="gateway-test",
        system_instructions="Return JSON.",
        analysis_task="Return JSON.",
        trusted_code_map_facts={},
        untrusted_sources=(),
        response_model=_Result,
        allow_fenced_json=False,
    )
    with pytest.raises(StructuredResponseError, match="not permitted"):
        validate_structured_response(
            f"```json\n{_valid()}\n```",
            request=request,
            max_response_bytes=1_000,
        )


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_unacceptable_finish_states_enter_repair_policy(finish_reason: str) -> None:
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=0),
        scripts=[ProviderTransportResponse(text=_valid(), finish_reason=finish_reason)],
    )
    with pytest.raises(StructuredResponseError) as captured:
        asyncio.run(provider.complete_structured(_request()))
    assert captured.value.issues[0].code in {
        "truncated_response",
        "invalid_field_value",
    }


def test_internal_conversion_failure_is_repairable_and_safe() -> None:
    def reject(_: BaseModel) -> None:
        raise ValueError("bounded internal conversion failed")

    request = _request()
    request = ModelRequest(
        operation_id=request.operation_id,
        purpose=request.purpose,
        system_instructions=request.system_instructions,
        analysis_task=request.analysis_task,
        trusted_code_map_facts=request.trusted_code_map_facts,
        untrusted_sources=request.untrusted_sources,
        response_model=request.response_model,
        response_validator=reject,
    )
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=0), scripts=[_valid()]
    )
    with pytest.raises(StructuredResponseError) as captured:
        asyncio.run(provider.complete_structured(request))
    assert captured.value.issues[0].code == "internal_conversion_failure"
    assert "bounded internal conversion failed" in str(captured.value)


@pytest.mark.parametrize(
    ("payload", "code", "path"),
    [
        ("", "empty_response", "/"),
        ("{", "malformed_json", "/"),
        ("{} {}", "multiple_json_values", "/"),
        ("[]", "wrong_top_level_type", "/"),
        ('{"schema_version":1}', "missing_required_field", "/content"),
        (
            '{"schema_version":1,"content":[],"selected_candidates":[]}',
            "wrong_field_type",
            "/content",
        ),
        (
            '{"schema_version":1,"content":"ok","extra":true}',
            "additional_property",
            "/extra",
        ),
        (
            '{"schema_version":2,"content":"ok"}',
            "unsupported_schema_version",
            "/schema_version",
        ),
        (
            '{"schema_version":1,"content":"' + "x" * 101 + '"}',
            "string_limit_exceeded",
            "/content",
        ),
    ],
)
def test_gateway_exposes_typed_issues(payload: str, code: str, path: str) -> None:
    with pytest.raises(StructuredResponseError) as captured:
        validate_structured_response(
            payload, request=_request(), max_response_bytes=10_000
        )

    assert captured.value.issues[0].code == code
    assert captured.value.issues[0].path == path


def test_valid_first_and_fifth_repairs_and_exhaustion_call_bounds() -> None:
    first = FakeModelProvider(
        _configuration(max_json_repair_attempts=5), scripts=["{", _valid()]
    )
    response = asyncio.run(first.complete_structured(_request()))
    assert first.call_count == 2
    assert response.diagnostic is not None
    assert response.diagnostic.json_repair_attempt == 1

    fifth = FakeModelProvider(
        _configuration(max_json_repair_attempts=5),
        scripts=["{"] * 5 + [_valid()],
    )
    response = asyncio.run(fifth.complete_structured(_request()))
    assert fifth.call_count == 6
    assert response.diagnostic is not None
    assert response.diagnostic.json_repair_attempt == 5
    assert response.diagnostic.total_provider_calls == 6

    exhausted = FakeModelProvider(
        _configuration(max_json_repair_attempts=5), scripts=["{"] * 7
    )
    with pytest.raises(StructuredResponseError) as captured:
        asyncio.run(exhausted.complete_structured(_request()))
    assert exhausted.call_count == 6
    assert captured.value.diagnostic is not None
    assert captured.value.diagnostic.total_provider_calls == 6


def test_zero_repairs_and_non_output_failures_consume_no_repairs() -> None:
    disabled = FakeModelProvider(
        _configuration(max_json_repair_attempts=0), scripts=["{", _valid()]
    )
    with pytest.raises(StructuredResponseError):
        asyncio.run(disabled.complete_structured(_request()))
    assert disabled.call_count == 1

    authentication = FakeModelProvider(
        _configuration(max_json_repair_attempts=5),
        scripts=[ProviderRequestError("authentication failed"), _valid()],
    )
    with pytest.raises(ProviderRequestError) as captured:
        asyncio.run(authentication.complete_structured(_request()))
    assert authentication.call_count == 1
    assert captured.value.diagnostic is not None
    assert captured.value.diagnostic.json_repair_attempt == 0

    overflow = FakeModelProvider(
        _configuration(context_window=1_024), scripts=[_valid()]
    )
    oversized = ModelRequest(
        operation_id="overflow-test",
        purpose="gateway-test",
        system_instructions="x" * 3_000,
        analysis_task="task",
        trusted_code_map_facts={},
        untrusted_sources=(),
        response_model=_Result,
        max_output_tokens=128,
    )
    with pytest.raises(ContextWindowExceededError):
        asyncio.run(overflow.complete_structured(oversized))
    assert overflow.call_count == 0


def test_transport_and_repair_counters_are_independent() -> None:
    provider = FakeModelProvider(
        _configuration(retry_limit=1, max_json_repair_attempts=1),
        scripts=[ProviderTimeoutError("timeout"), "{", _valid()],
        retry_delays=(0,),
    )
    response = asyncio.run(provider.complete_structured(_request()))

    assert response.diagnostic is not None
    assert response.diagnostic.transport_max_attempts == 2
    assert response.diagnostic.transport_attempt == 1
    assert response.diagnostic.json_repair_attempt == 1
    assert response.diagnostic.total_provider_calls == 3


def test_progress_has_one_terminal_state_and_one_success_count() -> None:
    events: list[ProgressEvent] = []
    provider = FakeModelProvider(
        _configuration(max_json_repair_attempts=1), scripts=["{", _valid()]
    )
    asyncio.run(provider.complete_structured(_request(progress=events.append)))

    assert any(event.lifecycle_state == "json_repair" for event in events)
    assert sum(event.status is not ProgressStatus.RUNNING for event in events) == 1
    validated = [event for event in events if event.lifecycle_state == "validated"]
    assert len(validated) == 1
    assert validated[0].processed_units == validated[0].succeeded_units == 1


def test_configuration_precedence_and_persistent_context_window(tmp_path: Path) -> None:
    config_dir = tmp_path / ".contextforge"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[models]\ncontext_window=8192\n"
        "[models.structured_response]\nmax_repair_attempts=2\n",
        encoding="utf-8",
    )
    (config_dir / "config.local.toml").write_text(
        "[models]\ncontext_window=98304\n"
        "[models.structured_response]\nmax_repair_attempts=3\n",
        encoding="utf-8",
    )
    project = load_project_configuration(
        tmp_path, environment={"CONTEXTFORGE_JSON_REPAIR_ATTEMPTS": "4"}
    )
    provider = resolve_provider_configuration(project, json_repair_attempts=5)

    assert project.models.context_window == 98304
    assert provider is not None
    assert provider.context_window == 98304
    assert provider.context_window_source == "config.local.toml"
    assert provider.max_json_repair_attempts == 5


def test_model_call_sites_delegate_acceptance_to_provider_gateway() -> None:
    root = Path(__file__).parents[1] / "src" / "contextforge"
    call_sites: list[Path] = []
    forbidden: list[Path] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".complete_structured(" in text:
            call_sites.append(path)
        if "parse_structured_response(" in text and path.name != "providers.py":
            forbidden.append(path)

    assert call_sites
    assert not forbidden
