"""Deterministic offline model provider for tests and development."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import SecretStr

from contextforge.models.providers import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderRequestError,
    ProviderRuntime,
    ProviderTransportResponse,
)

FakeResult = ProviderTransportResponse | str | bytes | BaseException
FakeResponder = Callable[[ModelRequest, int], FakeResult]


@dataclass(frozen=True, slots=True)
class FakeScript:
    """One deterministic fake result with an optional cancellable delay."""

    result: FakeResult
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.delay_seconds, bool)
            or not isinstance(self.delay_seconds, (int, float))
            or not math.isfinite(self.delay_seconds)
            or self.delay_seconds < 0
        ):
            raise ValueError("fake delay_seconds must be a non-negative number")


class FakeModelProvider:
    """Scripted provider with deterministic call ordering and concurrency metrics."""

    def __init__(
        self,
        configuration: ProviderConfiguration,
        *,
        scripts: Sequence[FakeScript | FakeResult] = (),
        responder: FakeResponder | None = None,
        environment: Mapping[str, str] | None = None,
        retry_delays: Sequence[float] = (0.25, 1.0),
    ) -> None:
        if configuration.provider_id != "fake":
            raise ValueError("fake provider configuration must use provider_id='fake'")
        if scripts and responder is not None:
            raise ValueError("fake provider accepts scripts or a responder, not both")
        self.configuration = configuration
        self._scripts = tuple(
            item if isinstance(item, FakeScript) else FakeScript(item)
            for item in scripts
        )
        self._responder = responder
        self._runtime = ProviderRuntime(
            configuration,
            environment=environment,
            retry_delays=retry_delays,
        )
        self.call_count = 0
        self.in_flight = 0
        self.maximum_in_flight = 0

    @property
    def provider_id(self) -> str:
        return "fake"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_responses=True,
            cancellation=True,
            token_usage=True,
            local=True,
        )

    async def complete_structured(
        self,
        request: ModelRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> ModelResponse:
        return await self._runtime.execute(
            request, self._complete_once, cancellation=cancellation
        )

    async def close(self) -> None:
        await self._runtime.close()

    async def _complete_once(
        self, request: ModelRequest, credential: SecretStr | None
    ) -> ProviderTransportResponse:
        del credential
        call_index = self.call_count
        self.call_count += 1
        if self._responder is not None:
            script = FakeScript(self._responder(request, call_index))
        elif call_index < len(self._scripts):
            script = self._scripts[call_index]
        else:
            raise ProviderRequestError("deterministic fake script is exhausted")

        self.in_flight += 1
        self.maximum_in_flight = max(self.maximum_in_flight, self.in_flight)
        try:
            if script.delay_seconds:
                await asyncio.sleep(script.delay_seconds)
            result = script.result
            if isinstance(result, BaseException):
                raise result
            if isinstance(result, ProviderTransportResponse):
                return result
            return ProviderTransportResponse(text=result)
        finally:
            self.in_flight -= 1


__all__ = ["FakeModelProvider", "FakeResponder", "FakeResult", "FakeScript"]
