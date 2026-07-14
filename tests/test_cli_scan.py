import json
import os
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner, Result

import contextforge.cli.main as cli_module
import contextforge.repositories.scanner as scanner_module
from contextforge.cli.main import app
from contextforge.repositories.files import FileInspection
from contextforge.repositories.files import inspect_file as file_inspector

runner = CliRunner()
TERMINAL_WIDTH = 120


def _invoke_scan(root: Path, *arguments: str) -> Result:
    return runner.invoke(
        app,
        ["scan", str(root), *arguments],
        terminal_width=TERMINAL_WIDTH,
    )


def _plain_output(result: Result) -> str:
    return click.unstyle(result.output)


def test_scan_help_documents_the_public_contract() -> None:
    result = runner.invoke(
        app,
        ["scan", "--help"],
        terminal_width=TERMINAL_WIDTH,
    )
    output = _plain_output(result)

    assert result.exit_code == 0
    for value in (
        "PATH",
        "--format",
        "--output",
        "--max-file-size",
        "--show-excluded",
        "--fail-on-error",
    ):
        assert value in output


def test_empty_repository_has_complete_human_readable_summary(
    tmp_path: Path,
) -> None:
    result = _invoke_scan(tmp_path)
    output = _plain_output(result)

    assert result.exit_code == 0
    assert f"Project root: {tmp_path.resolve()}" in output
    expected_counts = {
        "Discovered files": 0,
        "Included files": 0,
        "Ignored files": 0,
        "Protected exclusions": 0,
        "Binary files": 0,
        "Oversized files": 0,
        "Failed/unreadable files": 0,
        "Symlinks": 0,
        "Total included size": "0 bytes",
    }
    for label, value in expected_counts.items():
        assert f"{label}: {value}" in output
    assert "Languages:\n  (none)" in output
    assert "schema_version" not in output
    assert not output.lstrip().startswith("{")


def test_default_path_scans_the_current_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "current.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["scan"], terminal_width=TERMINAL_WIDTH)
    output = _plain_output(result)

    assert result.exit_code == 0
    assert f"Project root: {tmp_path.resolve()}" in output
    assert "Included files: 1" in output
    assert "  Python: 1" in output
    size_bytes = (tmp_path / "current.py").stat().st_size
    assert f"current.py | {size_bytes} | Python |" in output


