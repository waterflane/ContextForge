import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

import contextforge.repositories.scanner as scanner_module
from contextforge.repositories import ScanOptions, scan_repository
from contextforge.repositories.files import is_binary_file as binary_detector


class _PathItem(Protocol):
    path: str


def _paths(items: tuple[_PathItem, ...]) -> list[str]:
    return [item.path for item in items]


def _create_symlink(link: Path, target: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are not available on this platform: {exc}")


def test_empty_repository(tmp_path: Path) -> None:
    snapshot = scan_repository(tmp_path)

    assert snapshot.root == tmp_path.resolve()
    assert snapshot.files == ()
    assert snapshot.ignored_files == ()
    assert snapshot.skipped_files == ()
    assert snapshot.summary.model_dump() == {
        "file_count": 0,
        "ignored_count": 0,
        "total_size_bytes": 0,
        "languages": {},
        "discovered_count": 0,
        "protected_count": 0,
        "binary_count": 0,
        "oversized_count": 0,
        "failed_count": 0,
        "symlink_count": 0,
        "unsupported_count": 0,
        "skipped_count": 0,
    }


def test_one_text_file_has_metadata_language_and_hash(tmp_path: Path) -> None:
    content = b"print('hello')\n"
    (tmp_path / "app.py").write_bytes(content)

    snapshot = scan_repository(tmp_path)

    assert len(snapshot.files) == 1
    project_file = snapshot.files[0]
    assert project_file.path == "app.py"
    assert project_file.size_bytes == len(content)
    assert project_file.language == "Python"
    assert project_file.sha256 == hashlib.sha256(content).hexdigest()
    assert project_file.is_text is True


def test_nested_unicode_files_are_portable_and_deterministic(tmp_path: Path) -> None:
    files = {
        "z-last.txt": "last",
        "nested/beta.js": "const beta = true;",
        "nested/alpha.md": "# Alpha",
        "данные/пример.py": "print('привет')",
    }
    for relative_path, content in reversed(tuple(files.items())):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    first = scan_repository(tmp_path)
    second = scan_repository(tmp_path / "nested" / "..")

    expected = sorted(files)
    assert _paths(first.files) == expected
    assert _paths(second.files) == expected
    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


@pytest.mark.parametrize(
    "directory", [".venv", "node_modules", "__pycache__", "build", "dist"]
)
def test_ordinary_default_exclusions_are_reported(
    tmp_path: Path, directory: str
) -> None:
    ignored_path = tmp_path / directory / "nested" / "artifact.txt"
    ignored_path.parent.mkdir(parents=True)
    ignored_path.write_text("ignored", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ["keep.txt"]
    assert _paths(snapshot.ignored_files) == [f"{directory}/nested/artifact.txt"]
    assert snapshot.ignored_files[0].source == "default"
    assert snapshot.summary.ignored_count == 1


@pytest.mark.parametrize("directory", [".git", ".hg", ".svn"])
def test_protected_vcs_directories_are_never_entered(
    tmp_path: Path, directory: str
) -> None:
    metadata = tmp_path / directory
    metadata.mkdir()
    (metadata / "config").write_text("secret", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text(
        f"!{directory}/\n!{directory}/config\n", encoding="utf-8"
    )

    snapshot = scan_repository(tmp_path)

    assert f"{directory}/config" not in _paths(snapshot.files)
    protected = [item for item in snapshot.ignored_files if item.source == "protected"]
    assert _paths(tuple(protected)) == [directory]
    assert snapshot.summary.protected_count == 1


def test_protected_vcs_metadata_file_is_excluded_case_safely(tmp_path: Path) -> None:
    (tmp_path / ".GIT").write_text("gitdir: elsewhere", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text("!.GIT\n", encoding="utf-8")

    snapshot = scan_repository(tmp_path)

    assert ".GIT" not in _paths(snapshot.files)
    protected = [item for item in snapshot.ignored_files if item.source == "protected"]
    assert _paths(tuple(protected)) == [".GIT"]
    assert snapshot.summary.protected_count == 1


def test_gitignore_and_contextforgeignore_precedence(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "*.log\nignored-by-git.txt\n", encoding="utf-8"
    )
    (tmp_path / ".contextforgeignore").write_text(
        "!keep.log\nignored-by-context.txt\n", encoding="utf-8"
    )
    for name in (
        "debug.log",
        "keep.log",
        "ignored-by-git.txt",
        "ignored-by-context.txt",
        "keep.txt",
    ):
        (tmp_path / name).write_text(name, encoding="utf-8")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == [
        ".contextforgeignore",
        ".gitignore",
        "keep.log",
        "keep.txt",
    ]
    ignored = {item.path: item.source for item in snapshot.ignored_files}
    assert ignored == {
        "debug.log": "gitignore",
        "ignored-by-context.txt": "contextforgeignore",
        "ignored-by-git.txt": "gitignore",
    }


def test_ordinary_default_can_be_reincluded_by_project_rules(tmp_path: Path) -> None:
    virtual_file = tmp_path / ".venv" / "keep.py"
    virtual_file.parent.mkdir()
    virtual_file.write_text("value = 1", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text(
        "!.venv/\n!.venv/keep.py\n", encoding="utf-8"
    )

    snapshot = scan_repository(tmp_path)

    assert ".venv/keep.py" in _paths(snapshot.files)


def test_ignore_control_files_are_included_unless_explicitly_ignored(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text(
        ".contextforgeignore\n", encoding="utf-8"
    )

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == [".gitignore"]
    assert _paths(snapshot.ignored_files) == [".contextforgeignore"]


def test_ignore_files_can_be_missing_or_disabled(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("git.txt\n", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text("context.txt\n", encoding="utf-8")
    (tmp_path / "git.txt").write_text("git", encoding="utf-8")
    (tmp_path / "context.txt").write_text("context", encoding="utf-8")

    snapshot = scan_repository(
        tmp_path,
        ScanOptions(respect_gitignore=False, respect_contextforgeignore=False),
    )

    assert _paths(snapshot.files) == [
        ".contextforgeignore",
        ".gitignore",
        "context.txt",
        "git.txt",
    ]
    assert snapshot.ignored_files == ()


def test_binary_oversized_exact_limit_empty_and_unknown_files(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"text\x00more")
    (tmp_path / "oversized.txt").write_bytes(b"12345")
    (tmp_path / "exact.txt").write_bytes(b"1234")
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "unknown.xyz").write_text("unknown", encoding="utf-8")

    snapshot = scan_repository(tmp_path, ScanOptions(max_file_size_bytes=4))

    assert _paths(snapshot.files) == ["empty", "exact.txt"]
    assert snapshot.files[0].language is None
    assert snapshot.files[0].sha256 == hashlib.sha256(b"").hexdigest()
    skipped = {item.path: item.reason for item in snapshot.skipped_files}
    assert skipped == {
        "binary.bin": "too_large",
        "oversized.txt": "too_large",
        "unknown.xyz": "too_large",
    }
    assert snapshot.summary.binary_count == 0
    assert snapshot.summary.oversized_count == 3


def test_binary_file_within_size_limit_is_reported(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"a\x00b")

    snapshot = scan_repository(tmp_path, ScanOptions(max_file_size_bytes=3))

    assert snapshot.files == ()
    assert snapshot.skipped_files[0].reason == "binary"
    assert snapshot.summary.binary_count == 1


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_invalid_repository_roots(tmp_path: Path, root_kind: str) -> None:
    root = tmp_path / root_kind
    expected_error: type[OSError]
    if root_kind == "file":
        root.write_text("not a directory", encoding="utf-8")
        expected_error = NotADirectoryError
    else:
        expected_error = FileNotFoundError

    with pytest.raises(expected_error):
        scan_repository(root)


def test_file_and_broken_symlinks_are_excluded(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    _create_symlink(tmp_path / "link.txt", target, target_is_directory=False)
    _create_symlink(
        tmp_path / "broken.txt", tmp_path / "missing.txt", target_is_directory=False
    )

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ["target.txt"]
    assert {item.path: item.reason for item in snapshot.skipped_files} == {
        "broken.txt": "symlink",
        "link.txt": "symlink",
    }
    assert snapshot.summary.symlink_count == 2


def test_directory_symlink_cycle_is_never_followed(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "file.txt").write_text("text", encoding="utf-8")
    _create_symlink(nested / "back", tmp_path, target_is_directory=True)

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ["nested/file.txt"]
    assert _paths(snapshot.skipped_files) == ["nested/back"]
    assert snapshot.skipped_files[0].reason == "symlink"


def test_symlink_classification_branch_is_portably_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "link"
    link.write_text("placeholder", encoding="utf-8")
    original = Path.stat

    def symlink_lstat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original(path, follow_symlinks=follow_symlinks)
        if path != link or follow_symlinks:
            return result
        values = list(result)
        values[0] = stat.S_IFLNK
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", symlink_lstat)

    snapshot = scan_repository(tmp_path)

    assert snapshot.files == ()
    assert snapshot.skipped_files[0].reason == "symlink"
    assert snapshot.summary.symlink_count == 1


def test_individual_read_failure_is_reported_and_scan_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("unreadable", encoding="utf-8")
    (tmp_path / "readable.txt").write_text("readable", encoding="utf-8")

    def fail_one_file(path: Path) -> bool:
        if path == unreadable:
            raise PermissionError("denied for test")
        return binary_detector(path)

    monkeypatch.setattr(scanner_module, "is_binary_file", fail_one_file)

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ["readable.txt"]
    assert _paths(snapshot.skipped_files) == ["unreadable.txt"]
    assert snapshot.skipped_files[0].reason == "unreadable"
    assert "PermissionError" in (snapshot.skipped_files[0].detail or "")
    assert snapshot.summary.failed_count == 1


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not reliable on Windows"
)
def test_real_unreadable_file_is_reported_where_supported(tmp_path: Path) -> None:
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("unreadable", encoding="utf-8")
    unreadable.chmod(0)
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("current user can still read mode-000 files")
        snapshot = scan_repository(tmp_path)
    finally:
        unreadable.chmod(0o600)

    assert snapshot.skipped_files[0].reason == "unreadable"


def test_unexpected_file_error_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")

    def unexpected_error(path: Path) -> bool:
        raise RuntimeError(f"unexpected: {path.name}")

    monkeypatch.setattr(scanner_module, "is_binary_file", unexpected_error)

    with pytest.raises(RuntimeError, match="unexpected: file.txt"):
        scan_repository(tmp_path)


def test_summary_counters_language_distribution_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_bytes(b"abc")
    (tmp_path / "LICENSE").write_bytes(b"x")
    ignored = tmp_path / "build" / "artifact.txt"
    ignored.parent.mkdir()
    ignored.write_text("ignored", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"a\x00b")
    (tmp_path / "oversized.txt").write_bytes(b"12345")
    failed = tmp_path / "failed.txt"
    failed.write_text("fail", encoding="utf-8")
    metadata = tmp_path / ".git"
    metadata.mkdir()
    (metadata / "config").write_text("protected", encoding="utf-8")

    def fail_one_file(path: Path) -> bool:
        if path == failed:
            raise PermissionError("denied for test")
        return binary_detector(path)

    monkeypatch.setattr(scanner_module, "is_binary_file", fail_one_file)

    snapshot = scan_repository(tmp_path, ScanOptions(max_file_size_bytes=4))
    summary = snapshot.summary

    assert summary.discovered_count == 6
    assert summary.file_count == 2
    assert summary.ignored_count == 1
    assert summary.protected_count == 1
    assert summary.binary_count == 1
    assert summary.oversized_count == 1
    assert summary.failed_count == 1
    assert summary.symlink_count == 0
    assert summary.unsupported_count == 0
    assert summary.skipped_count == 5
    assert summary.total_size_bytes == 4
    assert summary.languages == {"Python": 1}
    assert summary.discovered_count == (
        summary.file_count
        + summary.ignored_count
        + summary.binary_count
        + summary.oversized_count
        + summary.failed_count
        + summary.symlink_count
        + summary.unsupported_count
    )


def test_scan_does_not_modify_files_or_generate_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("value = 'unchanged'\n", encoding="utf-8")
    before_content = source.read_bytes()
    before_stat = source.stat()
    before_paths = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    first = scan_repository(tmp_path)
    second = scan_repository(tmp_path)

    after_paths = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert source.read_bytes() == before_content
    assert source.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert after_paths == before_paths
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_hash_failure_after_binary_check_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("text", encoding="utf-8")

    def fail_hash(candidate: Path) -> str:
        raise OSError(f"hash read failed: {candidate.name}")

    monkeypatch.setattr(scanner_module, "sha256_file", fail_hash)

    snapshot = scan_repository(tmp_path)

    assert snapshot.files == ()
    assert snapshot.skipped_files[0].reason == "unreadable"


def test_directory_read_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    original = Path.iterdir

    def fail_directory(path: Path) -> Iterator[Path]:
        if path == blocked:
            raise PermissionError("blocked directory")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", fail_directory)

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ["keep.txt"]
    assert _paths(snapshot.skipped_files) == ["blocked"]
    assert snapshot.summary.failed_count == 1


def test_entry_stat_failure_is_reported_and_scan_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = tmp_path / "failed.txt"
    failed.write_text("failed", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    original = Path.stat

    def fail_lstat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == failed and not follow_symlinks:
            raise PermissionError("cannot inspect entry")
        return original(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fail_lstat)

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ["keep.txt"]
    assert _paths(snapshot.skipped_files) == ["failed.txt"]
    assert snapshot.summary.failed_count == 1


def test_non_regular_entry_is_reported_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    special = tmp_path / "special"
    special.write_text("placeholder", encoding="utf-8")
    original = Path.stat

    def special_lstat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original(path, follow_symlinks=follow_symlinks)
        if path != special or follow_symlinks:
            return result
        values = list(result)
        values[0] = stat.S_IFIFO
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", special_lstat)

    snapshot = scan_repository(tmp_path)

    assert snapshot.files == ()
    assert snapshot.skipped_files[0].reason == "unsupported"
    assert snapshot.summary.unsupported_count == 1


def test_root_directory_read_failure_is_not_silently_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.iterdir

    def fail_root(path: Path) -> Iterator[Path]:
        if path == tmp_path:
            raise PermissionError("cannot list root")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", fail_root)

    with pytest.raises(PermissionError, match="cannot list root"):
        scan_repository(tmp_path)
