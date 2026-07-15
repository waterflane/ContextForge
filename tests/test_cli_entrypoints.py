import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import contextforge.__main__ as module_entry
import contextforge.cli.main as cli_module


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


def _console_command() -> list[str]:
    executable_name = "contextforge.exe" if os.name == "nt" else "contextforge"
    executable = Path(sys.executable).with_name(executable_name)
    assert executable.is_file(), f"installed console script is missing: {executable}"
    return [str(executable)]


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


def test_console_script_and_module_share_the_run_wrapper() -> None:
    project_root = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert configuration["project"]["scripts"]["contextforge"] == (
        "contextforge.cli.main:run"
    )
    assert module_entry.run is cli_module.run


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
