"""Shared dispatch ceilings for an explicitly bounded live benchmark."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from contextforge.models import ModelProviderError


class BenchmarkBudgetExceeded(ModelProviderError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.transport_attempts = 0
        self.total_provider_http_calls = 0


@dataclass
class BenchmarkDispatchBudget:
    max_calls: int
    max_estimated_input_tokens: int
    max_seconds: float
    phase_call_limits: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    estimated_input_tokens: int = 0
    phase_calls: dict[str, int] = field(default_factory=dict)
    stop_reasons: list[str] = field(default_factory=list)
    _pending_calls: int = field(default=0, init=False)
    _pending_tokens: int = field(default=0, init=False)
    _pending_phase_calls: dict[str, int] = field(default_factory=dict, init=False)

    @staticmethod
    def phase(operation_id: str) -> str:
        if operation_id.startswith("benchmark-groundedness-"):
            return "judge"
        if operation_id.startswith("benchmark-answer-"):
            return "answer"
        if operation_id.startswith("evidence-plan-"):
            return "planner"
        return "index"

    def authorize(self, operation_id: str, estimated_input_tokens: int) -> None:
        phase = self.phase(operation_id)
        reason = (
            "time_limit"
            if time.monotonic() - self.started >= self.max_seconds
            else "call_limit"
            if self.calls + self._pending_calls >= self.max_calls
            else "estimated_input_token_limit"
            if self.estimated_input_tokens
            + self._pending_tokens
            + estimated_input_tokens
            > self.max_estimated_input_tokens
            else f"{phase}_call_limit"
            if self.phase_calls.get(phase, 0) + self._pending_phase_calls.get(phase, 0)
            >= self.phase_call_limits.get(phase, self.max_calls)
            else None
        )
        if reason is not None:
            if reason not in self.stop_reasons:
                self.stop_reasons.append(reason)
            raise BenchmarkBudgetExceeded(reason)
        self._pending_calls += 1
        self._pending_tokens += estimated_input_tokens
        self._pending_phase_calls[phase] = self._pending_phase_calls.get(phase, 0) + 1

    def record(
        self, operation_id: str, actual_calls: int, estimated_input_tokens: int
    ) -> None:
        phase = self.phase(operation_id)
        self._pending_calls -= 1
        self._pending_tokens -= estimated_input_tokens
        self._pending_phase_calls[phase] -= 1
        self.calls += actual_calls
        self.phase_calls[phase] = self.phase_calls.get(phase, 0) + actual_calls
        self.estimated_input_tokens += estimated_input_tokens * actual_calls

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - (time.monotonic() - self.started))
