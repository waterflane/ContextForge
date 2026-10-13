import asyncio
from dataclasses import replace
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from contextforge.benchmarks.dispatch_budget import (
    BenchmarkBudgetExceeded,
    BenchmarkDispatchBudget,
)
from contextforge.benchmarks.live_real_repositories import _MeasuredProvider
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    ready: bool


def request(operation: str) -> ModelRequest:
    return ModelRequest(
        operation_id=operation,
        purpose="bounded-benchmark",
        system_instructions="Return the requested JSON.",
        analysis_task="Verify the provided sources.",
        trusted_code_map_facts={},
        untrusted_sources=(),
        response_model=Response,
    )


@pytest.mark.parametrize(
    ("limit", "value", "reason"),
    [
        ("max_calls", 0, "call_limit"),
        ("max_estimated_input_tokens", 0, "estimated_input_token_limit"),
        ("max_seconds", 0, "time_limit"),
    ],
)
def test_dispatch_guard_rejects_before_provider_call(
    limit: str, value: int, reason: str
) -> None:
    limits = dict(max_calls=20, max_estimated_input_tokens=500_000, max_seconds=1800)
    limits[limit] = value
    budget = BenchmarkDispatchBudget(
        max_calls=limits["max_calls"],
        max_estimated_input_tokens=limits["max_estimated_input_tokens"],
        max_seconds=limits["max_seconds"],
    )
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=lambda _request, _attempt: '{"schema_version":1,"ready":true}',
    )
    measured = _MeasuredProvider(provider, budget)
    with pytest.raises(BenchmarkBudgetExceeded, match=reason) as caught:
        asyncio.run(measured.complete_structured(request("benchmark-answer-ordinary")))
    assert caught.value.total_provider_http_calls == 0
    assert budget.calls == measured.calls == 0
    assert budget.estimated_input_tokens == 0
    assert budget.stop_reasons == [reason]


def test_phase_ceiling_preserves_other_phase_allowance() -> None:
    budget = BenchmarkDispatchBudget(20, 500_000, 1800, {"index": 1, "planner": 3})
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=lambda _request, _attempt: '{"schema_version":1,"ready":true}',
    )
    measured = _MeasuredProvider(provider, budget)
    asyncio.run(measured.complete_structured(request("card-build-a")))
    with pytest.raises(BenchmarkBudgetExceeded, match="index_call_limit"):
        asyncio.run(measured.complete_structured(request("lexicon-build-b")))
    asyncio.run(measured.complete_structured(request("evidence-plan-generation")))
    assert budget.calls == measured.calls == 2
    assert budget.phase_calls == {"index": 1, "planner": 1}
    assert budget.estimated_input_tokens == measured.estimated_input
    assert len(measured.records) == 2


@pytest.mark.parametrize("actual_calls", [0, 1])
def test_configuration_rejection_preserves_dispatch_accounting(
    monkeypatch: pytest.MonkeyPatch, actual_calls: int
) -> None:
    from contextforge.models import ModelResponse, ProviderConfigurationError

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        )
    )
    budget = BenchmarkDispatchBudget(80, 2_000_000, 7200, {"preflight": 4, "index": 16})
    measured = _MeasuredProvider(provider, budget)

    async def rejected(*_args: object, **_kwargs: object) -> ModelResponse:
        error = ProviderConfigurationError("Configured reasoning effort is unsupported")
        error.total_provider_http_calls = actual_calls
        error.transport_attempts = actual_calls
        raise error

    monkeypatch.setattr(provider, "complete_structured", rejected)
    with pytest.raises(ProviderConfigurationError):
        asyncio.run(measured.complete_structured(request("benchmark-preflight-model")))
    assert measured.calls == budget.calls == actual_calls
    assert budget.phase_calls == {"preflight": actual_calls}
    assert bool(budget.estimated_input_tokens) == bool(actual_calls)
    assert measured.records[0].provider_calls == actual_calls


def test_bounded_dispatch_rejects_automatic_retries() -> None:
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=1,
            max_json_repair_attempts=0,
        ),
    )
    with pytest.raises(ValueError, match="zero retries and repairs"):
        _MeasuredProvider(provider, BenchmarkDispatchBudget(20, 500_000, 1800))


def test_failed_dispatch_consumes_actual_call_allowance() -> None:
    from contextforge.models import ModelProviderError

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=lambda _request, _attempt: ModelProviderError("provider failed"),
    )
    budget = BenchmarkDispatchBudget(1, 500_000, 1800)
    measured = _MeasuredProvider(provider, budget)
    with pytest.raises(ModelProviderError):
        asyncio.run(measured.complete_structured(request("benchmark-answer-ordinary")))
    assert budget.calls == measured.calls == 1
    assert budget.estimated_input_tokens > 0
    with pytest.raises(BenchmarkBudgetExceeded, match="call_limit"):
        asyncio.run(measured.complete_structured(request("benchmark-answer-oracle")))
    assert budget.calls == 1


