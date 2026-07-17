"""Shared stderr-only CLI adapter for structured application progress events."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from enum import StrEnum
from typing import TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from typer.rich_utils import _get_rich_console

from contextforge.progress import ProgressActivity, ProgressEvent, ProgressStatus

_OPERATION_LABELS = {
    "repository.index.build": "Indexing repository",
    "repository.index.update": "Updating repository index",
    "repository.context.suggest": "Suggesting context",
    "repository.handoff.create": "Creating automatic context",
}


class ProgressMode(StrEnum):
    """Terminal progress rendering policy."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class CLIProgressRenderer:
    """Adapt shared progress events to one Rich live panel or discrete stderr."""

    def __init__(
        self,
        mode: ProgressMode | str | TextIO = ProgressMode.AUTO,
        *,
        stream: TextIO | None = None,
        console: Console | None = None,
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
        self._clock = clock
        self._started: float | None = None
        self._event: ProgressEvent | None = None
        self._live: Live | None = None
        self._last_discrete: dict[str, tuple[object, ...]] = {}
        self._request_key: tuple[str | None, int | None] | None = None
        self._request_started: float | None = None
        self._unicode = self._supports_unicode(self._console.encoding)
        self._spinner = Spinner("dots" if self._unicode else "line", style="cyan")
        self._dynamic = (
            self.mode is not ProgressMode.NEVER and self._console.is_terminal
        )

    @staticmethod
    def _console_for(stream: TextIO | None) -> Console:
        if stream is None:
            # Typer owns ContextForge's console policy. stderr=True is important:
            # captured/non-interactive stdout must not disable an interactive panel.
            return _get_rich_console(stderr=True)
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        return Console(file=stream, force_terminal=is_tty, color_system="auto")

    @staticmethod
    def _supports_unicode(encoding: str) -> bool:
        try:
            "╭—◐".encode(encoding or "utf-8")
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    @property
    def rendering_mode(self) -> str:
        """Expose the selected behavior for diagnostics and focused tests."""

        if self.mode is ProgressMode.NEVER:
            return "disabled"
        return "dynamic" if self._dynamic else "discrete"

    def __call__(self, event: ProgressEvent) -> None:
        """Render one structured event without changing application semantics."""

        if self.mode is ProgressMode.NEVER:
            return
        if self._started is None:
            self._started = self._clock()
        request_key = (event.current_item, event.current_attempt)
        if request_key != self._request_key:
            self._request_key = request_key
            self._request_started = self._clock()
        self._event = event
        if self._dynamic:
            self._update_dynamic(event)
        else:
            self._update_discrete(event)

    def close(self) -> None:
        """Restore the terminal even if a caller exits outside a terminal event."""

        if self._live is not None:
            self._live.stop()
            self._live = None

    def _update_dynamic(self, event: ProgressEvent) -> None:
        if self._live is None:
            self._live = Live(
                console=self._console,
                get_renderable=self._render_dynamic,
                auto_refresh=True,
                refresh_per_second=8,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._live.start(refresh=True)
        else:
            self._live.refresh()
        if event.status is not ProgressStatus.RUNNING:
            self.close()
            if event.status is not ProgressStatus.COMPLETED:
                self._console.print(self._terminal_summary(event))

    def _render_dynamic(self) -> RenderableType:
        event = self._event
        if event is None:  # pragma: no cover - Live starts only after an event.
            return Text("")

        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(width=9, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right", no_wrap=True)
        activity = self._activity_label(event)
        activity_icon: RenderableType = (
            self._spinner
            if event.status is ProgressStatus.RUNNING
            else Text("done" if event.status is ProgressStatus.COMPLETED else "failed")
        )
        grid.add_row(activity_icon, Text(activity, style="bold"), Text(""))
        grid.add_row(
            Text("Overall"),
            ProgressBar(total=100, completed=event.overall_percent),
            Text(f"{math.floor(event.overall_percent):>3}%"),
        )
        grid.add_row(
            Text("Phase"),
            ProgressBar(total=100, completed=event.phase_percent),
            Text(self._phase_detail(event)),
        )
        details: list[RenderableType] = [grid]
        item_grid = Table.grid(expand=True)
        item_grid.add_column(width=9, no_wrap=True)
        item_grid.add_column(ratio=1)
        if event.planned_units:
            counters = (
                f"{event.processed_units}/{event.planned_units}"
                f" · succeeded {event.succeeded_units}"
                f" · failed {event.failed_units}"
            )
            if event.fallback_units:
                counters += f" · fallback {event.fallback_units}"
            if event.skipped_units:
                counters += f" · skipped {event.skipped_units}"
            if event.reused_units:
                counters += f" · reused {event.reused_units}"
            item_grid.add_row("Processed:", Text(counters))
        if event.current_item is not None:
            item_grid.add_row("Current:", Text(event.current_item))
        if event.last_completed_item is not None:
            item_grid.add_row("Done:", Text(event.last_completed_item))
        if event.last_failed_item is not None:
            item_grid.add_row("Last fail:", Text(event.last_failed_item, style="red"))
        if event.safe_error_message is not None:
            item_grid.add_row("Reason:", Text(event.safe_error_message, style="red"))
        if event.current_attempt is not None and event.max_attempts is not None:
            item_grid.add_row(
                "Attempt:", Text(f"{event.current_attempt}/{event.max_attempts}")
            )
        if event.active_item_count > 1:
            item_grid.add_row("Active:", Text(str(event.active_item_count)))
        if item_grid.row_count:
            details.append(item_grid)

        footer = self._footer(event)
        if footer:
            details.append(Text(footer, style="dim"))
        panel_box = box.ASCII if not self._unicode else box.ROUNDED
        return Panel(
            Group(*details),
            title=self._operation_label(event),
            title_align="left",
            box=panel_box,
            padding=(0, 1),
        )

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
            fields.append(f"processed={event.processed_units}/{event.planned_units}")
            fields.append(f"succeeded={event.succeeded_units}")
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
        completed = self._number(event.completed_units)
        total = self._number(event.total_units)
        return f"{completed}/{total} {event.unit_type} · {percent}%"

    def _footer(self, event: ProgressEvent) -> str:
        metadata = event.metadata
        provider = metadata.get("provider_id")
        model = metadata.get("model_id")
        identity = ""
        if isinstance(model, str):
            identity = model
        if isinstance(provider, str) and provider not in {"none", "disabled"}:
            identity = f"{provider}/{identity}" if identity else provider
        elapsed = self._elapsed(event)
        fields = []
        if identity:
            fields.append(f"Model: {identity}")
        if event.current_attempt is not None:
            fields.append(
                f"request {self._format_elapsed(self._request_elapsed(event))}"
            )
        fields.append(f"elapsed {self._format_elapsed(elapsed)}")
        return " · ".join(fields)

    def _request_elapsed(self, event: ProgressEvent) -> float:
        if event.lifecycle_state != "waiting_for_provider":
            return event.request_elapsed_seconds
        local_since_event = (
            0.0
            if self._request_started is None
            else max(0.0, self._clock() - self._request_started)
        )
        return max(event.request_elapsed_seconds, local_since_event)

    def _elapsed(self, event: ProgressEvent) -> float:
        local = 0.0
        if self._started is not None:
            local = max(0.0, self._clock() - self._started)
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
        label = self._operation_label(event)
        return Text(
            f"{label}: {event.status.value} at "
            f"{math.floor(event.overall_percent)}% — {event.phase_label}",
            style="red" if event.status is ProgressStatus.FAILED else "yellow",
        )


__all__ = ["CLIProgressRenderer", "ProgressMode"]
