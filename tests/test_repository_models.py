from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.repositories import (
    IgnoredFile,
    ProjectFile,
    ProjectSnapshot,
    ScanOptions,
    ScanSummary,
)

SHA256 = "a" * 64


def test_scan_options_defaults_and_validation() -> None:
    options = ScanOptions()

    assert options.max_file_size_bytes == 1_000_000
    assert options.respect_gitignore is True
    assert options.respect_contextforgeignore is True

    with pytest.raises(ValidationError):
        ScanOptions(max_file_size_bytes=0)


def test_project_file_normalizes_path_and_validates_metadata() -> None:
    project_file = ProjectFile(
        path=r"src\contextforge\app.py",
        size_bytes=12,
        language="Python",
        sha256=SHA256,
        is_text=True,
    )

    assert project_file.path == "src/contextforge/app.py"

    with pytest.raises(ValidationError):
        ProjectFile(
            path="../app.py",
            size_bytes=-1,
            sha256="not-a-hash",
            is_text=True,
        )


def test_ignored_file_normalizes_path() -> None:
    ignored = IgnoredFile(
        path=r"build\artifact.bin", source="default", pattern="build/"
    )

    assert ignored.path == "build/artifact.bin"


def test_scan_summary_validates_non_negative_counts() -> None:
    with pytest.raises(ValidationError):
        ScanSummary(
            file_count=1,
            ignored_count=-1,
            total_size_bytes=10,
            languages={"Python": 1},
        )


def test_project_snapshot_uses_immutable_sequences_and_models(tmp_path: Path) -> None:
    project_file = ProjectFile(
        path="app.py",
        size_bytes=3,
        language="Python",
        sha256=SHA256,
        is_text=True,
    )
    ignored = IgnoredFile(path="build", source="default", pattern="build/")
    summary = ScanSummary(
        file_count=1,
        ignored_count=1,
        total_size_bytes=3,
        languages={"Python": 1},
    )
    snapshot = ProjectSnapshot(
        root=tmp_path,
        files=(project_file,),
        ignored_files=(ignored,),
        summary=summary,
    )

    assert snapshot.files == (project_file,)
    assert snapshot.ignored_files == (ignored,)
    with pytest.raises(ValidationError):
        snapshot.root = tmp_path / "other"
    with pytest.raises(ValidationError):
        project_file.size_bytes = 4
