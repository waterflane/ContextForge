"""Shared stderr-only CLI adapter for application progress events."""

from __future__ import annotations

import math
import sys
from typing import TextIO

from contextforge.progress import ProgressEvent, ProgressStatus

_OPERATION_LABELS = {
    "repository.index.build": "Indexing repository",
    "repository.index.update": "Updating repository index",
    "repository.context.suggest": "Suggesting context",
    "repository.handoff.create": "Creating automatic context",
}


class CLIProgressRenderer:
    """Render meaningful progress changes to stderr without terminal escapes."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._last_phase: dict[str, str] = {}
        self._last_percentage: dict[str, int] = {}

    def __call__(self, event: ProgressEvent) -> None:
        """Write phase changes and whole-percentage increments as discrete lines."""

        percentage = math.floor(event.percentage)
        previous_phase = self._last_phase.get(event.operation_id)
        previous_percentage = self._last_percentage.get(event.operation_id, -1)
        if (
            event.phase_id == previous_phase
            and percentage <= previous_percentage
            and event.status is ProgressStatus.RUNNING
        ):
            return

        label = _OPERATION_LABELS.get(
            event.operation_type,
            event.operation_type.replace(".", " ").capitalize(),
        )
        phase = event.message.rstrip(".")
        detail = self._work_detail(event)
        self._stream.write(f"{label}: {percentage}% — {phase}{detail}\n")
        self._stream.flush()
        self._last_phase[event.operation_id] = event.phase_id
        self._last_percentage[event.operation_id] = percentage

    @staticmethod
    def _work_detail(event: ProgressEvent) -> str:
        if event.total is None or event.total == 100:
            return ""
        completed = (
            int(event.completed) if event.completed.is_integer() else event.completed
        )
        total = int(event.total) if event.total.is_integer() else event.total
        return f" ({completed}/{total} files)"


__all__ = ["CLIProgressRenderer"]
