import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from contextforge.git import (
    GitDiffError,
    GitDiffRequest,
    collect_git_diff,
)
from contextforge.repositories import scan_repository


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def _repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "ContextForge Tests")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "old.py").write_text("OLD = 1\n", encoding="utf-8")
    _git(root, "add", "app.py", "old.py")
    _git(root, "commit", "-q", "-m", "initial")


def test_working_staged_and_base_diffs_use_bounded_portable_context(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    working = collect_git_diff(snapshot, GitDiffRequest(mode="working"))
    assert working.available is True
    assert "VALUE = 2" in working.text
    assert working.touched_paths == ("app.py", "new.py")
    assert {item.status for item in working.changed_files} >= {"modified", "untracked"}
    assert working.head_revision == base

    _git(tmp_path, "add", "app.py", "new.py")
    staged_snapshot = scan_repository(tmp_path)
    staged = collect_git_diff(staged_snapshot, GitDiffRequest(mode="staged"))
    comparison = collect_git_diff(
        staged_snapshot,
        GitDiffRequest(mode="base", base_ref=base),
    )
    assert staged.available is comparison.available is True
    assert "new.py" in staged.touched_paths
    assert (
        next(item for item in staged.changed_files if item.path == "new.py").status
        == "added"
    )
    assert comparison.base_revision == base


def test_deleted_summary_and_path_filter_are_canonical(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "old.py").unlink()
    (tmp_path / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    context = collect_git_diff(
        snapshot,
        GitDiffRequest(mode="working", paths=("app.py",)),
    )

    assert context.touched_paths == ("app.py",)
    assert context.deleted_paths == ()
    with pytest.raises(GitDiffError, match="snapshot"):
        collect_git_diff(snapshot, GitDiffRequest(paths=("missing.py",)))

    unfiltered = collect_git_diff(snapshot, GitDiffRequest(mode="working"))
    assert unfiltered.deleted_paths == ("old.py",)
    assert (
        next(item for item in unfiltered.changed_files if item.path == "old.py").status
        == "deleted"
    )


def test_non_git_and_missing_executable_are_explicitly_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        str(tmp_path.parent.resolve()),
    )

    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    non_git = collect_git_diff(snapshot, GitDiffRequest())
    assert non_git.available is False
    assert non_git.diagnostics == ("not-a-git-repository",)

    def missing(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr("contextforge.git.diff._run_git", missing)
    unavailable = collect_git_diff(snapshot, GitDiffRequest())
    assert unavailable.available is False
    assert unavailable.diagnostics == ("git-executable-unavailable",)


def test_repository_check_and_diff_failures_are_generic_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("absolute path and secret must not escape")

    monkeypatch.setattr("contextforge.git.diff._run_git", denied)
    checked = collect_git_diff(snapshot, GitDiffRequest())
    assert checked.diagnostics == ("git-repository-check-failed",)

    calls = 0

    def fail_after_check(*args: object, **kwargs: object) -> Any:
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=b"true\n",
                stderr=b"",
                stdout_truncated=False,
            )
        raise OSError("diff failed")

    monkeypatch.setattr("contextforge.git.diff._run_git", fail_after_check)
    diff = collect_git_diff(snapshot, GitDiffRequest())
    assert diff.diagnostics == ("git-diff-unavailable",)


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "base"},
        {"mode": "working", "base_ref": "main"},
        {"mode": "base", "base_ref": "-danger"},
        {"paths": ("b.py", "a.py")},
        {"paths": ("../secret",)},
        {"context_lines": 101},
        {"max_bytes": 0},
    ],
)
def test_request_rejects_arbitrary_or_unsafe_git_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GitDiffRequest.model_validate(payload)


def test_diff_truncation_preserves_valid_utf8_and_records_warning(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    (tmp_path / "app.py").write_text("VALUE = 'РџСЂРёРІРµС‚'\n" * 20, encoding="utf-8")
    snapshot = scan_repository(tmp_path)

    context = collect_git_diff(
        snapshot,
        GitDiffRequest(mode="working", max_bytes=80),
    )

    assert context.available is True
    assert context.truncated is True
    assert context.diagnostics == ("diff-truncated",)
    assert len(context.text.encode("utf-8")) <= 80
    context.text.encode("utf-8").decode("utf-8")


def test_collector_never_uses_shell_or_accepts_model_command_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    snapshot = scan_repository(tmp_path)
    calls: list[tuple[tuple[str, ...], bool]] = []
    original: Any = subprocess.Popen

    def record(command: tuple[str, ...], **kwargs: object) -> object:
        calls.append((command, bool(kwargs.get("shell"))))
        return original(command, **kwargs)

    monkeypatch.setattr("contextforge.git.diff.subprocess.Popen", record)
    collect_git_diff(snapshot, GitDiffRequest(mode="working"))

    assert calls
    assert all(command[0] == "git" and shell is False for command, shell in calls)
    assert all("sh" not in command[0].lower() for command, _ in calls)


def test_git_context_models_validate_digests_order_and_runtime_types(
    tmp_path: Path,
) -> None:
    from contextforge.git import GitChangedFile, GitDiffContext

    with pytest.raises(TypeError, match="ProjectSnapshot"):
        collect_git_diff(object(), GitDiffRequest())  # type: ignore[arg-type]
    snapshot = scan_repository(tmp_path)
    with pytest.raises(TypeError, match="GitDiffRequest"):
        collect_git_diff(snapshot, object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="SHA-256"):
        GitDiffContext(
            available=True,
            mode="working",
            sha256=hashlib.sha256(b"").hexdigest(),
            text="different",
        )
    with pytest.raises(ValidationError, match="canonical"):
        GitDiffContext(
            available=True,
            mode="working",
            sha256=hashlib.sha256(b"").hexdigest(),
            changed_files=(
                GitChangedFile(path="b.py", status="modified"),
                GitChangedFile(path="a.py", status="modified"),
            ),
        )
    with pytest.raises(ValidationError, match="cannot contain"):
        GitDiffContext(
            available=False,
            mode="working",
            sha256=hashlib.sha256(b"x").hexdigest(),
            text="x",
        )
