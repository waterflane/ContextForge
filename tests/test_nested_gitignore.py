import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from contextforge.context import (
    ContextSelection,
    build_context_package,
    build_project_tree,
    resolve_selection,
)
from contextforge.intelligence import (
    acquire_index_lock,
    build_structural_index,
    initialize_index,
)
from contextforge.repositories import scan_repository
from contextforge.repositories.ignore import IgnoreRulesError, load_ignore_rules


def _write(root: Path, path: str, content: str = "content\n") -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


class _PathItem(Protocol):
    path: str


def _paths(items: tuple[_PathItem, ...]) -> tuple[str, ...]:
    return tuple(item.path for item in items)


def test_nested_star_excludes_all_content_and_omits_empty_directory(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "nested/.gitignore", "*\n")
    _write(tmp_path, "nested/source.py")
    _write(tmp_path, "nested/generated/output.js")
    _write(tmp_path, "nested/generated/data.json")

    snapshot = scan_repository(tmp_path)
    tree = build_project_tree(snapshot)

    assert snapshot.files == ()
    assert _paths(snapshot.ignored_files) == (
        "nested/.gitignore",
        "nested/generated",
        "nested/source.py",
    )
    assert tree.entries == ()
    assert tree.directory_count == 0


def test_nested_star_negation_keeps_one_file(tmp_path: Path) -> None:
    _write(tmp_path, "nested/.gitignore", "*\n!keep.py\n")
    _write(tmp_path, "nested/keep.py")
    _write(tmp_path, "nested/drop.py")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ("nested/keep.py",)
    assert _paths(snapshot.ignored_files) == (
        "nested/.gitignore",
        "nested/drop.py",
    )


def test_nested_patterns_are_relative_to_their_control_directory(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "pkg/.gitignore", "/generated/\n*.log\n")
    _write(tmp_path, "pkg/generated/output.py")
    _write(tmp_path, "pkg/debug.log")
    _write(tmp_path, "pkg/deeper/debug.log")
    _write(tmp_path, "pkg/deeper/generated/keep.py")
    _write(tmp_path, "generated/root.py")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == (
        "generated/root.py",
        "pkg/.gitignore",
        "pkg/deeper/generated/keep.py",
    )
    assert _paths(snapshot.ignored_files) == (
        "pkg/debug.log",
        "pkg/deeper/debug.log",
        "pkg/generated",
    )


def test_parent_gitignore_rules_are_inherited_by_deeper_directories(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".gitignore", "*.tmp\n")
    _write(tmp_path, "pkg/deeper/cache.tmp")
    _write(tmp_path, "pkg/deeper/keep.py")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == (".gitignore", "pkg/deeper/keep.py")
    assert _paths(snapshot.ignored_files) == ("pkg/deeper/cache.tmp",)


def test_deeper_gitignore_adds_exclusions_without_leaking_to_siblings(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "pkg/deeper/.gitignore", "*.log\n")
    _write(tmp_path, "pkg/deeper/debug.log")
    _write(tmp_path, "pkg/deeper/keep.py")
    _write(tmp_path, "pkg/sibling/debug.log")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == (
        "pkg/deeper/.gitignore",
        "pkg/deeper/keep.py",
        "pkg/sibling/debug.log",
    )
    assert _paths(snapshot.ignored_files) == ("pkg/deeper/debug.log",)


def test_contextforgeignore_keeps_precedence_over_nested_gitignore(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".contextforgeignore", "pkg/blocked.py\n!pkg/keep.log\n")
    _write(tmp_path, "pkg/.gitignore", "!blocked.py\n*.log\n")
    _write(tmp_path, "pkg/blocked.py")
    _write(tmp_path, "pkg/keep.log")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == (
        ".contextforgeignore",
        "pkg/.gitignore",
        "pkg/keep.log",
    )
    assert _paths(snapshot.ignored_files) == ("pkg/blocked.py",)
    assert snapshot.ignored_files[0].source == "contextforgeignore"


