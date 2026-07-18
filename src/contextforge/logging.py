"""Central structured, redacted diagnostics for every ContextForge interface."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import re
import sys
import threading
import traceback
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DIAGNOSTIC_SCHEMA_VERSION: Final[int] = 1
TRACE_LEVEL: Final[int] = 5
QUIET_LEVEL: Final[int] = 100
_RECORD_ATTRIBUTE: Final[str] = "contextforge_diagnostic"
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|proxy[_-]?authorization|api[_-]?key|password|passwd|"
    r"cookie|set[_-]?cookie|bearer|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|credential|private[_-]?key|session[_-]?id)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:key|token|secret|password|signature|credential|auth|session)",
    re.IGNORECASE,
)
_PROHIBITED_CONTENT_KEY = re.compile(
    r"(?:^|_)(?:prompt|source_content|source_text|request_body|response_body|"
    r"raw_response|raw_model_output)(?:$|_)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_CREDENTIAL_URL = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)
_MAX_SAFE_TEXT = 4_096
_MAX_COLLECTION_ITEMS = 256

logging.addLevelName(TRACE_LEVEL, "TRACE")


class LogLevel(StrEnum):
    """Stable user-facing diagnostic levels."""

    QUIET = "quiet"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    DEBUG = "debug"
    TRACE = "trace"


class LogFormat(StrEnum):
    """Supported console serialization policies."""

    AUTO = "auto"
    PRETTY = "pretty"
    JSON = "json"


_LEVEL_NUMBERS: Final[dict[LogLevel, int]] = {
    LogLevel.QUIET: QUIET_LEVEL,
    LogLevel.ERROR: logging.ERROR,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.INFO: logging.INFO,
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.TRACE: TRACE_LEVEL,
}
_NUMBER_LEVELS: Final[tuple[tuple[int, LogLevel], ...]] = (
    (logging.ERROR, LogLevel.ERROR),
    (logging.WARNING, LogLevel.WARNING),
    (logging.INFO, LogLevel.INFO),
    (logging.DEBUG, LogLevel.DEBUG),
    (TRACE_LEVEL, LogLevel.TRACE),
)


@dataclass(frozen=True, slots=True)
class LoggingConfiguration:
    """Fully resolved logging policy; it never contains credential values."""

    level: LogLevel = LogLevel.WARNING
    format: LogFormat = LogFormat.AUTO
    file_enabled: bool = False
    file: Path = Path(".contextforge/logs/contextforge.log")
    rotation_bytes: int = 10_000_000
    retained_files: int = 5
    components: Mapping[str, LogLevel] = field(default_factory=dict)
    component_filter: frozenset[str] = frozenset()
    no_color: bool = False
    repository_root: Path | None = None

    def __post_init__(self) -> None:
        if self.rotation_bytes < 1_024:
            raise ValueError("log rotation_bytes must be at least 1024")
        if not 0 <= self.retained_files <= 100:
            raise ValueError("retained_files must be between 0 and 100")
        for component in (*self.components, *self.component_filter):
            if not _valid_component(component):
                raise ValueError(f"invalid log component: {component!r}")

    @property
    def resolved_file(self) -> Path:
        """Resolve a relative file under the repository without requiring it."""

        path = self.file.expanduser()
        if path.is_absolute() or self.repository_root is None:
            return path
        return self.repository_root / path


@dataclass(frozen=True, slots=True)
class DiagnosticContext:
    """Correlation identifiers inherited by nested diagnostic events."""

    operation_id: str | None = None
    top_level_operation_id: str | None = None
    parent_operation_id: str | None = None
    operation_type: str | None = None
    generation_id: str | None = None
    phase_id: str | None = None
    request_id: str | None = None
    parent_request_id: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None


@dataclass(frozen=True, slots=True)
class SafeError:
    """Bounded causal error information that is safe to serialize."""

    code: str
    exception_type: str
    message: str
    transient: bool | None = None
    retryable: bool | None = None
    retry_scheduled: bool | None = None
    fallback_selected: bool | None = None
    cause: SafeError | None = None
    stack_trace: str | None = None

    def to_dict(self, *, include_stack: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
            "transient": self.transient,
            "retryable": self.retryable,
            "retry_scheduled": self.retry_scheduled,
            "fallback_selected": self.fallback_selected,
        }
        if self.cause is not None:
            result["cause"] = self.cause.to_dict(include_stack=False)
        if include_stack and self.stack_trace is not None:
            result["stack_trace"] = self.stack_trace
        return result


@dataclass(frozen=True, slots=True)
class DiagnosticRecord:
    """Versioned machine-readable event shared by CLI and future adapters."""

    timestamp: str
    sequence: int
    level: LogLevel
    component: str
    event: str
    message: str
    operation_id: str | None = None
    top_level_operation_id: str | None = None
    parent_operation_id: str | None = None
    operation_type: str | None = None
    generation_id: str | None = None
    phase_id: str | None = None
    request_id: str | None = None
    parent_request_id: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    duration_ms: int | None = None
    status: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    error: SafeError | None = None

    def to_dict(self, *, include_stack: bool = False) -> dict[str, Any]:
        """Return a detached JSON-serializable representation."""

        result: dict[str, Any] = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "level": self.level.value,
            "component": self.component,
            "event": self.event,
            "message": self.message,
            "operation_id": self.operation_id,
            "top_level_operation_id": self.top_level_operation_id,
            "parent_operation_id": self.parent_operation_id,
            "operation_type": self.operation_type,
            "generation_id": self.generation_id,
            "phase_id": self.phase_id,
            "request_id": self.request_id,
            "parent_request_id": self.parent_request_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "data": redact_mapping(self.data),
            "error": (
                None
                if self.error is None
                else self.error.to_dict(include_stack=include_stack)
            ),
        }
        # Validate the public promise at the boundary, including custom safe data.
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result


@dataclass(slots=True)
class _LoggingState:
    configuration: LoggingConfiguration = field(default_factory=LoggingConfiguration)
    sequence: int = 0
    records: deque[DiagnosticRecord] = field(default_factory=lambda: deque(maxlen=5000))
    warning_emitted: bool = False


_STATE = _LoggingState()
_STATE_LOCK = threading.RLock()
_CONTEXT: contextvars.ContextVar[DiagnosticContext | None] = contextvars.ContextVar(
    "contextforge_diagnostic_context", default=None
)


class _DiagnosticFormatter(logging.Formatter):
    def __init__(self, *, json_lines: bool, include_stack: bool) -> None:
        super().__init__()
        self._json_lines = json_lines
        self._include_stack = include_stack

    def format(self, record: logging.LogRecord) -> str:
        diagnostic = getattr(record, _RECORD_ATTRIBUTE, None)
        if not isinstance(diagnostic, DiagnosticRecord):
            diagnostic = _legacy_record(record)
        if self._json_lines:
            return json.dumps(
                diagnostic.to_dict(include_stack=self._include_stack),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        return _pretty_record(diagnostic)


class _SafeStreamHandler(logging.StreamHandler[TextIO]):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        _report_logging_failure()


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        _report_logging_failure()


class _ConfigurationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        diagnostic = getattr(record, _RECORD_ATTRIBUTE, None)
        if isinstance(diagnostic, DiagnosticRecord):
            return is_enabled(diagnostic.level, diagnostic.component)
        component = record.name.removeprefix("contextforge.") or "application"
        return is_enabled(_level_for_number(record.levelno), component)


def configure_logging(
    configuration: LoggingConfiguration | LogLevel | str = LogLevel.WARNING,
    *,
    stream: TextIO | None = None,
) -> LoggingConfiguration:
    """Install the sole managed stderr/file logging pipeline idempotently.

    A string is accepted for compatibility with the former ``configure_logging``
    API. Managed handlers are replaced; unrelated test/application handlers are
    preserved.
    """

    if not isinstance(configuration, LoggingConfiguration):
        configuration = LoggingConfiguration(level=_coerce_level(configuration))
    target = logging.getLogger("contextforge")
    target.setLevel(TRACE_LEVEL)
    target.propagate = True
    with _STATE_LOCK:
        for handler in tuple(target.handlers):
            if getattr(handler, "_contextforge_managed", False):
                target.removeHandler(handler)
                handler.close()
        _STATE.configuration = configuration
        _STATE.warning_emitted = False
        if configuration.level is not LogLevel.QUIET or configuration.components:
            console_format = configuration.format
            if console_format is LogFormat.AUTO:
                output = sys.stderr if stream is None else stream
                console_format = LogFormat.PRETTY
                # Auto deliberately stays human-readable when redirected; JSON is
                # always explicit so scripts never receive a surprising format.
                del output
            console = _SafeStreamHandler(sys.stderr if stream is None else stream)
            console._contextforge_managed = True  # type: ignore[attr-defined]
            console.setLevel(TRACE_LEVEL)
            console.addFilter(_ConfigurationFilter())
            console.setFormatter(
                _DiagnosticFormatter(
                    json_lines=console_format is LogFormat.JSON,
                    include_stack=False,
                )
            )
            target.addHandler(console)
        if configuration.file_enabled:
            try:
                path = configuration.resolved_file
                path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = _SafeRotatingFileHandler(
                    path,
                    maxBytes=configuration.rotation_bytes,
                    backupCount=configuration.retained_files,
                    encoding="utf-8",
                    delay=True,
                )
                file_handler._contextforge_managed = True  # type: ignore[attr-defined]
                file_handler.setLevel(TRACE_LEVEL)
                file_handler.addFilter(_ConfigurationFilter())
                file_handler.setFormatter(
                    _DiagnosticFormatter(json_lines=True, include_stack=True)
                )
                target.addHandler(file_handler)
            except OSError:
                _report_logging_failure()
    return configuration


def current_configuration() -> LoggingConfiguration:
    """Return the immutable effective logging configuration."""

    with _STATE_LOCK:
        return _STATE.configuration


def color_enabled() -> bool:
    """Allow the shared Rich owner to honor the global no-color policy."""

    return not current_configuration().no_color


def is_enabled(level: LogLevel | str, component: str) -> bool:
    """Cheap level/filter check used before constructing expensive payloads."""

    requested = _coerce_level(level)
    with _STATE_LOCK:
        configuration = _STATE.configuration
    if configuration.component_filter and not any(
        component == item or component.startswith(item + ".")
        for item in configuration.component_filter
    ):
        return False
    threshold = configuration.components.get(component)
    if threshold is None:
        prefix_matches = [
            (key, value)
            for key, value in configuration.components.items()
            if component.startswith(key + ".")
        ]
        threshold = (
            max(prefix_matches, key=lambda item: len(item[0]))[1]
            if prefix_matches
            else configuration.level
        )
    if threshold is LogLevel.QUIET:
        return False
    return _LEVEL_NUMBERS[requested] >= _LEVEL_NUMBERS[threshold]


def emit(
    component: str,
    event: str,
    message: str,
    *,
    level: LogLevel | str = LogLevel.INFO,
    data: Mapping[str, Any] | None = None,
    error: BaseException | SafeError | None = None,
    error_code: str = "internal_error",
    duration_ms: int | None = None,
    status: str | None = None,
    operation_id: str | None = None,
    top_level_operation_id: str | None = None,
    parent_operation_id: str | None = None,
    operation_type: str | None = None,
    generation_id: str | None = None,
    phase_id: str | None = None,
    request_id: str | None = None,
    parent_request_id: str | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
    transient: bool | None = None,
    retryable: bool | None = None,
    retry_scheduled: bool | None = None,
    fallback_selected: bool | None = None,
) -> DiagnosticRecord | None:
    """Emit one safe structured event and return it for application consumers."""

    active_level = _coerce_level(level)
    if not _valid_component(component):
        raise ValueError("diagnostic component must be a dotted lowercase identifier")
    if not _valid_event(event):
        raise ValueError("diagnostic event must be a dotted lowercase identifier")
    sink_enabled = is_enabled(active_level, component)
    context = _current_context()
    safe_error = (
        error
        if isinstance(error, SafeError)
        else safe_error_from_exception(
            error,
            code=error_code,
            transient=transient,
            retryable=retryable,
            retry_scheduled=retry_scheduled,
            fallback_selected=fallback_selected,
        )
        if error is not None
        else None
    )
    with _STATE_LOCK:
        _STATE.sequence += 1
        record = DiagnosticRecord(
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
            sequence=_STATE.sequence,
            level=active_level,
            component=component,
            event=event,
            message=_safe_text(message),
            operation_id=operation_id or context.operation_id,
            top_level_operation_id=(
                top_level_operation_id or context.top_level_operation_id
            ),
            parent_operation_id=parent_operation_id or context.parent_operation_id,
            operation_type=operation_type or context.operation_type,
            generation_id=generation_id or context.generation_id,
            phase_id=phase_id or context.phase_id,
            request_id=request_id or context.request_id,
            parent_request_id=parent_request_id or context.parent_request_id,
            attempt=attempt if attempt is not None else context.attempt,
            max_attempts=(
                max_attempts if max_attempts is not None else context.max_attempts
            ),
            duration_ms=duration_ms,
            status=status,
            data=redact_mapping({} if data is None else data),
            error=safe_error,
        )
        # Fail before logging, not inside a formatter in the active operation.
        record.to_dict(include_stack=False)
        _STATE.records.append(record)
    if sink_enabled:
        logging.getLogger(f"contextforge.{component}").log(
            _LEVEL_NUMBERS[active_level],
            record.message,
            extra={_RECORD_ATTRIBUTE: record},
        )
    return record


@contextlib.contextmanager
def diagnostic_context(**values: Any) -> Iterator[DiagnosticContext]:
    """Temporarily add correlation values without coupling domain APIs to a UI."""

    current = _current_context()
    allowed = {item.name for item in current.__dataclass_fields__.values()}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown diagnostic context field: {sorted(unknown)[0]}")
    active = replace(current, **values)
    token = _CONTEXT.set(active)
    try:
        yield active
    finally:
        _CONTEXT.reset(token)


def recent_records(
    *, operation_id: str | None = None, component: str | None = None
) -> tuple[DiagnosticRecord, ...]:
    """Expose safe structured records to API/tests without parsing console text."""

    with _STATE_LOCK:
        records = tuple(_STATE.records)
    return tuple(
        item
        for item in records
        if (operation_id is None or item.operation_id == operation_id)
        and (
            component is None
            or item.component == component
            or item.component.startswith(component + ".")
        )
    )


def clear_recent_records() -> None:
    """Clear only the in-process observer buffer (primarily for deterministic tests)."""

    with _STATE_LOCK:
        _STATE.records.clear()


def safe_error_from_exception(
    error: BaseException,
    *,
    code: str,
    transient: bool | None = None,
    retryable: bool | None = None,
    retry_scheduled: bool | None = None,
    fallback_selected: bool | None = None,
    include_stack: bool | None = None,
    _depth: int = 0,
) -> SafeError:
    """Build a bounded causal chain without serializing arbitrary exception state."""

    if _depth >= 5:
        cause = None
    else:
        nested = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
        cause = (
            None
            if nested is None
            else safe_error_from_exception(
                nested,
                code="cause",
                include_stack=False,
                _depth=_depth + 1,
            )
        )
    if include_stack is None:
        configuration = current_configuration()
        verbose_file = configuration.file_enabled and (
            configuration.level in {LogLevel.DEBUG, LogLevel.TRACE}
            or any(
                value in {LogLevel.DEBUG, LogLevel.TRACE}
                for value in configuration.components.values()
            )
        )
        capture_stack = verbose_file
    else:
        capture_stack = include_stack
    stack = None
    if capture_stack and _depth == 0:
        stack = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        stack = redact_text(stack)[:32_768]
    return SafeError(
        code=_safe_identifier(code, "internal_error"),
        exception_type=type(error).__name__[:200],
        message=_safe_text(str(error) or type(error).__name__),
        transient=transient,
        retryable=retryable,
        retry_scheduled=retry_scheduled,
        fallback_selected=fallback_selected,
        cause=cause,
        stack_trace=stack,
    )


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact structured fields and normalize values to JSON types."""

    return {
        str(key)[:200]: _redact_value(item, key=str(key), depth=0)
        for key, item in list(value.items())[:_MAX_COLLECTION_ITEMS]
    }


