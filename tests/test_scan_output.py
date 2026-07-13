import json
import os
from pathlib import Path

import pytest

from contextforge.cli.scan_output import (
    OutputWriteError,
    ScanReport,
    render_scan_json,
    render_scan_table,
    write_output_atomic,
)
from contextforge.repositories import (
    IgnoredFile,
    ProjectSnapshot,
    ScanOptions,
    ScanSummary,
    SkippedFile,
)


def _excluded_snapshot(root: Path) -> ProjectSnapshot:
    return ProjectSnapshot(
        root=root,
        ignored_files=(IgnoredFile(path="ignored", source="default"),),
        skipped_files=(
            SkippedFile(path="binary", reason="binary"),
            SkippedFile(path="failed", reason="unreadable", detail="denied"),
        ),
        summary=ScanSummary(
            file_count=0,
            ignored_count=1,
            total_size_bytes=0,
            languages={},
            discovered_count=3,
            binary_count=1,
            failed_count=1,
            skipped_count=3,
        ),
    )


def test_scan_report_json_filters_exclusions_but_retains_failures(
    tmp_path: Path,
) -> None:
    snapshot = _excluded_snapshot(tmp_path)
    report = ScanReport(options=ScanOptions(max_file_size_bytes=42), snapshot=snapshot)

    concise = render_scan_json(report)
    detailed = render_scan_json(report, show_excluded=True)

    assert concise.endswith("\n")
    concise_snapshot = json.loads(concise)["snapshot"]
    detailed_snapshot = json.loads(detailed)["snapshot"]
    assert concise_snapshot["ignored_files"] == []
    assert [item["path"] for item in concise_snapshot["skipped_files"]] == ["failed"]
    assert [item["path"] for item in detailed_snapshot["ignored_files"]] == ["ignored"]
    assert [item["path"] for item in detailed_snapshot["skipped_files"]] == [
        "binary",
        "failed",
    ]
    assert concise_snapshot["summary"] == detailed_snapshot["summary"]
    assert '"schema_version": 1' in concise
    assert '"max_file_size_bytes": 42' in concise
    assert "\x1b[" not in concise


def test_table_excluded_section_handles_absent_patterns_and_details(
    tmp_path: Path,
) -> None:
    rendered = render_scan_table(_excluded_snapshot(tmp_path), show_excluded=True)

    assert "reason: ignored (default)" in rendered
    assert "reason: binary" in rendered
    assert "reason: unreadable; denied" in rendered


def test_table_excluded_section_reports_when_there_are_no_exclusions(
    tmp_path: Path,
) -> None:
    snapshot = ProjectSnapshot(
        root=tmp_path,
        summary=ScanSummary(
            file_count=0,
            ignored_count=0,
            total_size_bytes=0,
        ),
    )

    rendered = render_scan_table(snapshot, show_excluded=True)

    assert "Excluded entries:\n  (none)" in rendered


def test_atomic_writer_returns_resolved_destination_and_exact_content(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output.txt"

    written = write_output_atomic(output, "complete\n")

    assert written == output.resolve()
    assert output.read_bytes() == b"complete\n"
    assert list(tmp_path.glob(".output.txt.*.tmp")) == []


def test_atomic_writer_refuses_a_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output.txt"
    real_link = os.link

    def racing_link(source: Path, destination: Path) -> None:
        destination.write_text("racer", encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(OutputWriteError, match="unable to write output file"):
        write_output_atomic(output, "ours\n")

    assert output.read_text(encoding="utf-8") == "racer"
    assert list(tmp_path.glob(".output.txt.*.tmp")) == []
