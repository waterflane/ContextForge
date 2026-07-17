import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.context import (
    ProjectTree,
    ProjectTreeEntry,
    ProjectTreeError,
    build_project_tree,
    render_project_tree,
    render_project_tree_json,
    render_project_tree_markdown,
)
from contextforge.repositories import (
    ProjectFile,
    ProjectSnapshot,
    ScanSummary,
)

SHA256 = "a" * 64


def _file(path: str) -> ProjectFile:
    return ProjectFile(
        path=path,
        size_bytes=0,
        sha256=SHA256,
        is_text=True,
    )


def _forged_file(path: object) -> ProjectFile:
    return ProjectFile.model_construct(
        path=path,
        size_bytes=0,
        language=None,
        sha256=SHA256,
        is_text=True,
    )


def _snapshot(tmp_path: Path, *paths: str) -> ProjectSnapshot:
    files = tuple(_file(path) for path in paths)
    return ProjectSnapshot(
        root=tmp_path,
        files=files,
        summary=ScanSummary(
            file_count=len(files),
            ignored_count=0,
            total_size_bytes=0,
        ),
    )


def _specs(tree: ProjectTree) -> list[tuple[str, str]]:
    return [(entry.path, entry.kind) for entry in tree.entries]


def test_empty_snapshot_builds_empty_tree(tmp_path: Path) -> None:
    tree = build_project_tree(_snapshot(tmp_path))

    assert tree == ProjectTree()
    assert render_project_tree(tree) == ".\n"


def test_one_file_builds_one_root_entry(tmp_path: Path) -> None:
    tree = build_project_tree(_snapshot(tmp_path, "README.md"))

    assert _specs(tree) == [("README.md", "file")]
    assert tree.file_count == 1
    assert tree.directory_count == 0


def test_nested_directories_and_multiple_roots_are_synthesized(
    tmp_path: Path,
) -> None:
    tree = build_project_tree(
        _snapshot(
            tmp_path,
            "root.txt",
            "src/app.py",
            "docs/guide.md",
            "src/nested/model.py",
        )
    )

    assert _specs(tree) == [
        ("docs", "directory"),
        ("docs/guide.md", "file"),
        ("src", "directory"),
        ("src/nested", "directory"),
        ("src/nested/model.py", "file"),
        ("src/app.py", "file"),
        ("root.txt", "file"),
    ]
    assert tree.file_count == 4
    assert tree.directory_count == 3


def test_order_is_directory_first_code_point_sorted_and_input_independent(
    tmp_path: Path,
) -> None:
    paths = ("b.txt", "A.txt", "z/file", "Alpha/file", "a.txt")

    first = build_project_tree(_snapshot(tmp_path, *paths))
    second = build_project_tree(_snapshot(tmp_path, *reversed(paths)))

    assert first == second
    assert _specs(first) == [
        ("Alpha", "directory"),
        ("Alpha/file", "file"),
        ("z", "directory"),
        ("z/file", "file"),
        ("A.txt", "file"),
        ("a.txt", "file"),
        ("b.txt", "file"),
    ]


def test_unicode_paths_remain_unmodified(tmp_path: Path) -> None:
    tree = build_project_tree(_snapshot(tmp_path, "данные/пример.py", "文档/说明.md"))

    assert _specs(tree) == [
        ("данные", "directory"),
        ("данные/пример.py", "file"),
        ("文档", "directory"),
        ("文档/说明.md", "file"),
    ]
    assert "данные" in render_project_tree_json(tree)
    assert "\\u" not in render_project_tree_json(tree)


def test_builder_accepts_included_file_inventory_without_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py")

    def reject_traversal(path: Path) -> None:
        raise AssertionError(f"unexpected traversal: {path}")

    monkeypatch.setattr(Path, "iterdir", reject_traversal)

    assert build_project_tree(snapshot).file_count == 1
    assert build_project_tree(snapshot.files).file_count == 1


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "./file.py",
        "dir//file.py",
        "dir/",
        "../secret.py",
        "safe/../secret.py",
        "/etc/passwd",
        r"\rooted\file.py",
        r"C:\repo\file.py",
        "C:/repo/file.py",
        r"C:file.py",
        r"\\server\share\file.py",
        "nul\x00file.py",
        "escape\x1b[2J.py",
    ],
)
def test_malformed_and_nonportable_inventory_paths_are_rejected(
    tmp_path: Path, path: str
) -> None:
    snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(_forged_file(path),),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(
            file_count=1,
            ignored_count=0,
            total_size_bytes=0,
        ),
    )

    with pytest.raises(ProjectTreeError):
        build_project_tree(snapshot)


