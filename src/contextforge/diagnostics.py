"""Safe run-diagnostic summaries over the existing ``.contextforge/runs`` state."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from contextforge.logging import DiagnosticRecord, LogLevel, emit, redact_mapping

DIAGNOSTIC_SUMMARY_SCHEMA_VERSION: Final[int] = 1
MAX_SUMMARY_BYTES: Final[int] = 2_000_000
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_SUMMARY_PREFIX = "diagnostic-"
_SUMMARY_SUFFIX = ".json"


class DiagnosticStoreError(OSError):
    """Raised when a safe read-only diagnostic lookup cannot be completed."""


@dataclass(frozen=True, slots=True)
class OperationDiagnosticSummary:
    """Compact, secret-free terminal operation record."""

    operation_id: str
    command: str
    operation_type: str
    started_at: str
    ended_at: str
    duration_ms: int
    outcome: Literal["completed", "failed", "cancelled"]
    generation_id: str | None
    provider_models: tuple[dict[str, str], ...]
    context_windows: tuple[dict[str, Any], ...]
    budget_breakdowns: tuple[dict[str, Any], ...]
    request_count: int
    estimated_token_total: int
    actual_input_tokens: int
    actual_output_tokens: int
    retry_count: int
    failed_phases: tuple[str, ...]
    fallback_phases: tuple[str, ...]
    final_error_code: str | None
    error_chain: dict[str, Any] | None
    remediation_hints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": DIAGNOSTIC_SUMMARY_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "command": self.command,
            "operation_type": self.operation_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "generation_id": self.generation_id,
            "provider_models": list(self.provider_models),
            "context_windows": list(self.context_windows),
            "budget_breakdowns": list(self.budget_breakdowns),
            "request_count": self.request_count,
            "estimated_token_total": self.estimated_token_total,
            "actual_input_tokens": self.actual_input_tokens,
            "actual_output_tokens": self.actual_output_tokens,
            "retry_count": self.retry_count,
            "failed_phases": list(self.failed_phases),
            "fallback_phases": list(self.fallback_phases),
            "final_error_code": self.final_error_code,
            "error_chain": self.error_chain,
            "remediation_hints": list(self.remediation_hints),
        }
        safe = redact_mapping(value)
        json.dumps(safe, ensure_ascii=False, allow_nan=False)
        return safe


def summarize_operation(
    operation_id: str,
    command: str,
    records: tuple[DiagnosticRecord, ...],
    *,
    operation_type: str,
    outcome: Literal["completed", "failed", "cancelled"],
    generation_id: str | None = None,
    final_error_code: str | None = None,
    remediation_hints: tuple[str, ...] = (),
) -> OperationDiagnosticSummary:
    """Aggregate safe records without retaining prompts, source, or responses."""

    if not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation_id is not portable")
    ordered = tuple(sorted(records, key=lambda item: item.sequence))
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    started = ordered[0].timestamp if ordered else now
    ended = ordered[-1].timestamp if ordered else now
    duration_ms = max(
        (
            _duration_between(started, ended),
            *(item.duration_ms or 0 for item in ordered),
        )
    )
    budgets = [
        _budget_summary(item.data)
        for item in ordered
        if item.event in {"budget.calculated", "budget.rejected"}
    ]
    unique_budgets = _unique_dicts(budgets)[:50]
    context_windows = _unique_dicts(
        [
            {
                key: value
                for key, value in item.data.items()
                if "context_window" in key or key in {"provider", "model", "explicit"}
            }
            for item in ordered
            if item.event == "config.value_resolved"
        ]
    )[:20]
    providers = _unique_dicts(
        [
            {
                "provider": str(item.data.get("provider", "unknown")),
                "model": str(item.data.get("model", "unknown")),
            }
            for item in ordered
            if item.event == "budget.calculated"
        ]
    )
    errors = [item for item in ordered if item.error is not None]
    terminal_error = errors[-1].error if errors else None
    error_code = final_error_code
    if error_code is None and terminal_error is not None:
        error_code = terminal_error.code
    estimated_total = sum(
        cast(int, value.get("estimated_total_tokens", 0))
        for value in unique_budgets
        if type(value.get("estimated_total_tokens")) is int
    )
    return OperationDiagnosticSummary(
        operation_id=operation_id,
        command=command[:500],
        operation_type=operation_type,
        started_at=started,
        ended_at=ended,
        duration_ms=duration_ms,
        outcome=outcome,
        generation_id=generation_id,
        provider_models=tuple(cast(dict[str, str], item) for item in providers),
        context_windows=tuple(context_windows),
        budget_breakdowns=tuple(unique_budgets),
        request_count=sum(item.event == "provider.request.started" for item in ordered),
        estimated_token_total=estimated_total,
        actual_input_tokens=_sum_metric(ordered, "provider_input_tokens"),
        actual_output_tokens=_sum_metric(ordered, "provider_output_tokens"),
        retry_count=sum(item.event == "provider.retry.scheduled" for item in ordered),
        failed_phases=tuple(
            dict.fromkeys(
                item.phase_id
                for item in ordered
                if item.status == "failed" and item.phase_id is not None
            )
        ),
        fallback_phases=tuple(
            dict.fromkeys(
                item.phase_id or item.component
                for item in ordered
                if item.event.endswith("fallback.selected")
            )
        ),
        final_error_code=error_code,
        error_chain=(
            None
            if terminal_error is None
            else terminal_error.to_dict(include_stack=False)
        ),
        remediation_hints=remediation_hints,
    )


def persist_summary(
    repository_root: str | Path, summary: OperationDiagnosticSummary
) -> bool:
    """Atomically persist a summary; logging failure never controls the operation."""

    temporary: Path | None = None
    try:
        runs = _runs_directory(repository_root, create=True)
        destination = runs / _summary_filename(summary.operation_id)
        encoded = (
            json.dumps(
                summary.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_SUMMARY_BYTES:
            raise DiagnosticStoreError("diagnostic summary exceeds its byte limit")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=runs,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        return True
    except (OSError, ValueError) as exc:
        emit(
            "storage",
            "diagnostic.persistence_failed",
            "Unable to persist the safe operation diagnostic summary.",
            level=LogLevel.WARNING,
            operation_id=summary.operation_id,
            operation_type=summary.operation_type,
            error=exc,
            error_code="persistence_failure",
            data={"previous_active_generation_intact": True},
        )
        return False
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def load_summary(repository_root: str | Path, operation_id: str) -> dict[str, Any]:
    """Read and validate one bounded summary without mutating repository state."""

    if not _OPERATION_ID.fullmatch(operation_id):
        raise DiagnosticStoreError("operation ID is invalid")
    return _read_summary(
        _runs_directory(repository_root, create=False) / _summary_filename(operation_id)
    )


def load_last_summary(
    repository_root: str | Path, *, failed_only: bool = False
) -> dict[str, Any]:
    """Return the newest safe summary by its own terminal timestamp."""

    runs = _runs_directory(repository_root, create=False)
    values: list[dict[str, Any]] = []
    try:
        paths = tuple(runs.glob(f"{_SUMMARY_PREFIX}*{_SUMMARY_SUFFIX}"))
    except OSError as exc:
        raise DiagnosticStoreError("unable to list diagnostic summaries") from exc
    for path in paths:
        try:
            value = _read_summary(path)
        except DiagnosticStoreError:
            continue
        if not failed_only or value.get("outcome") == "failed":
            values.append(value)
    if not values:
        raise DiagnosticStoreError("no operation diagnostic summary is available")
    return max(values, key=lambda item: str(item.get("ended_at", "")))


def _runs_directory(repository_root: str | Path, *, create: bool) -> Path:
    try:
        root = Path(repository_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DiagnosticStoreError("repository root is unavailable") from exc
    if not root.is_dir():
        raise DiagnosticStoreError("repository root is not a directory")
    state = root / ".contextforge"
    runs = state / "runs"
    for path in (state, runs):
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise DiagnosticStoreError("diagnostic storage path is unsafe")
        if create and not path.exists():
            path.mkdir()
    if not runs.is_dir():
        raise DiagnosticStoreError("no operation diagnostic summary is available")
    return runs


def _read_summary(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise DiagnosticStoreError("diagnostic summary path is unsafe")
        data = path.read_bytes()
    except OSError as exc:
        raise DiagnosticStoreError("unable to read diagnostic summary") from exc
    if len(data) > MAX_SUMMARY_BYTES:
        raise DiagnosticStoreError("diagnostic summary exceeds its byte limit")
    try:
        value = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticStoreError("diagnostic summary is malformed") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != DIAGNOSTIC_SUMMARY_SCHEMA_VERSION
        or not isinstance(value.get("operation_id"), str)
    ):
        raise DiagnosticStoreError("diagnostic summary schema is unsupported")
    return cast(dict[str, Any], value)


def _summary_filename(operation_id: str) -> str:
    if not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation ID is invalid")
    import hashlib

    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:32]
    return f"{_SUMMARY_PREFIX}{digest}{_SUMMARY_SUFFIX}"


def _duration_between(start: str, end: str) -> int:
    try:
        return max(
            0,
            round(
                (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
                * 1_000
            ),
        )
    except ValueError:
        return 0


def _budget_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = dict(value)
    keys = (
        "task_kind",
        "provider",
        "model",
        "effective_context_window",
        "effective_context_window_source",
        "provider_reported_context_window",
        "model_metadata_context_window",
        "estimated_system_tokens",
        "estimated_user_tokens",
        "estimated_source_tokens",
        "estimated_index_tokens",
        "estimated_schema_tokens",
        "requested_output_tokens",
        "protocol_overhead_tokens",
        "safety_margin_tokens",
        "estimated_total_tokens",
        "remaining_tokens",
        "budget_ratio",
        "input_truncated",
        "input_chunked",
        "request_dispatched",
        "selected_source_paths",
        "selected_index_record_count",
        "result",
        "error_code",
        "remediation",
    )
    return {key: value.get(key) for key in keys if key in value}


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        safe = redact_mapping(value)
        key = json.dumps(
            safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if key not in seen:
            seen.add(key)
            result.append(safe)
    return result


def _sum_metric(records: tuple[DiagnosticRecord, ...], key: str) -> int:
    total = 0
    for item in records:
        value = item.data.get(key)
        if type(value) is int:
            total += value
    return total


__all__ = [
    "DIAGNOSTIC_SUMMARY_SCHEMA_VERSION",
    "DiagnosticStoreError",
    "OperationDiagnosticSummary",
    "load_last_summary",
    "load_summary",
    "persist_summary",
    "summarize_operation",
]
