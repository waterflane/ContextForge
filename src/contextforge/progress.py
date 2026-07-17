"""Interface-independent application progress contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

PROGRESS_SCHEMA_VERSION: Literal[1] = 1


class ProgressStatus(StrEnum):
    """Lifecycle state represented by a progress event."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressEvent(BaseModel):
    """One immutable, JSON-serializable application progress observation."""

    schema_version: Literal[1] = PROGRESS_SCHEMA_VERSION
    operation_id: str
    operation_type: str
    phase_id: str
    message: str
    completed: float = Field(ge=0, allow_inf_nan=False)
    total: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    percentage: float = Field(ge=0, le=100, allow_inf_nan=False)
    status: ProgressStatus = ProgressStatus.RUNNING
    parent_operation_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    sequence: int = Field(ge=0, strict=True)
    indeterminate: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @field_validator(
        "operation_id", "operation_type", "phase_id", "parent_operation_id"
    )
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        """Keep identifiers bounded, visible, and portable across adapters."""

        if value is None:
            return None
        if (
            not value
            or len(value) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("progress identifiers must be non-empty and bounded")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Require a useful but bounded presentation-neutral phase message."""

        if not value or len(value) > 2_000:
            raise ValueError("progress messages must be non-empty and bounded")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, value: object) -> object:
        """Accept the public wire value while retaining a typed enum in Python."""

        if isinstance(value, str):
            return ProgressStatus(value)
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Reject non-finite values that JSON encoders handle inconsistently."""

        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("progress metadata must be JSON serializable") from exc
        return value

    @model_validator(mode="after")
    def validate_work_and_state(self) -> ProgressEvent:
        """Keep work units and terminal success internally consistent."""

        if self.total is not None and self.completed > self.total:
            raise ValueError("completed work cannot exceed total work")
        if self.status is ProgressStatus.COMPLETED and self.percentage != 100:
            raise ValueError("completed progress events must report 100 percent")
        if self.status is not ProgressStatus.COMPLETED and self.percentage == 100:
            raise ValueError("only completed progress events may report 100 percent")
        if self.operation_id == self.parent_operation_id:
            raise ValueError("an operation cannot be its own parent")
        return self


type ProgressObserver = Callable[[ProgressEvent], None]
"""Synchronous event callback usable by sync and async application code."""


class NoOpProgressObserver:
    """Observer for callers that intentionally discard progress events."""

    def __call__(self, event: ProgressEvent) -> None:
        """Discard one progress event."""


NO_OP_PROGRESS_OBSERVER = NoOpProgressObserver()


class ProgressReporter:
    """Create an isolated monotonic event stream for one operation."""

    def __init__(
        self,
        operation_id: str,
        operation_type: str,
        *,
        observer: ProgressObserver | None = None,
        parent_operation_id: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._operation_id = operation_id
        self._operation_type = operation_type
        self._observer = observer or NO_OP_PROGRESS_OBSERVER
        self._parent_operation_id = parent_operation_id
        self._metadata = dict(metadata or {})
        self._sequence = 0
        self._last_percentage = 0.0
        self._last_completed = 0.0
        self._last_total: float | None = 100.0
        self._last_indeterminate = False
        self._terminal = False
        self._observer_error_count = 0
        self._last_event: ProgressEvent | None = None

    @property
    def last_event(self) -> ProgressEvent | None:
        """Return the most recently created event, whether observed or not."""

        return self._last_event

    @property
    def observer_error_count(self) -> int:
        """Return the number of isolated observer failures."""

        return self._observer_error_count

    def scaled_observer(
        self,
        start_percentage: float,
        end_percentage: float,
        *,
        phase_prefix: str | None = None,
    ) -> ProgressObserver:
        """Map a child event stream into one weighted range of this operation.

        Child terminal states remain phase observations in the parent. The parent
        workflow owns its terminal event, so rollback and exception classification
        continue to happen at the application boundary.
        """

        if (
            not math.isfinite(start_percentage)
            or not math.isfinite(end_percentage)
            or not 0 <= start_percentage <= end_percentage < 100
        ):
            raise ValueError("scaled progress range must be within 0 and 100")

        def observe(event: ProgressEvent) -> None:
            percentage = start_percentage + (
                (end_percentage - start_percentage) * event.percentage / 100
            )
            phase_id = (
                event.phase_id
                if phase_prefix is None
                else f"{phase_prefix}.{event.phase_id}"
            )
            self.report(
                phase_id,
                event.message,
                percentage=percentage,
                metadata={
                    **event.metadata,
                    "child_operation_id": event.operation_id,
                    "child_operation_type": event.operation_type,
                    "child_status": event.status.value,
                },
            )

        return observe

    def report(
        self,
        phase_id: str,
        message: str,
        *,
        percentage: float,
        completed: float | None = None,
        total: float | None = 100.0,
        indeterminate: bool = False,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ProgressEvent:
        """Emit one running event, rejecting regressions within the operation."""

        return self._emit(
            phase_id,
            message,
            percentage=percentage,
            completed=percentage if completed is None else completed,
            total=total,
            status=ProgressStatus.RUNNING,
            indeterminate=indeterminate,
            metadata=metadata,
        )

    def complete(
        self,
        phase_id: str = "completed",
        message: str = "Operation completed.",
        *,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ProgressEvent:
        """Emit the required successful terminal event at 100 percent."""

        completed = self._last_total
        if completed is None:
            completed = self._last_completed
        return self._emit(
            phase_id,
            message,
            percentage=100.0,
            completed=completed,
            total=self._last_total,
            status=ProgressStatus.COMPLETED,
            indeterminate=False,
            metadata=metadata,
        )

    def fail(
        self,
        phase_id: str = "failed",
        message: str = "Operation failed.",
        *,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ProgressEvent:
        """Emit an unsuccessful terminal event without claiming completion."""

        return self._terminal_event(
            ProgressStatus.FAILED, phase_id, message, metadata=metadata
        )

    def cancel(
        self,
        phase_id: str = "cancelled",
        message: str = "Operation cancelled.",
        *,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ProgressEvent:
        """Emit a cancelled terminal event without claiming completion."""

        return self._terminal_event(
            ProgressStatus.CANCELLED, phase_id, message, metadata=metadata
        )

    def _terminal_event(
        self,
        status: ProgressStatus,
        phase_id: str,
        message: str,
        *,
        metadata: Mapping[str, JsonValue] | None,
    ) -> ProgressEvent:
        return self._emit(
            phase_id,
            message,
            percentage=self._last_percentage,
            completed=self._last_completed,
            total=self._last_total,
            status=status,
            indeterminate=self._last_indeterminate,
            metadata=metadata,
        )

    def _emit(
        self,
        phase_id: str,
        message: str,
        *,
        percentage: float,
        completed: float,
        total: float | None,
        status: ProgressStatus,
        indeterminate: bool,
        metadata: Mapping[str, JsonValue] | None,
    ) -> ProgressEvent:
        if self._terminal:
            raise RuntimeError("progress operation already has a terminal event")
        if math.isfinite(percentage) and percentage < self._last_percentage:
            raise ValueError("progress percentage cannot decrease")
        event_metadata = {**self._metadata, **dict(metadata or {})}
        event = ProgressEvent(
            operation_id=self._operation_id,
            operation_type=self._operation_type,
            phase_id=phase_id,
            message=message,
            completed=completed,
            total=total,
            percentage=percentage,
            status=status,
            parent_operation_id=self._parent_operation_id,
            metadata=event_metadata,
            sequence=self._sequence,
            indeterminate=indeterminate,
        )
        self._sequence += 1
        self._last_percentage = event.percentage
        self._last_completed = event.completed
        self._last_total = event.total
        self._last_indeterminate = event.indeterminate
        self._terminal = event.status is not ProgressStatus.RUNNING
        self._last_event = event
        try:
            self._observer(event)
        except BaseException:  # Observers never control operation lifecycle.
            self._observer_error_count += 1
        return event


__all__ = [
    "NO_OP_PROGRESS_OBSERVER",
    "PROGRESS_SCHEMA_VERSION",
    "NoOpProgressObserver",
    "ProgressEvent",
    "ProgressObserver",
    "ProgressReporter",
    "ProgressStatus",
]
