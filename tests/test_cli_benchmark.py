import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner, Result

import contextforge.cli.benchmark_commands as benchmark_cli
from contextforge.cli.main import app

runner = CliRunner()
TERMINAL_WIDTH = 140


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _repository(root: Path) -> None:
    _write(
        root,
        ".contextforge/config.toml",
        (
            "[models]\nprovider='fake'\nmodel='benchmark-v1'\n"
            "[logging]\nlevel='debug'\nfile_enabled=true\n"
            "file='.contextforge/logs/benchmark.log'\n"
        ),
    )
    _write(root, "repository/main.py", "def main():\n    return 1\n")


def _task(task_id: str, repository_path: str = "repository") -> dict[str, object]:
    return {
        "task_id": task_id,
        "repository_path": repository_path,
        "task": "Find the main implementation.",
        "modes": ["fresh", "hybrid"],
        "include_paths": ["main.py"],
        "required_files_all": ["main.py"],
        "expected_facets": ["main"],
        "max_selected_files": 2,
        "max_files_read": 10,
        "max_model_generations": 10,
        "max_provider_http_calls": 10,
    }


def _manifest(root: Path, *tasks: dict[str, object]) -> Path:
    path = root / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_name": "cli-discovery",
                "tasks": tasks or (_task("main"),),
            }
        ),
        encoding="utf-8",
    )
    return path


def _invoke(root: Path, manifest: Path, *arguments: str) -> Result:
    return runner.invoke(
        app,
        [
            "benchmark",
            "discovery",
            str(root),
            "--tasks",
            str(manifest),
            *arguments,
        ],
        terminal_width=TERMINAL_WIDTH,
    )


def test_help_documents_command_and_regression_exit_policy() -> None:
    result = runner.invoke(
        app,
        ["benchmark", "discovery", "--help"],
        terminal_width=TERMINAL_WIDTH,
    )
    output = click.unstyle(result.output)

    assert result.exit_code == 0
    for value in ("PATH", "--tasks", "--modes", "--repeat", "--format", "--output"):
        assert value in output
    assert "Exit code 3" in output
    parent = click.unstyle(
        runner.invoke(
            app, ["benchmark", "--help"], terminal_width=TERMINAL_WIDTH
        ).output
    )
    assert "Exit code 3" in parent


def test_default_text_and_json_repeat_override_are_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    task = _task("main")
    task["repeat_count"] = 2
    task["mode_overrides"] = {"fresh": {"repeat_count": 4}}
    manifest = _manifest(tmp_path, task)
    monkeypatch.chdir(tmp_path)

    text = _invoke(tmp_path, manifest, "--modes", "fresh")
    machine = _invoke(
        tmp_path,
        manifest,
        "--modes",
        "fresh",
        "--repeat",
        "3",
        "--format",
        "json",
    )

    assert text.exit_code == 0, text.output
    plain = click.unstyle(text.stdout)
    for heading in (
        "Task and mode summaries:",
        "Overall quality:",
        "Repeatability:",
        "Performance:",
        "Warnings:",
        "Failed expectations:",
    ):
        assert heading in plain
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert payload["suite_name"] == "cli-discovery"
    assert len(payload["runs"]) == 3
    assert {run["mode"] for run in payload["runs"]} == {"fresh"}
    assert payload["metrics"][0]["exact_selected_file_match_rate"] == 1.0
    assert "ContextForge discovery benchmark" not in machine.stdout
    assert "Suggesting context" not in machine.stdout
    assert "Suggesting context" in machine.stderr
    assert not (tmp_path / "repository/.contextforge").exists()
    assert not (tmp_path / ".contextforge/logs/benchmark.log").exists()


def test_markdown_output_file_receives_the_only_result(tmp_path: Path) -> None:
    _repository(tmp_path)
    manifest = _manifest(tmp_path)
    output = tmp_path / "report.md"

    result = _invoke(
        tmp_path,
        manifest,
        "--modes",
        "fresh",
        "--format",
        "markdown",
        "--output",
        str(output),
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    report = output.read_text(encoding="utf-8")
    assert report.startswith("# ContextForge discovery benchmark\n")
    assert "## Overall quality" in report
    assert "## Failed expectations" in report


def test_invalid_modes_and_manifest_fail_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    manifest = _manifest(tmp_path)
    called = False

    async def unexpected(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("benchmark execution must not start")

    monkeypatch.setattr(benchmark_cli, "run_discovery_benchmark", unexpected)
    invalid_modes = _invoke(tmp_path, manifest, "--modes", "fresh,unknown")
    manifest.write_text('{"schema_version":1,"unknown":true}', encoding="utf-8")
    invalid_manifest = _invoke(tmp_path, manifest)

    assert invalid_modes.exit_code == 2
    assert invalid_modes.stdout == ""
    assert "--modes accepts only" in invalid_modes.stderr
    assert invalid_manifest.exit_code == 2
    assert invalid_manifest.stdout == ""
    assert called is False


def test_regressions_and_partial_failures_emit_json_then_exit_three(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    failing_expectation = _task("expectation")
    failing_expectation["required_files_all"] = ["missing.py"]
    expectation_manifest = _manifest(tmp_path, failing_expectation)

    regression = _invoke(
        tmp_path,
        expectation_manifest,
        "--modes",
        "fresh",
        "--format",
        "json",
    )

    assert regression.exit_code == benchmark_cli.BENCHMARK_REGRESSION_EXIT_CODE
    regression_payload = json.loads(regression.stdout)
    assert regression_payload["passed"] is False
    assert regression_payload["runs"][0]["status"] == "complete"
    assert regression_payload["runs"][0]["expectations"][
        "missing_required_files"
    ] == ["missing.py"]

    partial_manifest = _manifest(
        tmp_path,
        _task("a-complete"),
        _task("b-failed", repository_path="missing"),
    )
    partial = _invoke(
        tmp_path,
        partial_manifest,
        "--modes",
        "fresh",
        "--format",
        "json",
    )

    assert partial.exit_code == benchmark_cli.BENCHMARK_REGRESSION_EXIT_CODE
    partial_payload = json.loads(partial.stdout)
    assert [run["status"] for run in partial_payload["runs"]] == [
        "complete",
        "failed",
    ]
