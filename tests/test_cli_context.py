import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click
import pytest
from typer.testing import CliRunner, Result

import contextforge.cli.context_commands as context_cli
import contextforge.context.builder as builder_module
from contextforge.cli.main import app
from contextforge.context import ContextPackage, ContextRenderError
from contextforge.repositories import scan_repository as real_scan_repository

runner = CliRunner()
TERMINAL_WIDTH = 200


def _write_repository(root: Path, files: Mapping[str, str | bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative_path, content in files.items():
        path = root.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def _invoke_create(root: Path, *arguments: str) -> Result:
    return runner.invoke(
        app,
        ["context", "create", str(root), *arguments],
        terminal_width=TERMINAL_WIDTH,
    )


def _invoke_inspect(package: Path) -> Result:
    return runner.invoke(
        app,
        ["context", "inspect", str(package)],
        terminal_width=TERMINAL_WIDTH,
    )


def _plain(value: str) -> str:
    return click.unstyle(value)


def _json_package(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, _plain(result.output)
    return cast(dict[str, Any], json.loads(result.stdout))


def test_context_help_and_subcommand_help_document_the_contract() -> None:
    context_help = runner.invoke(
        app, ["context", "--help"], terminal_width=TERMINAL_WIDTH
    )
    create_help = runner.invoke(
        app, ["context", "create", "--help"], terminal_width=TERMINAL_WIDTH
    )
    inspect_help = runner.invoke(
        app, ["context", "inspect", "--help"], terminal_width=TERMINAL_WIDTH
    )

    assert (
        context_help.exit_code == create_help.exit_code == inspect_help.exit_code == 0
    )
    assert all(
        command in _plain(context_help.output) for command in ("create", "inspect")
    )
    for option in (
        "PATH",
        "--task",
        "--include",
        "--directory",
        "--glob",
        "--exclude",
        "--include-lines",
        "--include-tree",
        "--format",
        "--output",
        "--force",
        "--max-files",
    ):
        assert option in _plain(create_help.output)
    assert "PACKAGE" in _plain(inspect_help.output)


def test_create_exact_include_json_stdout_is_parseable_and_portable(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write_repository(
        repository,
        {"src/app.py": "print('hello')\n", "README.md": "read\n"},
    )

    result = _invoke_create(
        repository,
        "--include",
        "src/app.py",
        "--task",
        "Review app",
        "--format",
        "json",
    )
    payload = _json_package(result)

    assert payload["title"] == "Review app"
    assert [item["path"] for item in payload["files"]] == ["src/app.py"]
    assert str(repository.resolve()) not in result.stdout
    assert result.stderr == ""


def test_directory_include_is_recursive(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(
        repository,
        {
            "src/app.py": "app\n",
            "src/nested/model.py": "model\n",
            "tests/test_app.py": "test\n",
        },
    )

    payload = _json_package(
        _invoke_create(repository, "--directory", "src", "--format", "json")
    )

    assert [item["path"] for item in payload["files"]] == [
        "src/app.py",
        "src/nested/model.py",
    ]


def test_glob_multiple_includes_and_exclusions_are_core_resolved(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write_repository(
        repository,
        {
            "README.md": "readme\n",
            "src/app.py": "app\n",
            "src/generated.py": "generated\n",
            "tests/test_app.py": "test\n",
        },
    )

    globbed = _json_package(
        _invoke_create(repository, "--glob", "*.py", "--format", "json")
    )
    multiple = _json_package(
        _invoke_create(
            repository,
            "--include",
            "README.md",
            "--directory",
            "src",
            "--exclude",
            "**/generated.py",
            "--format",
            "json",
        )
    )

    assert [item["path"] for item in globbed["files"]] == [
        "src/app.py",
        "src/generated.py",
        "tests/test_app.py",
    ]
    assert [item["path"] for item in multiple["files"]] == [
        "README.md",
        "src/app.py",
    ]


def test_no_include_uses_approved_all_files_default_and_task_default(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"a.txt": "a", "b.txt": "b"})

    payload = _json_package(_invoke_create(repository, "--format", "json"))

    assert payload["title"] == "Context package"
    assert [item["path"] for item in payload["files"]] == ["a.txt", "b.txt"]


def test_unmatched_selector_and_empty_task_are_usage_errors(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})

    unmatched = _invoke_create(repository, "--include", "missing.py")
    empty_task = _invoke_create(repository, "--task", "   ")

    assert unmatched.exit_code == empty_task.exit_code == 2
    assert "matched no snapshot file" in _plain(unmatched.stderr)
    assert "task description must not be empty" in _plain(empty_task.stderr)
    assert unmatched.stdout == empty_task.stdout == ""
    assert "Traceback" not in unmatched.output + empty_task.output


@pytest.mark.parametrize(
    "arguments",
    [
        ("--include-lines", "app.py:0-1"),
        ("--format", "yaml"),
        ("--max-files", "0"),
        ("--max-context-bytes", "0"),
    ],
)
def test_invalid_ranges_formats_and_nonpositive_limits_are_usage_errors(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})

    result = _invoke_create(repository, *arguments)

    assert result.exit_code == 2
    assert "Traceback" not in result.output


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_invalid_repository_roots_are_usage_errors(
    tmp_path: Path, root_kind: str
) -> None:
    repository = tmp_path / root_kind
    if root_kind == "file":
        repository.write_text("not a directory", encoding="utf-8")

    result = _invoke_create(repository)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "repository root" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_line_ranges_and_optional_tree_are_rendered_from_core(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "one\ntwo\nthree\nfour\n"})

    payload = _json_package(
        _invoke_create(
            repository,
            "--include",
            "app.py",
            "--include-lines",
            "app.py:2-3",
            "--no-include-tree",
            "--format",
            "json",
        )
    )

    assert payload["tree"] is None
    assert payload["files"][0]["selection"] == "ranges"
    assert payload["files"][0]["blocks"][0]["text"] == "two\nthree\n"
    assert payload["files"][0]["blocks"][0]["start_line"] == 2
    assert payload["files"][0]["blocks"][0]["end_line"] == 3


def test_markdown_stdout_contains_only_the_package(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})

    result = _invoke_create(repository, "--include", "app.py")

    assert result.exit_code == 0
    assert result.stdout.startswith("# Context package\n")
    assert "## Project tree" in result.stdout
    assert "### `app.py`" in result.stdout
    assert result.stderr == ""
    assert str(repository.resolve()) not in result.stdout


def test_output_write_refusal_and_force_replacement(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})
    output = tmp_path / "package.json"

    created = _invoke_create(repository, "--format", "json", "--output", str(output))
    refused = _invoke_create(repository, "--format", "json", "--output", str(output))
    original = output.read_text(encoding="utf-8")
    forced = _invoke_create(
        repository,
        "--task",
        "Replacement",
        "--format",
        "json",
        "--output",
        str(output),
        "--force",
    )

    assert created.exit_code == forced.exit_code == 0
    assert created.stdout.strip() == f"Output written to {output.resolve()}"
    assert refused.exit_code == 1
    assert "already exists" in _plain(refused.stderr)
    assert "Traceback" not in refused.output
    assert json.loads(original)["title"] == "Context package"
    assert json.loads(output.read_text(encoding="utf-8"))["title"] == "Replacement"
    assert list(tmp_path.glob(".package.json.*.tmp")) == []


