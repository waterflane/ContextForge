import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner, Result

import contextforge.cli.benchmark_commands as benchmark_cli
from contextforge.benchmarks import BenchmarkManifest, BenchmarkMode
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


def _filtering_manifest() -> BenchmarkManifest:
    mixed = _task("a-mixed")
    mixed["modes"] = ["fresh", "indexed", "hybrid"]
    mixed["index_precondition"] = {"kind": "clean"}
    fresh = _task("b-fresh")
    fresh["modes"] = ["fresh"]
    return BenchmarkManifest.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "suite_name": "mode-filtering",
                "tasks": [mixed, fresh],
            }
        )
    )


@pytest.mark.parametrize(
    ("requested", "expected_modes", "precondition_preserved", "task_ids"),
    [
        (
            (BenchmarkMode.FRESH,),
            (BenchmarkMode.FRESH,),
            False,
            ("a-mixed", "b-fresh"),
        ),
        (
            (BenchmarkMode.INDEXED,),
            (BenchmarkMode.INDEXED,),
            True,
            ("a-mixed",),
        ),
        (
            (BenchmarkMode.HYBRID,),
            (BenchmarkMode.HYBRID,),
            True,
            ("a-mixed",),
        ),
        (
            (BenchmarkMode.FRESH, BenchmarkMode.INDEXED),
            (BenchmarkMode.FRESH, BenchmarkMode.INDEXED),
            True,
            ("a-mixed", "b-fresh"),
        ),
    ],
)
def test_mode_filtering_normalizes_only_derived_index_preconditions(
    requested: tuple[BenchmarkMode, ...],
    expected_modes: tuple[BenchmarkMode, ...],
    precondition_preserved: bool,
    task_ids: tuple[str, ...],
) -> None:
    manifest = _filtering_manifest()
    original_json = manifest.model_dump_json()

    effective = benchmark_cli._effective_manifest(
        manifest,
        modes=requested,
        repeat=None,
    )

    assert tuple(task.task_id for task in effective.tasks) == task_ids
    assert effective.tasks[0].modes == expected_modes
    assert (effective.tasks[0].index_precondition is not None) is (
        precondition_preserved
    )
    if BenchmarkMode.FRESH in requested and len(effective.tasks) == 2:
        assert effective.tasks[1] == manifest.tasks[1]
    assert BenchmarkManifest.model_validate_json(effective.model_dump_json()) == (
        effective
    )
    assert manifest.model_dump_json() == original_json


def test_repeated_mode_filtering_does_not_mutate_the_loaded_manifest() -> None:
    manifest = _filtering_manifest()
    original_modes = manifest.tasks[0].modes
    original_precondition = manifest.tasks[0].index_precondition

    first = benchmark_cli._effective_manifest(
        manifest,
        modes=(BenchmarkMode.FRESH,),
        repeat=None,
    )
    second = benchmark_cli._effective_manifest(
        manifest,
        modes=(BenchmarkMode.FRESH,),
        repeat=None,
    )

    assert first == second
    assert manifest.tasks[0].modes == original_modes
    assert manifest.tasks[0].index_precondition == original_precondition
    assert first.tasks[0].index_precondition is None


def test_committed_asp_manifest_can_be_derived_as_fresh_only() -> None:
    fixture = Path(__file__).parent / "fixtures" / "asp_discovery_benchmark.json"
    manifest = BenchmarkManifest.model_validate_json(fixture.read_bytes())

    effective = benchmark_cli._effective_manifest(
        manifest,
        modes=(BenchmarkMode.FRESH,),
        repeat=None,
    )

    assert effective.tasks
    assert all(task.modes == (BenchmarkMode.FRESH,) for task in effective.tasks)
    assert all(task.index_precondition is None for task in effective.tasks)
    assert BenchmarkManifest.model_validate_json(effective.model_dump_json()) == (
        effective
    )