def test_windows_separators_are_canonicalized_for_forged_serialized_inventory(
    tmp_path: Path,
) -> None:
    tree = build_project_tree((_forged_file(r"src\app.py"),))

    assert _specs(tree) == [("src", "directory"), ("src/app.py", "file")]


def test_non_project_file_inventory_entry_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectTreeError, match="ProjectFile"):
        build_project_tree(("file.py",))  # type: ignore[arg-type]


def test_non_string_forged_inventory_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectTreeError, match="must be a string"):
        build_project_tree((_forged_file(42),))


def test_duplicate_inventory_paths_are_rejected(tmp_path: Path) -> None:
    snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(_file("same.py"), _file("same.py")),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(
            file_count=2,
            ignored_count=0,
            total_size_bytes=0,
        ),
    )

    with pytest.raises(ProjectTreeError, match="duplicate inventory path"):
        build_project_tree(snapshot)


@pytest.mark.parametrize(
    "paths", [("node", "node/child.py"), ("node/child.py", "node")]
)
def test_file_directory_collisions_are_rejected(
    tmp_path: Path, paths: tuple[str, str]
) -> None:
    with pytest.raises(ProjectTreeError, match="both a file and directory"):
        build_project_tree(tuple(_file(path) for path in paths))


def test_text_and_markdown_render_the_normative_visual_tree(tmp_path: Path) -> None:
    tree = build_project_tree(
        _snapshot(tmp_path, "docs/guide.md", "src/app.py", "pyproject.toml")
    )
    expected = (
        ".\n|-- docs/\n|   `-- guide.md\n|-- src/\n|   `-- app.py\n`-- pyproject.toml\n"
    )

    assert render_project_tree(tree) == expected
    assert render_project_tree_markdown(tree) == expected
    assert str(tmp_path.resolve()) not in expected


def test_depth_zero_one_deeper_and_unlimited_semantics(tmp_path: Path) -> None:
    tree = build_project_tree(_snapshot(tmp_path, "src/nested/app.py", "root.txt"))

    assert render_project_tree(tree, max_depth=0) == ".\n"
    assert render_project_tree(tree, max_depth=1) == (".\n|-- src/\n`-- root.txt\n")
    assert render_project_tree(tree, max_depth=2) == (
        ".\n|-- src/\n|   `-- nested/\n`-- root.txt\n"
    )
    unlimited = render_project_tree(tree)
    assert render_project_tree(tree, max_depth=99) == unlimited
    assert "app.py" in unlimited


@pytest.mark.parametrize("depth", [-1, True, 1.5, "1"])
def test_invalid_core_depth_is_rejected(tmp_path: Path, depth: object) -> None:
    tree = build_project_tree(_snapshot(tmp_path, "file.py"))

    with pytest.raises(ProjectTreeError, match="maximum depth"):
        render_project_tree(tree, max_depth=depth)  # type: ignore[arg-type]


def test_json_has_explicit_stable_schema_and_depth_filtered_counts(
    tmp_path: Path,
) -> None:
    tree = build_project_tree(_snapshot(tmp_path, "src/nested/app.py", "root.txt"))

    first = render_project_tree_json(tree, max_depth=1)
    second = render_project_tree_json(tree, max_depth=1)
    payload = json.loads(first)

    assert first == second
    assert payload == {
        "schema_version": 1,
        "root": ".",
        "max_depth": 1,
        "file_count": 1,
        "directory_count": 1,
        "entries": [
            {"path": "src", "kind": "directory"},
            {"path": "root.txt", "kind": "file"},
        ],
    }
    assert first.endswith("\n")
    assert "\\" not in first
    assert str(tmp_path.resolve()) not in first


def test_tree_models_are_frozen_and_reject_inconsistent_shapes(
    tmp_path: Path,
) -> None:
    tree = build_project_tree(_snapshot(tmp_path, "file.py"))

    with pytest.raises(ValidationError):
        tree.file_count = 2
    with pytest.raises(ValidationError):
        ProjectTree(
            entries=(ProjectTreeEntry(path="file.py", kind="file"),),
            file_count=2,
            directory_count=0,
        )
    with pytest.raises(ValidationError, match="directory_count"):
        ProjectTree(
            entries=(ProjectTreeEntry(path="file.py", kind="file"),),
            file_count=1,
            directory_count=1,
        )
    with pytest.raises(ValidationError, match="canonical pre-order"):
        ProjectTree(
            entries=(
                ProjectTreeEntry(path="z.py", kind="file"),
                ProjectTreeEntry(path="a.py", kind="file"),
            ),
            file_count=2,
            directory_count=0,
        )
    with pytest.raises(ValidationError):
        ProjectTreeEntry(path="file.py", kind="file", unknown=True)  # type: ignore[call-arg]