def test_output_parent_must_exist(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})
    output = tmp_path / "missing" / "package.md"

    result = _invoke_create(repository, "--output", str(output))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "parent directory does not exist" in _plain(result.stderr)
    assert not output.exists()


def test_render_failure_is_an_expected_operational_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})

    def fail_render(package: object) -> str:
        raise ContextRenderError(f"cannot render {type(package).__name__}")

    monkeypatch.setattr(context_cli, "render_context_package_markdown", fail_render)

    result = _invoke_create(repository)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "cannot render ContextPackage" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_unexpected_context_error_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"app.py": "pass\n"})

    def fail_build(source: object, options: object) -> object:
        raise RuntimeError(
            f"unexpected context failure: {source} {type(options).__name__}"
        )

    monkeypatch.setattr(context_cli, "build_context_package", fail_build)

    result = _invoke_create(repository)

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert result.stdout == ""
    assert "Error:" not in result.output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--max-files", "1"), "maximum selected files"),
        (("--max-context-bytes", "3"), "maximum total content bytes"),
    ],
)
def test_builder_limits_are_usage_errors(
    tmp_path: Path, arguments: tuple[str, ...], message: str
) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"a.txt": "aa", "b.txt": "bb"})

    result = _invoke_create(repository, *arguments)

    assert result.exit_code == 2
    assert message in _plain(result.stderr)
    assert result.stdout == ""
    assert "Traceback" not in result.output


def test_unicode_paths_and_content_round_trip_on_stdout(tmp_path: Path) -> None:
    repository = tmp_path / "проект"
    _write_repository(repository, {"данные/пример.txt": "Привет, 世界\n"})

    result = _invoke_create(repository, "--format", "json")
    payload = _json_package(result)

    assert payload["files"][0]["path"] == "данные/пример.txt"
    assert payload["files"][0]["blocks"][0]["text"] == "Привет, 世界\n"


