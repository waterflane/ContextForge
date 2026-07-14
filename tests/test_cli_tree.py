import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner, Result

from contextforge.cli.main import app

runner = CliRunner()
TERMINAL_WIDTH = 120


def _invoke_tree(root: Path, *arguments: str) -> Result:
    return runner.invoke(
        app,
        ["tree", str(root), *arguments],
        terminal_width=TERMINAL_WIDTH,
    )


def _plain_output(result: Result) -> str:
    return click.unstyle(result.output)


def test_tree_help_documents_the_public_contract() -> None:
    result = runner.invoke(app, ["tree", "--help"], terminal_width=TERMINAL_WIDTH)
    output = _plain_output(result)

    assert result.exit_code == 0
    for value in ("PATH", "--depth", "--format", "--output", "--force"):
        assert value in output


def test_tree_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["tree"], terminal_width=TERMINAL_WIDTH)

    assert result.exit_code == 0
    assert _plain_output(result) == ".\n`-- app.py\n"
    assert str(tmp_path.resolve()) not in result.output


def test_tree_text_markdown_and_json_formats(tmp_path: Path) -> None:
    source = tmp_path / "данные" / "пример.py"
    source.parent.mkdir()
    source.write_text("print('привет')\n", encoding="utf-8")

    text = _invoke_tree(tmp_path, "--format", "text")
    markdown = _invoke_tree(tmp_path, "--format", "MARKDOWN")
    json_result = _invoke_tree(tmp_path, "--format", "json")

    assert text.exit_code == markdown.exit_code == json_result.exit_code == 0
    assert text.stdout == markdown.stdout
    assert "данные/" in text.stdout
    payload = json.loads(json_result.stdout)
    assert payload["schema_version"] == 1
    assert payload["root"] == "."
    assert [entry["path"] for entry in payload["entries"]] == [
        "данные",
        "данные/пример.py",
    ]


def test_tree_depth_limits_all_output_formats(tmp_path: Path) -> None:
    source = tmp_path / "src" / "nested" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("pass\n", encoding="utf-8")

    text = _invoke_tree(tmp_path, "--depth", "1")
    json_result = _invoke_tree(tmp_path, "--depth", "1", "--format", "json")

    assert text.exit_code == json_result.exit_code == 0
    assert _plain_output(text) == ".\n`-- src/\n"
    payload = json.loads(json_result.stdout)
    assert payload["max_depth"] == 1
    assert payload["entries"] == [{"path": "src", "kind": "directory"}]


def test_tree_output_file_is_created_atomically(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("pass\n", encoding="utf-8")
    output = tmp_path / "tree.json"

    result = _invoke_tree(
        repository,
        "--format",
        "json",
        "--output",
        str(output),
    )

    assert result.exit_code == 0
    assert _plain_output(result).strip() == f"Output written to {output.resolve()}"
    assert json.loads(output.read_text(encoding="utf-8"))["file_count"] == 1
    assert list(tmp_path.glob(".tree.json.*.tmp")) == []


def test_tree_refuses_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "tree.txt"
    output.write_text("original", encoding="utf-8")

    result = _invoke_tree(tmp_path, "--output", str(output))

    assert result.exit_code == 1
    assert "already exists" in _plain_output(result)
    assert output.read_text(encoding="utf-8") == "original"
    assert "Traceback" not in result.output


def test_tree_force_atomically_replaces_existing_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("pass\n", encoding="utf-8")
    output = tmp_path / "tree.txt"
    output.write_text("original", encoding="utf-8")

    result = _invoke_tree(
        repository,
        "--output",
        str(output),
        "--force",
    )

    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8") == ".\n`-- app.py\n"
    assert list(tmp_path.glob(".tree.txt.*.tmp")) == []


def test_tree_output_parent_must_exist(tmp_path: Path) -> None:
    output = tmp_path / "missing" / "tree.txt"

    result = _invoke_tree(tmp_path, "--output", str(output))

    assert result.exit_code == 1
    assert "parent directory does not exist" in _plain_output(result)
    assert not output.exists()


def test_tree_invalid_ignore_file_is_an_expected_scanner_error(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_bytes(b"\xff")

    result = _invoke_tree(tmp_path)

    assert result.exit_code == 1
    assert "unable to read ignore file" in _plain_output(result)
    assert "Traceback" not in result.output


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_tree_invalid_root_is_a_usage_error(tmp_path: Path, root_kind: str) -> None:
    root = tmp_path / root_kind
    if root_kind == "file":
        root.write_text("not a repository", encoding="utf-8")

    result = _invoke_tree(root)

    assert result.exit_code == 2
    expected = "not a directory" if root_kind == "file" else "does not exist"
    assert expected in _plain_output(result)
    assert "Traceback" not in result.output


@pytest.mark.parametrize("depth", ["-1", "not-an-integer"])
def test_tree_invalid_depth_is_rejected(tmp_path: Path, depth: str) -> None:
    result = _invoke_tree(tmp_path, "--depth", depth)

    assert result.exit_code == 2
    assert "--depth" in _plain_output(result)
    assert "Traceback" not in result.output


def test_tree_invalid_format_is_rejected(tmp_path: Path) -> None:
    result = _invoke_tree(tmp_path, "--format", "yaml")

    assert result.exit_code == 2
    assert "yaml" in _plain_output(result)
    assert "Traceback" not in result.output


def test_root_help_and_existing_commands_remain_available(tmp_path: Path) -> None:
    help_result = runner.invoke(app, ["--help"], terminal_width=TERMINAL_WIDTH)
    version_result = runner.invoke(app, ["version"])
    doctor_result = runner.invoke(app, ["doctor"])
    scan_result = runner.invoke(app, ["scan", str(tmp_path)])
    help_output = _plain_output(help_result)

    assert help_result.exit_code == 0
    assert all(
        command in help_output for command in ("tree", "scan", "version", "doctor")
    )
    assert (
        version_result.exit_code
        == doctor_result.exit_code
        == scan_result.exit_code
        == 0
    )