def test_deeper_negation_reopens_a_file_when_its_parent_is_reachable(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".gitignore", "*.log\n")
    _write(tmp_path, "pkg/.gitignore", "!keep.log\n")
    _write(tmp_path, "pkg/keep.log")
    _write(tmp_path, "pkg/drop.log")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == (
        ".gitignore",
        "pkg/.gitignore",
        "pkg/keep.log",
    )
    assert _paths(snapshot.ignored_files) == ("pkg/drop.log",)


def test_nested_negation_reopens_parent_before_reopening_descendant(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "pkg/.gitignore",
        "*\n!deep/\n!deep/keep.py\n",
    )
    _write(tmp_path, "pkg/deep/keep.py")
    _write(tmp_path, "pkg/deep/drop.py")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ("pkg/deep/keep.py",)
    assert _paths(snapshot.ignored_files) == (
        "pkg/.gitignore",
        "pkg/deep/drop.py",
    )


def test_ignored_parent_is_pruned_without_reading_its_nested_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".gitignore", "blocked/\n")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / ".gitignore").write_bytes(b"\xff")
    _write(tmp_path, "blocked/keep.py")
    original_iterdir = Path.iterdir

    def reject_blocked_traversal(path: Path) -> Iterator[Path]:
        if path == blocked:
            raise AssertionError("ignored directory was traversed")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", reject_blocked_traversal)

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == (".gitignore",)
    assert _paths(snapshot.ignored_files) == ("blocked",)


def test_invalid_reachable_nested_gitignore_is_reported(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_bytes(b"\xff")

    with pytest.raises(IgnoreRulesError, match="unable to read ignore file"):
        scan_repository(tmp_path)


def test_windows_and_posix_root_spellings_produce_the_same_inventory(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "pkg/.gitignore", "*\n!keep.py\n")
    _write(tmp_path, "pkg/keep.py")
    _write(tmp_path, "pkg/drop.py")
    windows_style = (
        str(tmp_path).replace("/", "\\") if os.name == "nt" else str(tmp_path)
    )
    posix_style = tmp_path.as_posix()

    windows_snapshot = scan_repository(windows_style)
    posix_snapshot = scan_repository(posix_style)
    nested_rules = load_ignore_rules(tmp_path).for_directory(tmp_path / "pkg")

    assert windows_snapshot == posix_snapshot
    assert _paths(windows_snapshot.files) == ("pkg/keep.py",)
    assert nested_rules.match(r"pkg\drop.py") == nested_rules.match("pkg/drop.py")


def test_scanner_tree_index_selection_and_context_share_nested_inventory(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "visible.py", "visible = True\n")
    _write(tmp_path, "pkg/.gitignore", "*\n!keep.py\n")
    _write(tmp_path, "pkg/keep.py", "kept = True\n")
    _write(tmp_path, "pkg/drop.py", "dropped = True\n")
    snapshot = scan_repository(tmp_path)
    expected = ("pkg/keep.py", "visible.py")

    tree = build_project_tree(snapshot)
    selection = resolve_selection(snapshot, ContextSelection())
    package = build_context_package(snapshot)
    initialize_index(tmp_path)
    with acquire_index_lock(tmp_path, "nested-ignore") as lock:
        index = build_structural_index(snapshot, lock)

    assert _paths(snapshot.files) == expected
    assert (
        tuple(entry.path for entry in tree.entries if entry.kind == "file") == expected
    )
    assert _paths(selection.files) == expected
    assert tuple(item.path for item in index.code_maps) == expected
    assert tuple(item.path for item in package.files) == expected
    assert package.tree == tree


def test_gitkeep_is_an_ordinary_file(tmp_path: Path) -> None:
    _write(tmp_path, "empty/.gitkeep", "")
    _write(tmp_path, "ignored/.gitignore", "*\n")
    _write(tmp_path, "ignored/.gitkeep", "")

    snapshot = scan_repository(tmp_path)

    assert _paths(snapshot.files) == ("empty/.gitkeep",)
    assert _paths(snapshot.ignored_files) == (
        "ignored/.gitignore",
        "ignored/.gitkeep",
    )