def test_changed_selected_file_is_an_expected_operational_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    source = repository / "app.py"
    _write_repository(repository, {"app.py": "before\n"})

    def scan_then_change(root: str | Path) -> object:
        snapshot = real_scan_repository(root)
        source.write_text("changed\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(builder_module, "scan_repository", scan_then_change)

    result = _invoke_create(repository, "--include", "app.py")

    assert result.exit_code == 1
    assert "selected file" in _plain(result.stderr)
    assert "changed" in _plain(result.stderr)
    assert result.stdout == ""
    assert "Traceback" not in result.output


def test_decode_error_is_expected_and_does_not_publish_output(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_repository(repository, {"late-invalid.txt": b"a" * 8192 + b"\xff"})
    output = tmp_path / "package.json"

    result = _invoke_create(repository, "--format", "json", "--output", str(output))

    assert result.exit_code == 1
    assert "not valid UTF-8" in _plain(result.stderr)
    assert result.stdout == ""
    assert not output.exists()
    assert "Traceback" not in result.output


def _write_valid_json_package(tmp_path: Path, *, ranged: bool = False) -> Path:
    repository = tmp_path / "repository"
    _write_repository(repository, {"src/app.py": "one\ntwo\nthree\n"})
    package = tmp_path / "package.json"
    arguments = [
        "--include",
        "src/app.py",
        "--task",
        "Inspect this",
        "--format",
        "json",
        "--output",
        str(package),
    ]
    if ranged:
        arguments[2:2] = ["--include-lines", "src/app.py:2-3"]
    result = _invoke_create(repository, *arguments)
    assert result.exit_code == 0
    return package


def test_inspect_valid_package_lists_task_statistics_paths_and_ranges(
    tmp_path: Path,
) -> None:
    package = _write_valid_json_package(tmp_path, ranged=True)
    (tmp_path / "repository" / "src" / "app.py").unlink()

    result = _invoke_inspect(package)

    assert result.exit_code == 0
    for expected in (
        "Schema version: 1",
        "Task: Inspect this",
        "Selected files: 1",
        "Ranged files: 1",
        "Included content bytes: 10",
        "Selected paths:",
        "src/app.py (2-3)",
    ):
        assert expected in _plain(result.stdout)
    assert result.stderr == ""


def test_inspect_rejects_malformed_unsupported_and_invalid_statistics(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    valid = _write_valid_json_package(tmp_path)
    payload = cast(dict[str, Any], json.loads(valid.read_text(encoding="utf-8")))
    unsupported = tmp_path / "unsupported.json"
    payload["schema_version"] = 2
    unsupported.write_text(json.dumps(payload), encoding="utf-8")
    invalid_statistics = tmp_path / "invalid-statistics.json"
    payload["schema_version"] = 1
    payload["statistics"]["included_line_count"] = 999
    invalid_statistics.write_text(json.dumps(payload), encoding="utf-8")

    results = (
        _invoke_inspect(malformed),
        _invoke_inspect(unsupported),
        _invoke_inspect(invalid_statistics),
    )

    assert [result.exit_code for result in results] == [1, 1, 1]
    assert "malformed JSON" in _plain(results[0].stderr)
    assert "unsupported context package schema version" in _plain(results[1].stderr)
    assert "statistics" in _plain(results[2].stderr)
    for result in results:
        assert result.stdout == ""
        assert "Traceback" not in result.output


def test_inspect_missing_path_is_usage_error(tmp_path: Path) -> None:
    result = _invoke_inspect(tmp_path / "missing.json")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "does not exist" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_inspect_directory_is_an_operational_read_error(tmp_path: Path) -> None:
    result = _invoke_inspect(tmp_path)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unable to read context package" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_root_commands_and_api_import_remain_available(tmp_path: Path) -> None:
    from contextforge.api.app import app as api_app

    help_result = runner.invoke(app, ["--help"], terminal_width=TERMINAL_WIDTH)
    version_result = runner.invoke(app, ["version"])
    doctor_result = runner.invoke(app, ["doctor"])
    scan_result = runner.invoke(app, ["scan", str(tmp_path)])
    tree_result = runner.invoke(app, ["tree", str(tmp_path)])

    assert all(
        command in _plain(help_result.output)
        for command in ("context", "scan", "tree", "version", "doctor")
    )
    assert [
        result.exit_code
        for result in (
            help_result,
            version_result,
            doctor_result,
            scan_result,
            tree_result,
        )
    ] == [0, 0, 0, 0, 0]
    assert api_app.title == "ContextForge"


def test_context_package_output_can_be_loaded_as_public_model(tmp_path: Path) -> None:
    package = _write_valid_json_package(tmp_path)

    model = ContextPackage.model_validate_json(package.read_text(encoding="utf-8"))

    assert model.task_description == "Inspect this"
