"""Interface-independent application progress contracts."""

from __future__ import annotations

import json
import math
import time
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

from contextforge.logging import LogLevel, emit

PROGRESS_SCHEMA_VERSION: Literal[3] = 3


class ProgressStatus(StrEnum):
    """Lifecycle state represented by a progress event."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressActivity(StrEnum):
    """Current activity state within a running progress phase."""

    IDLE = "idle"
    ACTIVE = "active"
    WAITING = "waiting"


class ProgressEvent(BaseModel):
    """One immutable, JSON-serializable application progress observation."""

    schema_version: Literal[1, 2, 3] = PROGRESS_SCHEMA_VERSION
    operation_id: str
    operation_type: str
    phase_id: str
    message: str
    completed: float = Field(ge=0, allow_inf_nan=False)
    total: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    percentage: float = Field(ge=0, le=100, allow_inf_nan=False)
    status: ProgressStatus = ProgressStatus.RUNNING
    top_level_operation_id: str | None = None
    parent_operation_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    sequence: int = Field(ge=0, strict=True)
    indeterminate: bool = False
    overall_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    phase_label: str
    phase_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    phase_weight: float = Field(ge=0, le=100, allow_inf_nan=False)
    completed_units: float = Field(ge=0, allow_inf_nan=False)
    total_units: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit_type: str = "percent"
    current_item: str | None = None
    last_completed_item: str | None = None
    last_failed_item: str | None = None
    active_items: tuple[str, ...] = ()
    active_item_count: int = Field(default=0, ge=0, strict=True)
    reused_units: int = Field(default=0, ge=0, strict=True)
    skipped_units: int = Field(default=0, ge=0, strict=True)
    failed_units: int = Field(default=0, ge=0, strict=True)
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    activity: ProgressActivity = ProgressActivity.IDLE
    planned_units: int = Field(default=0, ge=0, strict=True)
    processed_units: int = Field(default=0, ge=0, strict=True)
    succeeded_units: int = Field(default=0, ge=0, strict=True)
    fallback_units: int = Field(default=0, ge=0, strict=True)
    active_units: int = Field(default=0, ge=0, strict=True)
    current_attempt: int | None = Field(default=None, ge=1, strict=True)
    max_attempts: int | None = Field(default=None, ge=1, strict=True)
    lifecycle_state: str = "idle"
    safe_error_code: str | None = None
    safe_error_message: str | None = None
    request_elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    operation_elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    analyzer_kind: str | None = None
    estimated_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_token_budget: int | None = Field(default=None, ge=1, strict=True)
    input_truncated: bool = False
    configured_context_window: int | None = Field(default=None, ge=1_024, strict=True)
    schema_overhead_tokens: int | None = Field(default=None, ge=0, strict=True)
    safety_margin_tokens: int | None = Field(default=None, ge=0, strict=True)
    estimated_total_tokens: int | None = Field(default=None, ge=0, strict=True)

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def populate_compatibility_fields(cls, value: object) -> object:
        """Accept schema-v1 payloads while emitting the richer v2 shape."""

        if not isinstance(value, Mapping):
            return value
        values = dict(value)
        percentage = values.get("percentage", 0)
        completed = values.get("completed", percentage)
        total = values.get("total", 100)
        message = values.get("message", "Progress")
        active_items = values.get("active_items", ())
        metadata = values.get("metadata", {})
        if isinstance(metadata, Mapping):
            for field in (
                "configured_context_window",
                "schema_overhead_tokens",
                "safety_margin_tokens",
                "estimated_total_tokens",
            ):
                raw = metadata.get(field)
                if type(raw) is int:
                    values.setdefault(field, raw)
        values.setdefault("overall_percent", percentage)
        values.setdefault("phase_label", str(message).rstrip(".") or str(message))
        values.setdefault("phase_percent", percentage)
        values.setdefault("phase_weight", 100)
        values.setdefault("completed_units", completed)
        values.setdefault("total_units", total)
        values.setdefault("unit_type", "percent")
        values.setdefault("active_item_count", len(active_items))
        values.setdefault("active_units", values.get("active_item_count", 0))
        values.setdefault(
            "operation_elapsed_seconds",
            max(
                values.get("elapsed_seconds", 0),
                values.get("request_elapsed_seconds", 0),
            ),
        )
        return values

    @field_validator(
        "operation_id",
        "operation_type",
        "phase_id",
        "top_level_operation_id",
        "parent_operation_id",
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

    @field_validator("phase_label", "unit_type", "lifecycle_state")
    @classmethod
    def validate_label(cls, value: str) -> str:
        """Keep display-neutral phase and unit labels bounded and printable."""

        if (
            not value
            or len(value) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("progress labels must be non-empty and bounded")
        return value

    @field_validator("analyzer_kind")
    @classmethod
    def validate_optional_label(cls, value: str | None) -> str | None:
        """Keep optional analyzer identities complete and presentation-neutral."""

        return None if value is None else cls.validate_label(value)

    @field_validator(
        "current_item",
        "last_completed_item",
        "last_failed_item",
        "safe_error_code",
        "safe_error_message",
    )
    @classmethod
    def validate_item(cls, value: str | None) -> str | None:
        """Allow portable paths without allowing terminal control content."""

        if value is not None and (
            not value
            or len(value) > 2_000
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("progress items must be non-empty and bounded")
        return value

    @field_validator("active_items", mode="before")
    @classmethod
    def validate_active_items(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, list):
            value = tuple(value)
        if not isinstance(value, tuple):
            raise ValueError("active progress items must be a tuple")
        if len(value) > 32:
            raise ValueError("active progress items must be bounded")
        for item in value:
            if not isinstance(item, str):
                raise ValueError("active progress items must be strings")
            cls.validate_item(item)
        return value

    @field_validator("status", "activity", mode="before")
    @classmethod
    def validate_status(cls, value: object) -> object:
        """Accept the public wire value while retaining a typed enum in Python."""

        if isinstance(value, str):
            try:
                return ProgressStatus(value)
            except ValueError:
                return ProgressActivity(value)
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
        if self.total_units is not None and self.completed_units > self.total_units:
            raise ValueError("completed progress units cannot exceed total units")
        if self.overall_percent != self.percentage:
            raise ValueError("overall_percent must match percentage")
        if self.active_item_count < len(self.active_items):
            raise ValueError("active item count cannot be smaller than active items")
        if self.active_units != self.active_item_count:
            raise ValueError("active_units must match active_item_count")
        if self.processed_units > self.planned_units:
            raise ValueError("processed units cannot exceed planned units")
        terminal_units = (
            self.succeeded_units
            + self.failed_units
            + self.skipped_units
            + self.reused_units
        )
        if (
            self.planned_units or self.processed_units
        ) and terminal_units > self.processed_units:
            raise ValueError("semantic terminal counters cannot exceed processed units")
        if self.fallback_units > self.succeeded_units:
            raise ValueError("fallback units must be a subset of succeeded units")
        if (self.current_attempt is None) != (self.max_attempts is None):
            raise ValueError("attempt counters must be provided together")
        if (
            self.current_attempt is not None
            and self.max_attempts is not None
            and self.current_attempt > self.max_attempts
        ):
            raise ValueError("current attempt cannot exceed maximum attempts")
        if self.operation_elapsed_seconds < self.request_elapsed_seconds:
            raise ValueError(
                "request elapsed time cannot exceed operation elapsed time"
            )
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
        top_level_operation_id: str | None = None,
        parent_operation_id: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._operation_id = operation_id
        self._operation_type = operation_type
        self._observer = observer or NO_OP_PROGRESS_OBSERVER
        self._top_level_operation_id = (
            top_level_operation_id
            if top_level_operation_id is not None
            else operation_id
            if parent_operation_id is None
            else None
        )
        self._parent_operation_id = parent_operation_id
        self._metadata = dict(metadata or {})
        self._clock = clock
        self._started = clock()
        self._sequence = 0
        self._last_percentage = 0.0
        self._last_completed = 0.0
        self._last_total: float | None = 100.0
        self._last_indeterminate = False
        self._last_phase_label = "Operation"
        self._last_phase_percent = 0.0
        self._last_phase_weight = 100.0
        self._last_completed_units = 0.0
        self._last_total_units: float | None = 100.0
        self._last_unit_type = "percent"
        self._last_current_item: str | None = None
        self._last_completed_item: str | None = None
        self._last_failed_item: str | None = None
        self._last_active_items: tuple[str, ...] = ()
        self._last_active_item_count = 0
        self._last_reused_units = 0
        self._last_skipped_units = 0
        self._last_failed_units = 0
        self._last_activity = ProgressActivity.IDLE
        self._last_planned_units = 0
        self._last_processed_units = 0
        self._last_succeeded_units = 0
        self._last_fallback_units = 0
        self._last_current_attempt: int | None = None
        self._last_max_attempts: int | None = None
        self._last_lifecycle_state = "idle"
        self._last_safe_error_code: str | None = None
        self._last_safe_error_message: str | None = None
        self._last_request_elapsed_seconds = 0.0
        self._last_analyzer_kind: str | None = None
        self._last_estimated_input_tokens: int | None = None
        self._last_output_token_budget: int | None = None
        self._last_input_truncated = False
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
                phase_label=event.phase_label,
                phase_percent=event.phase_percent,
                phase_weight=(
                    event.phase_weight * (end_percentage - start_percentage) / 100
                ),
                completed_units=event.completed_units,
                total_units=event.total_units,
                unit_type=event.unit_type,
                current_item=event.current_item,
                last_completed_item=event.last_completed_item,
                last_failed_item=event.last_failed_item,
                active_items=event.active_items,
                active_item_count=event.active_item_count,
                reused_units=event.reused_units,
                skipped_units=event.skipped_units,
                failed_units=event.failed_units,
                activity=event.activity,
                planned_units=event.planned_units,
                processed_units=event.processed_units,
                succeeded_units=event.succeeded_units,
                fallback_units=event.fallback_units,
                current_attempt=event.current_attempt,
                max_attempts=event.max_attempts,
                lifecycle_state=event.lifecycle_state,
                safe_error_code=event.safe_error_code,
                safe_error_message=event.safe_error_message,
                request_elapsed_seconds=event.request_elapsed_seconds,
                analyzer_kind=event.analyzer_kind,
                estimated_input_tokens=event.estimated_input_tokens,
                output_token_budget=event.output_token_budget,
                input_truncated=event.input_truncated,
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
        phase_label: str | None = None,
        phase_percent: float | None = None,
        phase_weight: float = 100.0,
        completed_units: float | None = None,
        total_units: float | None = None,
        unit_type: str = "percent",
        current_item: str | None = None,
        last_completed_item: str | None = None,
        last_failed_item: str | None = None,
        active_items: tuple[str, ...] = (),
        active_item_count: int | None = None,
        reused_units: int = 0,
        skipped_units: int = 0,
        failed_units: int = 0,
        activity: ProgressActivity = ProgressActivity.IDLE,
        planned_units: int = 0,
        processed_units: int = 0,
        succeeded_units: int = 0,
        fallback_units: int = 0,
        current_attempt: int | None = None,
        max_attempts: int | None = None,
        lifecycle_state: str = "active",
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
        request_elapsed_seconds: float = 0,
        analyzer_kind: str | None = None,
        estimated_input_tokens: int | None = None,
        output_token_budget: int | None = None,
        input_truncated: bool = False,
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
            phase_label=phase_label or message.rstrip(".") or message,
            phase_percent=percentage if phase_percent is None else phase_percent,
            phase_weight=phase_weight,
            completed_units=(
                (percentage if completed is None else completed)
                if completed_units is None
                else completed_units
            ),
            total_units=total if total_units is None else total_units,
            unit_type=unit_type,
            current_item=current_item,
            last_completed_item=last_completed_item,
            last_failed_item=last_failed_item,
            active_items=active_items,
            active_item_count=(
                len(active_items) if active_item_count is None else active_item_count
            ),
            reused_units=reused_units,
            skipped_units=skipped_units,
            failed_units=failed_units,
            activity=activity,
            planned_units=planned_units,
            processed_units=processed_units,
            succeeded_units=succeeded_units,
            fallback_units=fallback_units,
            current_attempt=current_attempt,
            max_attempts=max_attempts,
            lifecycle_state=lifecycle_state,
            safe_error_code=safe_error_code,
            safe_error_message=safe_error_message,
            request_elapsed_seconds=request_elapsed_seconds,
            analyzer_kind=analyzer_kind,
            estimated_input_tokens=estimated_input_tokens,
            output_token_budget=output_token_budget,
            input_truncated=input_truncated,
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
            phase_label="Completed",
            phase_percent=100,
            phase_weight=self._last_phase_weight,
            completed_units=(
                self._last_total_units
                if self._last_total_units is not None
                else self._last_completed_units
            ),
            total_units=self._last_total_units,
            unit_type=self._last_unit_type,
            current_item=None,
            last_completed_item=self._last_completed_item,
            last_failed_item=self._last_failed_item,
            active_items=(),
            active_item_count=0,
            reused_units=self._last_reused_units,
            skipped_units=self._last_skipped_units,
            failed_units=self._last_failed_units,
            activity=ProgressActivity.IDLE,
            planned_units=self._last_planned_units,
            processed_units=self._last_processed_units,
            succeeded_units=self._last_succeeded_units,
            fallback_units=self._last_fallback_units,
            current_attempt=None,
            max_attempts=None,
            lifecycle_state="completed",
            safe_error_code=self._last_safe_error_code,
            safe_error_message=self._last_safe_error_message,
            request_elapsed_seconds=0,
            analyzer_kind=self._last_analyzer_kind,
            estimated_input_tokens=self._last_estimated_input_tokens,
            output_token_budget=self._last_output_token_budget,
            input_truncated=self._last_input_truncated,
            metadata=metadata,
        )

    def fail(
        self,
        phase_id: str = "failed",
        message: str = "Operation failed.",
        *,
        metadata: Mapping[str, JsonValue] | None = None,
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
    ) -> ProgressEvent:
        """Emit an unsuccessful terminal event without claiming completion."""

        if safe_error_code is not None:
            self._last_safe_error_code = safe_error_code
        if safe_error_message is not None:
            self._last_safe_error_message = safe_error_message
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
            phase_label=self._last_phase_label,
            phase_percent=self._last_phase_percent,
            phase_weight=self._last_phase_weight,
            completed_units=self._last_completed_units,
            total_units=self._last_total_units,
            unit_type=self._last_unit_type,
            current_item=None,
            last_completed_item=self._last_completed_item,
            last_failed_item=self._last_failed_item,
            active_items=(),
            active_item_count=0,
            reused_units=self._last_reused_units,
            skipped_units=self._last_skipped_units,
            failed_units=self._last_failed_units,
            activity=ProgressActivity.IDLE,
            planned_units=self._last_planned_units,
            processed_units=self._last_processed_units,
            succeeded_units=self._last_succeeded_units,
            fallback_units=self._last_fallback_units,
            current_attempt=self._last_current_attempt,
            max_attempts=self._last_max_attempts,
            lifecycle_state=status.value,
            safe_error_code=self._last_safe_error_code,
            safe_error_message=self._last_safe_error_message,
            request_elapsed_seconds=self._last_request_elapsed_seconds,
            analyzer_kind=self._last_analyzer_kind,
            estimated_input_tokens=self._last_estimated_input_tokens,
            output_token_budget=self._last_output_token_budget,
            input_truncated=self._last_input_truncated,
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
        phase_label: str,
        phase_percent: float,
        phase_weight: float,
        completed_units: float,
        total_units: float | None,
        unit_type: str,
        current_item: str | None,
        last_completed_item: str | None,
        last_failed_item: str | None,
        active_items: tuple[str, ...],
        active_item_count: int,
        reused_units: int,
        skipped_units: int,
        failed_units: int,
        activity: ProgressActivity,
        planned_units: int,
        processed_units: int,
        succeeded_units: int,
        fallback_units: int,
        current_attempt: int | None,
        max_attempts: int | None,
        lifecycle_state: str,
        safe_error_code: str | None,
        safe_error_message: str | None,
        request_elapsed_seconds: float,
        analyzer_kind: str | None,
        estimated_input_tokens: int | None,
        output_token_budget: int | None,
        input_truncated: bool,
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
            top_level_operation_id=self._top_level_operation_id,
            parent_operation_id=self._parent_operation_id,
            metadata=event_metadata,
            sequence=self._sequence,
            indeterminate=indeterminate,
            overall_percent=percentage,
            phase_label=phase_label,
            phase_percent=phase_percent,
            phase_weight=phase_weight,
            completed_units=completed_units,
            total_units=total_units,
            unit_type=unit_type,
            current_item=current_item,
            last_completed_item=last_completed_item,
            last_failed_item=last_failed_item,
            active_items=active_items,
            active_item_count=active_item_count,
            reused_units=reused_units,
            skipped_units=skipped_units,
            failed_units=failed_units,
            elapsed_seconds=max(0.0, self._clock() - self._started),
            activity=activity,
            planned_units=planned_units,
            processed_units=processed_units,
            succeeded_units=succeeded_units,
            fallback_units=fallback_units,
            active_units=active_item_count,
            current_attempt=current_attempt,
            max_attempts=max_attempts,
            lifecycle_state=lifecycle_state,
            safe_error_code=safe_error_code,
            safe_error_message=safe_error_message,
            request_elapsed_seconds=request_elapsed_seconds,
            operation_elapsed_seconds=max(0.0, self._clock() - self._started),
            analyzer_kind=analyzer_kind,
            estimated_input_tokens=estimated_input_tokens,
            output_token_budget=output_token_budget,
            input_truncated=input_truncated,
        )
        self._sequence += 1
        self._last_percentage = event.percentage
        self._last_completed = event.completed
        self._last_total = event.total
        self._last_indeterminate = event.indeterminate
        self._last_phase_label = event.phase_label
        self._last_phase_percent = event.phase_percent
        self._last_phase_weight = event.phase_weight
        self._last_completed_units = event.completed_units
        self._last_total_units = event.total_units
        self._last_unit_type = event.unit_type
        self._last_current_item = event.current_item
        self._last_completed_item = event.last_completed_item
        self._last_failed_item = event.last_failed_item
        self._last_active_items = event.active_items
        self._last_active_item_count = event.active_item_count
        self._last_reused_units = event.reused_units
        self._last_skipped_units = event.skipped_units
        self._last_failed_units = event.failed_units
        self._last_activity = event.activity
        self._last_planned_units = event.planned_units
        self._last_processed_units = event.processed_units
        self._last_succeeded_units = event.succeeded_units
        self._last_fallback_units = event.fallback_units
        self._last_current_attempt = event.current_attempt
        self._last_max_attempts = event.max_attempts
        self._last_lifecycle_state = event.lifecycle_state
        self._last_safe_error_code = event.safe_error_code
        self._last_safe_error_message = event.safe_error_message
        self._last_request_elapsed_seconds = event.request_elapsed_seconds
        self._last_analyzer_kind = event.analyzer_kind
        self._last_estimated_input_tokens = event.estimated_input_tokens
        self._last_output_token_budget = event.output_token_budget
        self._last_input_truncated = event.input_truncated
        self._terminal = event.status is not ProgressStatus.RUNNING
        self._last_event = event
        if event.sequence == 0:
            emit(
                "progress",
                "operation.started",
                "Operation started.",
                level=LogLevel.INFO,
                operation_id=event.operation_id,
                top_level_operation_id=event.top_level_operation_id,
                parent_operation_id=event.parent_operation_id,
                operation_type=event.operation_type,
                phase_id=event.phase_id,
                status="running",
                data={},
            )
        if event.status is not ProgressStatus.RUNNING:
            terminal_event = {
                ProgressStatus.COMPLETED: "operation.completed",
                ProgressStatus.FAILED: "operation.failed",
                ProgressStatus.CANCELLED: "operation.cancelled",
            }[event.status]
            terminal_level = (
                LogLevel.INFO
                if event.status is ProgressStatus.COMPLETED
                else LogLevel.ERROR
                if event.status is ProgressStatus.FAILED
                else LogLevel.WARNING
            )
            emit(
                "progress",
                terminal_event,
                event.message,
                level=terminal_level,
                operation_id=event.operation_id,
                top_level_operation_id=event.top_level_operation_id,
                parent_operation_id=event.parent_operation_id,
                operation_type=event.operation_type,
                phase_id=event.phase_id,
                duration_ms=round(event.operation_elapsed_seconds * 1_000),
                status=event.status.value,
                data={
                    **(
                        {"safe_error_code": event.safe_error_code}
                        if event.safe_error_code is not None
                        else {}
                    ),
                    **(
                        {"safe_error_message": event.safe_error_message}
                        if event.safe_error_message is not None
                        else {}
                    ),
                    "processed_units": event.processed_units,
                    "succeeded_units": event.succeeded_units,
                    "failed_units": event.failed_units,
                    "fallback_units": event.fallback_units,
                },
            )
        try:
            self._observer(event)
        except BaseException:  # Observers never control operation lifecycle.
            self._observer_error_count += 1
        return event


__all__ = [
    "NO_OP_PROGRESS_OBSERVER",
    "PROGRESS_SCHEMA_VERSION",
    "NoOpProgressObserver",
    "ProgressActivity",
    "ProgressEvent",
    "ProgressObserver",
    "ProgressReporter",
    "ProgressStatus",
]
