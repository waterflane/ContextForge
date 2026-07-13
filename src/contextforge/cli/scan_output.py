"""Deterministic rendering and safe file output for repository scans."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from contextforge.repositories import ProjectSnapshot, ScanOptions


class ScanReport(BaseModel):
    """Stable JSON envelope for a repository scan."""

    schema_version: Literal[1] = 1
    options: ScanOptions
    snapshot: ProjectSnapshot

    model_config = ConfigDict(frozen=True)


class OutputWriteError(OSError):
    """Raised when a rendered scan report cannot be safely published."""


def render_scan_json(report: ScanReport, *, show_excluded: bool = False) -> str:
    """Serialize a deterministic JSON report with optional exclusion details.

    Summary-only output retains unreadable entries for diagnostics but removes
    ignored paths and non-failure skipped entries. The summary remains unchanged.
    """

    visible_report = report if show_excluded else _without_exclusion_details(report)
    return f"{visible_report.model_dump_json(indent=2)}\n"


def render_scan_table(snapshot: ProjectSnapshot, *, show_excluded: bool = False) -> str:
    """Render a narrow-terminal-friendly repository scan summary."""

    summary = snapshot.summary
    lines = [
        "ContextForge repository scan",
        f"Project root: {snapshot.root}",
        f"Discovered files: {summary.discovered_count}",
        f"Included files: {summary.file_count}",
        f"Ignored files: {summary.ignored_count}",
        f"Protected exclusions: {summary.protected_count}",
        f"Binary files: {summary.binary_count}",
        f"Oversized files: {summary.oversized_count}",
        f"Failed/unreadable files: {summary.failed_count}",
        f"Symlinks: {summary.symlink_count}",
        f"Unsupported entries: {summary.unsupported_count}",
        f"Total included size: {summary.total_size_bytes} bytes",
        "File inventory:",
    ]
    if snapshot.files:
        lines.append("  Path | Bytes | Language | SHA-256")
        lines.extend(
            (
                f"  {item.path} | {item.size_bytes} | "
                f"{item.language or '-'} | {item.sha256}"
            )
            for item in snapshot.files
        )
    else:
        lines.append("  (none)")

    lines.append("Languages:")
    if summary.languages:
        lines.extend(
            f"  {language}: {count}"
            for language, count in sorted(summary.languages.items())
        )
    else:
        lines.append("  (none)")

    if show_excluded:
        lines.append("Excluded entries:")
        excluded = [
            (
                item.path,
                _ignored_reason(item.source, item.pattern, item.is_directory),
            )
            for item in snapshot.ignored_files
        ]
        excluded.extend(
            (item.path, _skipped_reason(item.reason, item.detail))
            for item in snapshot.skipped_files
        )
        if excluded:
            for path, reason in sorted(excluded):
                lines.extend((f"  {path}", f"    reason: {reason}"))
        else:
            lines.append("  (none)")

    return "\n".join(lines) + "\n"


def write_output_atomic(destination: Path, content: str) -> Path:
    """Publish ``content`` atomically without creating parents or overwriting."""

    requested = destination.expanduser()
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise OutputWriteError(
            f"output parent directory does not exist: {requested.parent}"
        ) from exc
    if not parent.is_dir():
        raise OutputWriteError(f"output parent is not a directory: {parent}")

    resolved_destination = parent / requested.name
    if os.path.lexists(resolved_destination):
        raise OutputWriteError(f"output file already exists: {resolved_destination}")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{resolved_destination.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        # A hard-link publish is atomic and fails if the destination appeared
        # after the preflight check, preserving the no-overwrite contract.
        os.link(temporary_path, resolved_destination)
    except OSError as exc:
        raise OutputWriteError(
            f"unable to write output file {resolved_destination}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    return resolved_destination


def _without_exclusion_details(report: ScanReport) -> ScanReport:
    failures = tuple(
        item for item in report.snapshot.skipped_files if item.reason == "unreadable"
    )
    visible_snapshot = report.snapshot.model_copy(
        update={"ignored_files": (), "skipped_files": failures}
    )
    return report.model_copy(update={"snapshot": visible_snapshot})


def _ignored_reason(source: str, pattern: str | None, is_directory: bool) -> str:
    reason = "protected" if source == "protected" else f"ignored ({source})"
    if is_directory:
        reason = f"{reason} directory"
    if pattern is not None:
        return f"{reason}; pattern: {pattern}"
    return reason


def _skipped_reason(reason: str, detail: str | None) -> str:
    if detail is not None:
        return f"{reason}; {detail}"
    return reason