def test_json_output_is_parseable_explicit_and_deterministic_with_unicode(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "проект"
    repository.mkdir()
    unicode_name = "данные/пример.py"
    unicode_file = repository / unicode_name
    unicode_file.parent.mkdir()
    content = "print('привет')\n"
    unicode_file.write_bytes(content.encode("utf-8"))
    (repository / "alpha.md").write_bytes(b"# Alpha\n")

    first = _invoke_scan(repository, "--format", "json")
    second = _invoke_scan(repository, "--format", "JSON")

    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["schema_version"] == 1
    assert payload["options"] == {
        "max_file_size_bytes": 1_000_000,
        "respect_gitignore": True,
        "respect_contextforgeignore": True,
    }
    snapshot = payload["snapshot"]
    assert snapshot["root"] == str(repository.resolve())
    assert [item["path"] for item in snapshot["files"]] == [
        "alpha.md",
        unicode_name,
    ]
    assert snapshot["files"][1]["size_bytes"] == len(content.encode("utf-8"))
    assert snapshot["summary"]["file_count"] == 2
    assert snapshot["summary"]["languages"] == {"Markdown": 1, "Python": 1}
    assert snapshot["ignored_files"] == []
    assert snapshot["skipped_files"] == []
    assert "\\" not in snapshot["files"][1]["path"]
    assert "\x1b[" not in first.stdout


def test_exclusions_summary_and_show_excluded_reasons(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("pass", encoding="utf-8")
    (tmp_path / "debug.log").write_text("ignored", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"a\x00b")
    (tmp_path / "oversized.txt").write_bytes(b"x" * 17)
    metadata = tmp_path / ".git"
    metadata.mkdir()
    (metadata / "config").write_text("protected", encoding="utf-8")

    result = _invoke_scan(
        tmp_path,
        "--max-file-size",
        "16",
        "--show-excluded",
    )
    output = _plain_output(result)

    assert result.exit_code == 0
    for expected in (
        "Discovered files: 5",
        "Included files: 2",
        "Ignored files: 1",
        "Protected exclusions: 1",
        "Binary files: 1",
        "Oversized files: 1",
        "Failed/unreadable files: 0",
        "debug.log",
        "reason: ignored (gitignore); pattern: *.log",
        ".git",
        "reason: protected directory; pattern: .git/",
        "binary.dat",
        "reason: binary",
        "oversized.txt",
        "reason: too_large; file size 17 exceeds limit 16",
    ):
        assert expected in output


def test_json_exclusion_details_follow_show_excluded(tmp_path: Path) -> None:
    virtual_environment = tmp_path / ".venv"
    virtual_environment.mkdir()
    (virtual_environment / "ignored.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"\x00")

    concise = _invoke_scan(tmp_path, "--format", "json")
    detailed = _invoke_scan(tmp_path, "--format", "json", "--show-excluded")

    assert concise.exit_code == detailed.exit_code == 0
    concise_snapshot = json.loads(concise.stdout)["snapshot"]
    detailed_snapshot = json.loads(detailed.stdout)["snapshot"]
    assert concise_snapshot["ignored_files"] == []
    assert concise_snapshot["skipped_files"] == []
    assert concise_snapshot["summary"]["ignored_count"] == 1
    assert concise_snapshot["summary"]["binary_count"] == 1
    assert detailed_snapshot["ignored_files"] == [
        {
            "path": ".venv",
            "source": "default",
            "pattern": ".venv/",
            "is_directory": True,
        }
    ]
    assert [item["path"] for item in detailed_snapshot["skipped_files"]] == [
        "binary.dat"
    ]
    assert "ignored.txt" not in detailed.stdout


def test_table_without_show_excluded_remains_concise(tmp_path: Path) -> None:
    virtual_environment = tmp_path / ".venv"
    virtual_environment.mkdir()
    for index in range(100):
        (virtual_environment / f"ignored-{index}.txt").write_text(
            "ignored", encoding="utf-8"
        )

    result = _invoke_scan(tmp_path)
    output = _plain_output(result)

    assert result.exit_code == 0
    assert "Ignored files: 1" in output
    assert "Excluded entries:" not in output
    assert ".venv" not in output
    assert "ignored-" not in output
    assert len(output.splitlines()) <= 16


def test_uv_cache_is_pruned_from_cli_outputs_while_uv_lock_is_included(
    tmp_path: Path,
) -> None:
    uv_cache = tmp_path / ".uv-cache"
    uv_cache.mkdir()
    for index in range(25):
        (uv_cache / f"artifact-{index}.txt").write_text("generated", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1", encoding="utf-8")

    table = _invoke_scan(tmp_path)
    concise_json = _invoke_scan(tmp_path, "--format", "json")
    detailed_json = _invoke_scan(tmp_path, "--format", "json", "--show-excluded")
    table_output = _plain_output(table)

    assert table.exit_code == concise_json.exit_code == detailed_json.exit_code == 0
    assert "Discovered files: 1" in table_output
    assert "Included files: 1" in table_output
    assert "Ignored files: 1" in table_output
    assert ".uv-cache" not in table_output

    concise_snapshot = json.loads(concise_json.stdout)["snapshot"]
    detailed_snapshot = json.loads(detailed_json.stdout)["snapshot"]
    assert [item["path"] for item in concise_snapshot["files"]] == ["uv.lock"]
    assert concise_snapshot["ignored_files"] == []
    assert detailed_snapshot["ignored_files"] == [
        {
            "path": ".uv-cache",
            "source": "default",
            "pattern": ".uv-cache/",
            "is_directory": True,
        }
    ]
    assert "artifact-" not in detailed_json.stdout
    assert detailed_snapshot["summary"] == concise_snapshot["summary"]


def test_fail_on_error_succeeds_when_there_are_no_failures(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("readable", encoding="utf-8")

    result = _invoke_scan(tmp_path, "--fail-on-error")
    output = _plain_output(result)

    assert result.exit_code == 0
    assert "Failed/unreadable files: 0" in output


def test_fail_on_error_returns_three_for_portably_simulated_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("unreadable", encoding="utf-8")

    def fail_file(path: Path, *, max_size_bytes: int) -> FileInspection:
        if path == unreadable:
            raise PermissionError("denied for CLI test")
        return file_inspector(path, max_size_bytes=max_size_bytes)

    monkeypatch.setattr(scanner_module, "inspect_file", fail_file)

    result = _invoke_scan(tmp_path, "--fail-on-error", "--show-excluded")
    output = _plain_output(result)

    assert result.exit_code == 3
    assert "Failed/unreadable files: 1" in output
    assert "reason: unreadable; PermissionError" in output
    assert "Traceback" not in output


def test_json_without_exclusions_retains_unreadable_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("unreadable", encoding="utf-8")

    def fail_file(path: Path, *, max_size_bytes: int) -> FileInspection:
        if path == unreadable:
            raise PermissionError("denied for JSON diagnostic test")
        return file_inspector(path, max_size_bytes=max_size_bytes)

    monkeypatch.setattr(scanner_module, "inspect_file", fail_file)

    result = _invoke_scan(tmp_path, "--format", "json")

    assert result.exit_code == 0
    snapshot = json.loads(result.stdout)["snapshot"]
    assert snapshot["ignored_files"] == []
    assert [item["path"] for item in snapshot["skipped_files"]] == ["unreadable.txt"]
    assert snapshot["skipped_files"][0]["reason"] == "unreadable"


@pytest.mark.parametrize("maximum", ["0", "-1"])
def test_non_positive_maximum_file_size_is_rejected(
    tmp_path: Path, maximum: str
) -> None:
    result = _invoke_scan(tmp_path, "--max-file-size", maximum)
    output = _plain_output(result)

    assert result.exit_code == 2
    assert "--max-file-size" in output
    assert "Traceback" not in output


def test_invalid_format_is_rejected_by_typer(tmp_path: Path) -> None:
    result = _invoke_scan(tmp_path, "--format", "yaml")
    output = _plain_output(result)

    assert result.exit_code == 2
    assert "yaml" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_invalid_roots_are_clear_cli_errors(tmp_path: Path, root_kind: str) -> None:
    root = tmp_path / root_kind
    if root_kind == "file":
        root.write_text("not a repository", encoding="utf-8")

    result = _invoke_scan(root)
    output = _plain_output(result)

    assert result.exit_code == 2
    expected = "not a directory" if root_kind == "file" else "does not exist"
    assert expected in output
    assert "Traceback" not in output


def test_invalid_ignore_file_is_a_normal_scanner_error(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_bytes(b"\xff")

    result = _invoke_scan(tmp_path)
    output = _plain_output(result)

    assert result.exit_code == 1
    assert "unable to read ignore file" in output
    assert "Traceback" not in output


def test_output_file_is_created_after_scan_and_reported(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    output = tmp_path / "scan.json"

    result = _invoke_scan(
        tmp_path,
        "--format",
        "json",
        "--output",
        str(output),
    )
    plain_output = _plain_output(result)

    assert result.exit_code == 0
    assert plain_output.strip() == f"Output written to {output.resolve()}"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [item["path"] for item in payload["snapshot"]["files"]] == ["source.py"]


def test_existing_output_file_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "scan.json"
    output.write_text("original", encoding="utf-8")

    result = _invoke_scan(tmp_path, "--output", str(output))
    plain_output = _plain_output(result)

    assert result.exit_code == 1
    assert "already exists" in plain_output
    assert output.read_text(encoding="utf-8") == "original"
    assert "Traceback" not in plain_output


def test_output_parent_directories_are_not_created(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing" / "nested"
    output = missing_parent / "scan.json"

    result = _invoke_scan(tmp_path, "--output", str(output))
    plain_output = _plain_output(result)

    assert result.exit_code == 1
    assert "parent directory does not exist" in plain_output
    assert not missing_parent.exists()
    assert not output.exists()


def test_output_parent_must_be_a_directory(tmp_path: Path) -> None:
    parent_file = tmp_path / "parent.txt"
    parent_file.write_text("file", encoding="utf-8")

    result = _invoke_scan(tmp_path, "--output", str(parent_file / "scan.json"))
    output = _plain_output(result)

    assert result.exit_code == 1
    assert "parent is not a directory" in output


def test_output_write_failure_is_clear_and_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "scan.json"

    def fail_publish(source: Path, destination: Path) -> None:
        raise PermissionError(f"cannot publish {source.name} to {destination.name}")

    monkeypatch.setattr(os, "link", fail_publish)

    result = _invoke_scan(tmp_path, "--output", str(output), "--format", "json")
    plain_output = _plain_output(result)

    assert result.exit_code == 1
    assert "unable to write output file" in plain_output
    assert "Traceback" not in plain_output
    assert not output.exists()
    assert list(tmp_path.glob(".scan.json.*.tmp")) == []


def test_unexpected_internal_error_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_scan(path: str | Path, options: object) -> None:
        raise RuntimeError(f"unexpected scanner failure: {path}")

    monkeypatch.setattr(cli_module, "scan_repository", fail_scan)

    result = _invoke_scan(tmp_path)
    output = _plain_output(result)

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert "repository scan" not in output


def test_root_help_version_and_doctor_remain_available() -> None:
    help_result = runner.invoke(app, ["--help"], terminal_width=TERMINAL_WIDTH)
    version_result = runner.invoke(app, ["version"])
    doctor_result = runner.invoke(app, ["doctor"])
    help_output = _plain_output(help_result)
    version_output = _plain_output(version_result)
    doctor_output = _plain_output(doctor_result)

    assert help_result.exit_code == 0
    assert "scan" in help_output
    assert version_result.exit_code == 0
    assert "ContextForge 0.1.0" in version_output
    assert doctor_result.exit_code == 0
    assert "ContextForge is installed." in doctor_output