def test_fresh_only_cli_filter_reaches_runner_with_mixed_mode_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    task = _task("mixed")
    task["modes"] = ["fresh", "indexed", "hybrid"]
    task["index_precondition"] = {"kind": "clean"}
    manifest = _manifest(tmp_path, task)
    original_runner = benchmark_cli.run_discovery_benchmark
    observed: list[BenchmarkManifest] = []

    async def tracking_runner(
        effective: BenchmarkManifest, *args: object, **kwargs: object
    ) -> object:
        observed.append(effective)
        return await original_runner(effective, *args, **kwargs)

    monkeypatch.setattr(
        benchmark_cli,
        "run_discovery_benchmark",
        tracking_runner,
    )

    result = _invoke(
        tmp_path,
        manifest,
        "--modes",
        "fresh",
        "--format",
        "json",
    )

    assert result.exit_code == 0, result.output
    assert len(observed) == 1
    assert observed[0].tasks[0].modes == (BenchmarkMode.FRESH,)
    assert observed[0].tasks[0].index_precondition is None


def test_help_documents_command_and_regression_exit_policy() -> None:
    result = runner.invoke(
        app,
        ["benchmark", "discovery", "--help"],
        terminal_width=TERMINAL_WIDTH,
    )
    output = click.unstyle(result.output)

    assert result.exit_code == 0
    for value in (
        "PATH",
        "--tasks",
        "--modes",
        "--repeat",
        "--config",
        "--format",
        "--output",
    ):
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


@pytest.mark.parametrize("config_kind", ["root", "file"])
def test_sibling_repository_uses_explicit_configuration_for_every_mode(
    tmp_path: Path, config_kind: str
) -> None:
    benchmark_root = tmp_path / "benchmarks"
    _write(
        benchmark_root,
        ".contextforge/config.toml",
        "[models]\nprovider='fake'\nmodel='benchmark-root'\n",
    )
    _write(
        benchmark_root,
        "ASP/.contextforge/config.toml",
        (
            "[models]\nprovider='fake'\nmodel='asp-model'\n"
            "endpoint='http://asp.invalid:11434'\ncontext_window=65536\n"
        ),
    )
    _write(benchmark_root, "ASP/main.py", "def main():\n    return 1\n")
    task = _task("asp", repository_path="ASP")
    task["allowed_warnings"] = ["hybrid-index-unavailable"]
    manifest = _manifest(tmp_path, task)
    config_root = benchmark_root / "ASP"
    config = (
        config_root
        if config_kind == "root"
        else config_root / ".contextforge/config.toml"
    )
    before = {
        path.relative_to(config_root): path.read_bytes()
        for path in config_root.rglob("*")
        if path.is_file()
    }

    result = _invoke(
        benchmark_root,
        manifest,
        "--config",
        str(config),
        "--format",
        "json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert {run["mode"] for run in payload["runs"]} == {"fresh", "hybrid"}
    assert {run["provider_id"] for run in payload["runs"]} == {"fake"}
    assert {run["model_id"] for run in payload["runs"]} == {"asp-model"}
    assert result.stderr
    assert {
        path.relative_to(config_root): path.read_bytes()
        for path in config_root.rglob("*")
        if path.is_file()
    } == before


def test_omitted_config_uses_benchmark_root_configuration(tmp_path: Path) -> None:
    _repository(tmp_path)
    _write(
        tmp_path,
        "repository/.contextforge/config.toml",
        "[models]\nprovider='fake'\nmodel='repository-model'\n",
    )
    manifest = _manifest(tmp_path)

    result = _invoke(
        tmp_path,
        manifest,
        "--modes",
        "fresh",
        "--format",
        "json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["runs"][0]["provider_id"] == "fake"
    assert payload["runs"][0]["model_id"] == "benchmark-v1"


def test_invalid_config_fails_cleanly_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    manifest = _manifest(tmp_path)

    async def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("benchmark execution must not start")

    monkeypatch.setattr(benchmark_cli, "run_discovery_benchmark", unexpected)
    missing = tmp_path / "missing-config.toml"

    for output_format in ("text", "json"):
        result = _invoke(
            tmp_path,
            manifest,
            "--config",
            str(missing),
            "--format",
            output_format,
        )

        assert result.exit_code == 2
        assert result.stdout == ""
        assert "configuration file does not exist" in result.stderr


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