def test_dispatch_reservation_prevents_concurrent_budget_oversubscription() -> None:
    budget = BenchmarkDispatchBudget(1, 100, 1800)
    budget.authorize("card-build-a", 60)
    with pytest.raises(BenchmarkBudgetExceeded, match="call_limit"):
        budget.authorize("card-build-b", 30)
    budget.record("card-build-a", 0, 60)
    budget.authorize("card-build-b", 100)
    budget.record("card-build-b", 1, 100)
    assert budget.calls == 1
    assert budget.estimated_input_tokens == 100


def test_cancelled_dispatch_keeps_its_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextforge.models import ModelResponse

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
    )
    budget = BenchmarkDispatchBudget(1, 500_000, 1800)
    measured = _MeasuredProvider(provider, budget)

    async def cancelled(*_args: object, **_kwargs: object) -> ModelResponse:
        raise asyncio.CancelledError

    monkeypatch.setattr(provider, "complete_structured", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(measured.complete_structured(request("benchmark-answer-ordinary")))
    assert budget.calls == measured.calls == 1
    assert measured.records[0].error == "cancelled_dispatch"
    with pytest.raises(BenchmarkBudgetExceeded, match="call_limit"):
        asyncio.run(measured.complete_structured(request("benchmark-answer-oracle")))


def test_circuit_rejection_has_zero_dispatched_token_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextforge.models import ModelResponse, ProviderCircuitOpenError

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        )
    )
    budget = BenchmarkDispatchBudget(20, 500_000, 1800)
    measured = _MeasuredProvider(provider, budget)

    async def rejected(*_args: object, **_kwargs: object) -> ModelResponse:
        raise ProviderCircuitOpenError("provider disabled before dispatch")

    monkeypatch.setattr(provider, "complete_structured", rejected)
    with pytest.raises(ProviderCircuitOpenError):
        asyncio.run(measured.complete_structured(request("benchmark-answer-ordinary")))
    assert measured.calls == budget.calls == 0
    assert measured.estimated_input == budget.estimated_input_tokens == 0
    assert measured.records[0].provider_calls == 0
    assert measured.records[0].estimated_input_tokens == 0


def test_concurrent_phase_records_count_only_their_own_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextforge.models import ModelResponse

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        )
    )
    budget = BenchmarkDispatchBudget(2, 500_000, 1800)
    measured = _MeasuredProvider(provider, budget)

    async def completed(*_args: object, **_kwargs: object) -> ModelResponse:
        await asyncio.sleep(0)
        value = Response(ready=True)
        return ModelResponse(
            normalized_json=value.model_dump_json(),
            value=value,
            provider_id="fake",
            model_id="fixture",
        )

    monkeypatch.setattr(provider, "complete_structured", completed)

    async def run() -> None:
        await asyncio.gather(
            measured.complete_structured(request("card-build-a")),
            measured.complete_structured(request("evidence-plan-generation")),
        )

    asyncio.run(run())
    assert measured.calls == budget.calls == 2
    assert [record.provider_calls for record in measured.records] == [1, 1]
    assert budget.phase_calls == {"index": 1, "planner": 1}
    assert (
        measured.estimated_input
        == budget.estimated_input_tokens
        == sum(record.estimated_input_tokens for record in measured.records)
    )


@pytest.mark.parametrize("bounded", [True, False])
def test_bounded_dispatch_disables_scheduler_repairs(bounded: bool) -> None:
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="fixture",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=lambda _request, _attempt: '{"schema_version":1,"ready":true}',
    )
    budget = BenchmarkDispatchBudget(20, 500_000, 1800) if bounded else None
    measured = _MeasuredProvider(provider, budget)
    repair = replace(
        request("semantic-card-repair-fixture"), purpose="semantic-card-repair"
    )
    if bounded:
        with pytest.raises(BenchmarkBudgetExceeded, match="repair_disabled"):
            asyncio.run(measured.complete_structured(repair))
        assert budget is not None
        assert budget.calls == budget.estimated_input_tokens == provider.call_count == 0
        assert budget.stop_reasons == ["repair_disabled"]
    else:
        response = asyncio.run(measured.complete_structured(repair))
        assert response.value == Response(ready=True)
        assert measured.calls == provider.call_count == 1
