import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from importlib.metadata import distribution
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import contextforge.__main__ as module_entry
import contextforge.cli.main as cli_module
from contextforge._metadata import APP_NAME, __version__


def _write_entrypoint_repository(root: Path) -> None:
    files = {
        "pyproject.toml": "[project]\nname = 'example'\n",
        "a.py": "a = 1\n",
        "b.py": "b = 1\n",
        "src/contextforge/context/__init__.py": "",
        "src/contextforge/context/package.py": "value = 1\n",
        "tests/test_alpha.py": "def test_alpha():\n    pass\n",
    }
    for relative_path, content in files.items():
        path = root.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")


def _console_command(name: str = "contextforge") -> list[str]:
    executable_name = f"{name}.exe" if os.name == "nt" else name
    executable = Path(sys.executable).with_name(executable_name)
    assert executable.is_file(), f"installed console script is missing: {executable}"
    return [str(executable)]


def _normalized_help(output: str, executable_name: str) -> str:
    decoration = str.maketrans({character: " " for character in "─│┌┐└┘"})
    return " ".join(
        output.translate(decoration).replace(executable_name, "contextforge").split()
    )


def _run_entrypoint(
    entrypoint: str, repository: Path, arguments: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    command = (
        _console_command()
        if entrypoint == "console"
        else [sys.executable, "-m", "contextforge"]
    )
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [*command, "context", "create", ".", *arguments, "--format", "json"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_disables_click_windows_argument_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def record_run(*, windows_expand_args: bool) -> None:
        calls.append(windows_expand_args)

    monkeypatch.setattr(cli_module, "app", record_run)

    cli_module.run()

    assert calls == [False]


def test_run_supports_embedded_text_streams_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def record_run(*, windows_expand_args: bool) -> None:
        calls.append(windows_expand_args)

    monkeypatch.setattr(sys, "stdout", StringIO())
    monkeypatch.setattr(sys, "stderr", StringIO())
    monkeypatch.setattr(cli_module, "app", record_run)

    cli_module.run()

    assert calls == [False]


def test_console_script_and_module_share_the_run_wrapper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    scripts = configuration["project"]["scripts"]
    assert scripts["contextforge"] == scripts["ctxf"] == "contextforge.cli.main:run"
    assert "cf" not in scripts
    assert "version" not in configuration["project"]
    assert configuration["project"]["dynamic"] == ["version"]
    assert configuration["tool"]["hatch"]["version"]["path"] == (
        "src/contextforge/_metadata.py"
    )
    assert module_entry.run is cli_module.run


def test_installed_distribution_exposes_both_console_scripts() -> None:
    console_scripts = {
        entry_point.name: entry_point.value
        for entry_point in distribution("contextforge").entry_points
        if entry_point.group == "console_scripts"
    }

    assert console_scripts["contextforge"] == "contextforge.cli.main:run"
    assert console_scripts["ctxf"] == console_scripts["contextforge"]
    assert "cf" not in console_scripts


@pytest.mark.parametrize(
    ("entrypoint", "arguments"),
    [
        ("contextforge", ["version"]),
        ("contextforge", ["--version"]),
        ("module", ["--version"]),
        ("ctxf", ["version"]),
        ("ctxf", ["--version"]),
    ],
)
def test_version_entrypoints_are_clean_outside_a_repository(
    tmp_path: Path, entrypoint: str, arguments: list[str]
) -> None:
    command = (
        [sys.executable, "-m", "contextforge"]
        if entrypoint == "module"
        else _console_command(entrypoint)
    )

    result = subprocess.run(
        [*command, *arguments],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"{APP_NAME} {__version__}\n"
    assert result.stderr == ""
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["doctor"],
        ["index", "status", "--help"],
        ["context", "suggest", "--help"],
        ["mcp", "serve", "--help"],
    ],
)
def test_console_alias_has_identical_help_and_command_behavior(
    tmp_path: Path, arguments: list[str]
) -> None:
    results = [
        subprocess.run(
            [*_console_command(name), *arguments],
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        for name in ("contextforge", "ctxf")
    ]

    assert results[0].returncode == results[1].returncode
    if "--help" in arguments:
        assert _normalized_help(results[0].stdout, "contextforge") == _normalized_help(
            results[1].stdout, "ctxf"
        )
    else:
        assert results[0].stdout == results[1].stdout
    assert results[0].stderr == results[1].stderr


@pytest.mark.parametrize("entrypoint", ["console", "module"])
def test_entrypoints_emit_utf8_when_the_inherited_stream_encoding_is_legacy(
    tmp_path: Path, entrypoint: str
) -> None:
    (tmp_path / "unicode.py").write_text('message = "Привет, 世界"\n', encoding="utf-8")
    command = (
        _console_command()
        if entrypoint == "console"
        else [sys.executable, "-m", "contextforge"]
    )
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "0"
    environment["PYTHONIOENCODING"] = "cp1251:strict"

    result = subprocess.run(
        [*command, "context", "create", ".", "--format", "json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = cast(dict[str, Any], json.loads(result.stdout))
    assert payload["files"][0]["blocks"][0]["text"] == ('message = "Привет, 世界"\n')


@pytest.mark.skipif(
    os.name != "nt",
    reason="Click performs native argv glob expansion only on Windows",
)
@pytest.mark.parametrize("entrypoint", ["console", "module"])
@pytest.mark.parametrize(
    ("arguments", "expected_paths"),
    [
        (
            (
                "--directory",
                "src/contextforge/context",
                "--include",
                "pyproject.toml",
                "--exclude",
                "**/__init__.py",
            ),
            ("pyproject.toml", "src/contextforge/context/package.py"),
        ),
        (
            ("--glob", "src/**/*.py"),
            (
                "src/contextforge/context/__init__.py",
                "src/contextforge/context/package.py",
            ),
        ),
        (("--glob", "tests/test_*.py"), ("tests/test_alpha.py",)),
        (
            ("--exclude", "[ab]*.py"),
            (
                "pyproject.toml",
                "src/contextforge/context/__init__.py",
                "src/contextforge/context/package.py",
                "tests/test_alpha.py",
            ),
        ),
    ],
)
def test_native_windows_entrypoints_preserve_selector_arguments(
    tmp_path: Path,
    entrypoint: str,
    arguments: tuple[str, ...],
    expected_paths: tuple[str, ...],
) -> None:
    _write_entrypoint_repository(tmp_path)

    result = _run_entrypoint(entrypoint, tmp_path, arguments)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = cast(dict[str, Any], json.loads(result.stdout))
    assert tuple(item["path"] for item in payload["files"]) == expected_paths