def redact_text(value: str) -> str:
    """Defensively remove token-like text and URL user information."""

    redacted = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
    redacted = _CREDENTIAL_URL.sub(r"\g<scheme>[REDACTED]@", redacted)
    return redacted


def sanitize_url(value: str, *, include_path: bool = True) -> str:
    """Return an origin/path with user-info and sensitive query values removed."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[invalid-url]"
    if not parsed.scheme or not parsed.hostname:
        return "[invalid-url]"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host += f":{parsed.port}"
    query = urlencode(
        [
            (key, "[REDACTED]" if _SENSITIVE_QUERY_KEY.search(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit(
        (parsed.scheme.lower(), host, parsed.path if include_path else "", query, "")
    )


def _redact_value(value: Any, *, key: str, depth: int) -> Any:
    if (
        key.endswith("_fingerprint")
        and isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
    ):
        return value
    if _PROHIBITED_CONTENT_KEY.search(key):
        return "[OMITTED]"
    safe_numeric_token_metric = key.endswith(
        ("_tokens", "_token_budget", "_token_limit")
    ) and (value is None or type(value) in {int, float})
    if _SENSITIVE_KEY.search(key) and not safe_numeric_token_metric:
        return "[REDACTED]"
    if depth >= 8:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            if key.endswith(("url", "endpoint", "origin")) and "://" in value:
                return sanitize_url(value)
            return _safe_text(value)
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return "[NON_FINITE]"
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {
            str(nested_key)[:200]: _redact_value(
                nested_value, key=str(nested_key), depth=depth + 1
            )
            for nested_key, nested_value in list(value.items())[:_MAX_COLLECTION_ITEMS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _redact_value(item, key=key, depth=depth + 1)
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
    return _safe_text(str(value))


def _legacy_record(record: logging.LogRecord) -> DiagnosticRecord:
    with _STATE_LOCK:
        _STATE.sequence += 1
        sequence = _STATE.sequence
    context = _current_context()
    return DiagnosticRecord(
        timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
        sequence=sequence,
        level=_level_for_number(record.levelno),
        component=record.name.removeprefix("contextforge.") or "application",
        event="legacy.message",
        message=_safe_text(record.getMessage()),
        operation_id=context.operation_id,
        operation_type=context.operation_type,
        phase_id=context.phase_id,
    )


def _pretty_record(record: DiagnosticRecord) -> str:
    try:
        timestamp = (
            datetime.fromisoformat(record.timestamp).astimezone().strftime("%H:%M:%S")
        )
    except ValueError:
        timestamp = record.timestamp[:8]
    fields: list[str] = []
    for key, value in record.data.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            fields.append(f"{key}={_pretty_value(value)}")
    suffix = "" if not fields else " " + " ".join(fields[:16])
    return (
        f"{timestamp} {record.level.value.upper():7} {record.component} "
        f"{record.event} {record.message}{suffix}"
    )


def _pretty_value(value: object) -> str:
    if isinstance(value, str):
        return (
            json.dumps(value, ensure_ascii=False)
            if any(char.isspace() for char in value)
            else value
        )
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _safe_text(value: str) -> str:
    bounded = redact_text(value.replace("\x00", "�"))[:_MAX_SAFE_TEXT]
    return "".join(
        character if character.isprintable() or character in "\t" else " "
        for character in bounded
    )


def _current_context() -> DiagnosticContext:
    return _CONTEXT.get() or DiagnosticContext()


def _valid_component(value: str) -> bool:
    return bool(value) and all(_SAFE_KEY.fullmatch(part) for part in value.split("."))


def _valid_event(value: str) -> bool:
    return _valid_component(value) and "." in value


def _safe_identifier(value: str, fallback: str) -> str:
    return value if _SAFE_KEY.fullmatch(value) else fallback


def _coerce_level(value: LogLevel | str) -> LogLevel:
    if isinstance(value, LogLevel):
        return value
    normalized = value.casefold()
    if normalized == "critical":
        normalized = "error"
    try:
        return LogLevel(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported log level: {value}") from exc


def _level_for_number(number: int) -> LogLevel:
    for minimum, level in _NUMBER_LEVELS:
        if number >= minimum:
            return level
    return LogLevel.TRACE


def _report_logging_failure() -> None:
    with _STATE_LOCK:
        if _STATE.warning_emitted:
            return
        _STATE.warning_emitted = True
    try:
        sys.stderr.write(
            "ContextForge warning: diagnostic logging failed; operation continues.\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


__all__ = [
    "DIAGNOSTIC_SCHEMA_VERSION",
    "DiagnosticContext",
    "DiagnosticRecord",
    "LogFormat",
    "LogLevel",
    "LoggingConfiguration",
    "SafeError",
    "clear_recent_records",
    "color_enabled",
    "configure_logging",
    "current_configuration",
    "diagnostic_context",
    "emit",
    "is_enabled",
    "recent_records",
    "redact_mapping",
    "redact_text",
    "safe_error_from_exception",
    "sanitize_url",
]
