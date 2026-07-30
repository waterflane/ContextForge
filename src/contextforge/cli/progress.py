"""Single-owner stderr CLI adapter for structured application progress events."""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import IO, TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.file_proxy import FileProxy
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from typer.rich_utils import _get_rich_console

from contextforge.logging import color_enabled
from contextforge.progress import ProgressActivity, ProgressEvent, ProgressStatus

_OPERATION_LABELS = {
    "repository.index.build": "Indexing repository",
    "repository.index.update": "Updating repository index",
    "repository.context.suggest": "Suggesting context",
    "repository.handoff.create": "Creating automatic context",
}
_LABEL_WIDTH = 16


class ProgressMode(StrEnum):
    """Terminal progress rendering policy."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class CLIProgressRenderer:
    """Own at most one Rich live display for one top-level CLI operation."""

    _ownership_lock = threading.RLock()
    _active_owners: dict[int, CLIProgressRenderer] = {}

    def __init__(
        self,
        mode: ProgressMode | str | TextIO = ProgressMode.AUTO,
        *,
        stream: TextIO | None = None,
        console: Console | None = None,
        stdout: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(mode, (str, ProgressMode)):
            if stream is not None:
                raise ValueError("progress stream was provided twice")
            stream = mode
            mode = ProgressMode.AUTO
        if stream is not None and console is not None:
            raise ValueError("progress accepts either a stream or a console")
        self.mode = ProgressMode(mode)
        self._console = console or self._console_for(stream)
        self._stream = self._console.file
        self._stdout = sys.stdout if stdout is None else stdout
        self._clock = clock
        self._state_lock = threading.RLock()
        self._started: float | None = None
        self._event: ProgressEvent | None = None
        self._live: Live | None = None
        self._owner: CLIProgressRenderer | None = None
        self._owns_live = False
        self._closed = False
        self._redirected_handlers: list[
            tuple[logging.StreamHandler[TextIO], TextIO]
        ] = []
        self._last_discrete: dict[str, tuple[object, ...]] = {}
        self._request_key: tuple[str | None, int | None] | None = None
        self._request_started: float | None = None
        self._unicode = self._supports_unicode(self._console.encoding)
        self._spinner = Spinner("dots" if self._unicode else "line", style="cyan")
        self._dynamic = (
            self.mode is not ProgressMode.NEVER
            and self._console.is_terminal
            and self._is_interactive(self._stdout)
            and self._is_interactive(self._stream)
        )

    @staticmethod
    def _console_for(stream: TextIO | None) -> Console:
        if stream is None:
            if color_enabled():
                return _get_rich_console(stderr=True)
            return Console(file=sys.stderr, color_system=None)
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        return Console(
            file=stream,
            force_terminal=is_tty,
            color_system="auto" if color_enabled() else None,
        )

    @staticmethod
    def _supports_unicode(encoding: str) -> bool:
        try:
            "╭—◐".encode(encoding or "utf-8")
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    @staticmethod
    def _is_interactive(stream: IO[str]) -> bool:
        try:
            return bool(stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    @property
    def rendering_mode(self) -> str:
        """Expose the selected behavior for diagnostics and focused tests."""

        if self.mode is ProgressMode.NEVER:
            return "disabled"
        return "dynamic" if self._dynamic else "discrete"

    def __call__(self, event: ProgressEvent) -> None:
        """Render one event through the stream's idempotent live owner."""

        with self._state_lock:
            if self.mode is ProgressMode.NEVER or self._closed:
                return
            if self._started is None:
                self._started = self._clock()
            request_key = (event.current_item, event.current_attempt)
            if request_key != self._request_key:
                self._request_key = request_key
                self._request_started = self._clock()
            self._event = event
            if not self._dynamic:
                self._update_discrete(event)
                return
            owner = self._claim_dynamic_owner()
            if owner is not self:
                owner(event)
                return
            self._update_dynamic(event)

    def close(self) -> None:
        """Stop an owned display once; delegated/nested close calls are harmless."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            if self._owner is not None and self._owner is not self:
                self._owner = None
                return
            if self._live is not None:
                self._restore_logging_handlers()
                self._live.stop()
                self._live = None
            if self._owns_live:
                with self._ownership_lock:
                    key = id(self._stream)
                    if self._active_owners.get(key) is self:
                        self._active_owners.pop(key, None)
                self._owns_live = False

    def _claim_dynamic_owner(self) -> CLIProgressRenderer:
        if self._owner is not None:
            return self._owner
        with self._ownership_lock:
            key = id(self._stream)
            owner = self._active_owners.get(key)
            if owner is None or owner._closed:
                owner = self
                self._active_owners[key] = self
                self._owns_live = True
            self._owner = owner
            return owner

    def _update_dynamic(self, event: ProgressEvent) -> None:
        if self._live is None:
            self._live = Live(
                console=self._console,
                get_renderable=self._render_dynamic,
                auto_refresh=True,
                refresh_per_second=8,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=True,
            )
            self._live.start(refresh=True)
        else:
            self._live.refresh()
        self._redirect_logging_handlers()
        if event.status is not ProgressStatus.RUNNING:
            self.close()
            if event.status is not ProgressStatus.COMPLETED:
                self._console.print(self._terminal_summary(event))

    def _redirect_logging_handlers(self) -> None:
        """Route existing stderr handlers through the active Rich console."""

        proxy = FileProxy(self._console, self._stream)
        loggers = [logging.getLogger()]
        loggers.extend(
            logger
            for logger in logging.Logger.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        )
        seen: set[int] = set()
        for logger in loggers:
            for handler in logger.handlers:
                if (
                    not isinstance(handler, logging.StreamHandler)
                    or isinstance(handler, logging.FileHandler)
                    or id(handler) in seen
                    or handler.stream is not self._stream
                ):
                    continue
                seen.add(id(handler))
                previous = handler.setStream(proxy)
                if previous is not None:
                    self._redirected_handlers.append((handler, previous))

    def _restore_logging_handlers(self) -> None:
        for handler, stream in reversed(self._redirected_handlers):
            handler.setStream(stream)
        self._redirected_handlers.clear()

    def _render_dynamic(self) -> RenderableType:
        event = self._event
        if event is None:  # pragma: no cover - Live starts only after an event.
            return Text("")
        narrow = self._console.width < 58
        header = Table.grid(expand=True, padding=(0, 1))
        header.add_column(width=9, no_wrap=True)
        header.add_column(ratio=1, overflow="fold")
        if not narrow:
            header.add_column(justify="right", no_wrap=True)
        activity_icon: RenderableType = (
            self._spinner
            if event.status is ProgressStatus.RUNNING
            else Text("done" if event.status is ProgressStatus.COMPLETED else "failed")
        )
        activity = Text(self._activity_label(event), style="bold", overflow="fold")
        if narrow:
            header.add_row(activity_icon, activity)
            header.add_row("Overall", f"{math.floor(event.overall_percent)}%")
            header.add_row("Phase", self._phase_detail(event))
        else:
            header.add_row(activity_icon, activity, "")
            header.add_row(
                "Overall",
                ProgressBar(total=100, completed=event.overall_percent),
                f"{math.floor(event.overall_percent):>3}%",
            )
            header.add_row(
                "Phase",
                ProgressBar(total=100, completed=event.phase_percent),
                self._phase_detail(event),
            )

        rows = self._detail_rows(event)
        details: list[RenderableType] = [header]
        if rows:
            if narrow:
                values: list[RenderableType] = []
                for label, value in rows:
                    values.extend(
                        (Text(label, style="bold"), Text(value, overflow="fold"))
                    )
                details.append(Group(*values))
            else:
                table = Table.grid(expand=True, padding=(0, 1))
                table.add_column(width=_LABEL_WIDTH, no_wrap=True)
                table.add_column(ratio=1, overflow="fold")
                for label, value in rows:
                    table.add_row(label, Text(value, overflow="fold"))
                details.append(table)
        details.append(Text(self._footer(event), style="dim"))
        return Panel(
            Group(*details),
            title=self._operation_label(event),
            title_align="left",
            box=box.ASCII if not self._unicode else box.ROUNDED,
            padding=(0, 1),
            width=self._console.width,
        )

    def _detail_rows(self, event: ProgressEvent) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        if event.planned_units:
            rows.extend(
                (
                    ("Processed:", f"{event.processed_units}/{event.planned_units}"),
                    ("Succeeded:", str(event.succeeded_units)),
                    ("Failed:", str(event.failed_units)),
                    ("Fallback:", str(event.fallback_units)),
                    ("Skipped:", str(event.skipped_units)),
                    ("Reused:", str(event.reused_units)),
                )
            )
        if event.current_item is not None:
            rows.append(("Current:", event.current_item))
        if event.last_completed_item is not None:
            rows.append(("Done:", event.last_completed_item))
        if event.last_failed_item is not None:
            rows.append(("Last failure:", event.last_failed_item))
        if event.safe_error_message is not None:
            rows.append(("Reason:", event.safe_error_message))
        if event.lifecycle_state not in {"idle", "active"}:
            rows.append(("State:", event.lifecycle_state.replace("_", " ")))
        if event.current_attempt is not None and event.max_attempts is not None:
            rows.extend(
                (
                    ("Attempt:", f"{event.current_attempt}/{event.max_attempts}"),
                    (
                        "Request elapsed:",
                        self._format_elapsed(self._request_elapsed(event)),
                    ),
                )
            )
        model = self._model_identity(event)
        if model:
            rows.append(("Model:", model))
        if event.analyzer_kind is not None:
            rows.append(("Analyzer:", event.analyzer_kind))
        return rows

    def _update_discrete(self, event: ProgressEvent) -> None:
        signature = (
            event.phase_id,
            math.floor(event.overall_percent),
            event.completed_units,
            event.total_units,
            event.current_item,
            event.last_completed_item,
            event.last_failed_item,
            event.active_item_count,
            event.reused_units,
            event.skipped_units,
            event.failed_units,
            event.processed_units,
            event.succeeded_units,
            event.fallback_units,
            event.current_attempt,
            event.safe_error_code,
            event.status,
        )
        if self._last_discrete.get(event.operation_id) == signature:
            return
        separator = " — " if self._unicode else " - "
        line = (
            f"{self._operation_label(event)}: {math.floor(event.overall_percent)}%"
            f"{separator}{event.phase_label} {self._phase_detail(event)}"
        )
        fields: list[str] = []
        if event.last_completed_item is not None:
            fields.append(f"completed={event.last_completed_item}")
        if event.current_item is not None:
            fields.append(f"current={event.current_item}")
        if event.last_failed_item is not None:
            fields.append(f"failed={event.last_failed_item}")
        if event.active_item_count > 1:
            fields.append(f"active={event.active_item_count}")
        if event.reused_units:
            fields.append(f"reused={event.reused_units}")
        if event.skipped_units:
            fields.append(f"skipped={event.skipped_units}")
        if event.failed_units:
            fields.append(f"failures={event.failed_units}")
        if event.planned_units:
            fields.extend(
                (
                    f"processed={event.processed_units}/{event.planned_units}",
                    f"succeeded={event.succeeded_units}",
                )
            )
        if event.fallback_units:
            fields.append(f"fallback={event.fallback_units}")
        if event.current_attempt is not None and event.max_attempts is not None:
            fields.append(f"attempt={event.current_attempt}/{event.max_attempts}")
        if event.safe_error_code is not None:
            fields.append(f"reason={event.safe_error_code}")
        if event.safe_error_message is not None:
            fields.append(f"error={event.safe_error_message}")
        if fields:
            line += "; " + " ".join(fields)
        self._stream.write(line.rstrip() + "\n")
        self._stream.flush()
        self._last_discrete[event.operation_id] = signature

    def _phase_detail(self, event: ProgressEvent) -> str:
        percent = math.floor(event.phase_percent)
        if event.total_units is None or event.unit_type == "percent":
            return f"{percent}%"
        if event.planned_units:
            return (
                f"{event.processed_units}/{event.planned_units} processed · {percent}%"
            )
        return (
            f"{self._number(event.completed_units)}/"
            f"{self._number(event.total_units)} {event.unit_type} · {percent}%"
        )

    def _footer(self, event: ProgressEvent) -> str:
        return f"Elapsed: {self._format_elapsed(self._elapsed(event))}"

    @staticmethod
    def _model_identity(event: ProgressEvent) -> str:
        provider = event.metadata.get("provider_id")
        model = event.metadata.get("model_id")
        identity = model if isinstance(model, str) else ""
        if isinstance(provider, str) and provider not in {"none", "disabled"}:
            identity = f"{provider}/{identity}" if identity else provider
        return identity

    def _request_elapsed(self, event: ProgressEvent) -> float:
        if event.lifecycle_state != "waiting_for_provider":
            return event.request_elapsed_seconds
        local = (
            0.0
            if self._request_started is None
            else max(0.0, self._clock() - self._request_started)
        )
        return max(event.request_elapsed_seconds, local)

    def _elapsed(self, event: ProgressEvent) -> float:
        local = (
            0.0 if self._started is None else max(0.0, self._clock() - self._started)
        )
        return max(local, event.elapsed_seconds)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, math.floor(seconds))
        hours, remainder = divmod(total, 3_600)
        minutes, secs = divmod(remainder, 60)
        return (
            f"{hours:02}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"
        )

    @staticmethod
    def _number(value: float) -> int | float:
        return int(value) if value.is_integer() else round(value, 1)

    @staticmethod
    def _operation_label(event: ProgressEvent) -> str:
        return _OPERATION_LABELS.get(
            event.operation_type,
            event.operation_type.replace(".", " ").capitalize(),
        )

    @staticmethod
    def _activity_label(event: ProgressEvent) -> str:
        if event.status is ProgressStatus.FAILED:
            return f"Failed · {event.phase_label}"
        if event.status is ProgressStatus.CANCELLED:
            return f"Cancelled · {event.phase_label}"
        if event.status is ProgressStatus.COMPLETED:
            return "Completed"
        if event.activity is ProgressActivity.WAITING:
            return f"{event.phase_label} · waiting for provider"
        return event.phase_label

    def _terminal_summary(self, event: ProgressEvent) -> Text:
        return Text(
            f"{self._operation_label(event)}: {event.status.value} at "
            f"{math.floor(event.overall_percent)}% — {event.phase_label}",
            style="red" if event.status is ProgressStatus.FAILED else "yellow",
        )


__all__ = ["CLIProgressRenderer", "ProgressMode"]
