import copy
import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

import contextforge.context.builder as builder_module
from contextforge.context import (
    ContextBlock,
    ContextBuilder,
    ContextBuildError,
    ContextBuildOptions,
    ContextContentByteLimitError,
    ContextFile,
    ContextFileCountLimitError,
    ContextPackage,
    ContextProject,
    ContextSelection,
    ContextSourceByteLimitError,
    LineRange,
    LineRangeRequest,
    SelectedFileChangedError,
    SelectedFileDecodeError,
    SelectedTextFile,
    SelectorNoMatchError,
    build_context_package,
    canonical_line_count,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot, scan_repository


def _write_files(root: Path, files: Mapping[str, str | bytes]) -> ProjectSnapshot:
    for relative_path, content in files.items():
        path = root.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
    return scan_repository(root)


def _options(
    selection: ContextSelection | None = None, **changes: object
) -> ContextBuildOptions:
    values: dict[str, object] = {
        "task_description": "Review the repository",
        "selection": selection or ContextSelection(),
    }
    values.update(changes)
    return ContextBuildOptions(**values)  # type: ignore[arg-type]


def _dump(package: ContextPackage) -> dict[str, object]:
    return package.model_dump(mode="json")


def _block(
    text: str, start_line: int | None = None, end_line: int | None = None
) -> ContextBlock:
    encoded = text.encode()
    return ContextBlock(
        start_line=start_line,
        end_line=end_line,
        text=text,
        line_count=canonical_line_count(text),
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def test_one_selected_file_builds_complete_portable_package(tmp_path: Path) -> None:
    snapshot = _write_files(
        tmp_path,
        {
            "README.md": "# Project\n",
            "src/app.py": "print('hello')\n",
        },
    )

    package = build_context_package(
        snapshot,
        _options(ContextSelection(exact_paths=("src/app.py",))),
    )

    assert package.schema_version == 1
    assert package.task_description == "Review the repository"
    assert tuple(item.path for item in package.items) == ("src/app.py",)
    assert package.files[0].language == "Python"
    assert package.files[0].source_sha256 == snapshot.files[1].sha256
    assert package.files[0].blocks[0].text == "print('hello')\n"
    assert package.project.selectable_file_count == 2
    assert package.tree is not None
    assert package.statistics.selected_file_count == 1
    assert package.statistics.selected_source_bytes == len(b"print('hello')\n")
    assert str(tmp_path.resolve()) not in str(_dump(package))


def test_multiple_directory_glob_excludes_and_duplicates_are_canonical(
    tmp_path: Path,
) -> None:
    snapshot = _write_files(
        tmp_path,
        {
            "src/z.py": "z\n",
            "src/a.py": "a\n",
            "src/generated.py": "generated\n",
            "tests/test_app.py": "test\n",
            "README.md": "readme\n",
        },
    )
    selection = ContextSelection(
        exact_paths=("src/z.py", "src/z.py"),
        directories=("src",),
        globs=("*.py", "*.py"),
        exclusions=("**/generated.py", "tests/**"),
    )

    package = build_context_package(snapshot, _options(selection))

    assert tuple(file.path for file in package.files) == ("src/a.py", "src/z.py")
    assert len({file.path for file in package.files}) == len(package.files)
    assert package.statistics.selected_file_count == 2


def test_line_ranges_build_disjoint_blocks_and_range_statistics(
    tmp_path: Path,
) -> None:
    snapshot = _write_files(tmp_path, {"app.py": "one\ntwo\nthree\nfour"})
    selection = ContextSelection(
        exact_paths=("app.py",),
        line_ranges=(
            LineRangeRequest("app.py", LineRange(3, 4)),
            LineRangeRequest("app.py", LineRange(1, 1)),
            LineRangeRequest("app.py", LineRange(3, 3)),
        ),
    )

    package = build_context_package(snapshot, _options(selection))
    item = package.files[0]

    assert item.selection == "ranges"
    assert tuple((block.start_line, block.end_line) for block in item.blocks) == (
        (1, 1),
        (3, 4),
    )
    assert tuple(block.text for block in item.blocks) == ("one\n", "three\nfour")
    assert package.statistics.ranged_file_count == 1
    assert package.statistics.included_line_count == 3


def test_optional_tree_does_not_remove_project_metadata(tmp_path: Path) -> None:
    snapshot = _write_files(tmp_path, {"src/app.py": "app", "README": "readme"})

    with_tree = build_context_package(snapshot, _options(include_tree=True))
    without_tree = build_context_package(snapshot, _options(include_tree=False))

    assert with_tree.tree is not None
    assert without_tree.tree is None
    assert with_tree.project == without_tree.project
    assert with_tree.files == without_tree.files
    assert with_tree.statistics == without_tree.statistics


@pytest.mark.parametrize("task", ["", "   ", "bad\ncontrol", "bad\x7fcontrol"])
def test_empty_or_control_character_task_is_rejected_before_build(task: str) -> None:
    with pytest.raises(ValidationError, match="task description"):
        ContextBuildOptions(title=task)


def test_task_is_trimmed_and_tab_is_allowed(tmp_path: Path) -> None:
    snapshot = _write_files(tmp_path, {"file.txt": "text"})

    package = build_context_package(
        snapshot,
        ContextBuildOptions.model_validate({"task_description": "  Review\tthis  "}),
    )

    assert package.title == "Review\tthis"


def test_unmatched_selector_propagates_selector_details(tmp_path: Path) -> None:
    snapshot = _write_files(tmp_path, {"file.txt": "text"})

    with pytest.raises(SelectorNoMatchError, match="missing.py"):
        build_context_package(
            snapshot,
            _options(ContextSelection(exact_paths=("missing.py",))),
        )


def test_file_count_limit_is_checked_after_deduplication_before_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _write_files(tmp_path, {"a.py": "a", "b.py": "b"})
    calls = 0

    def reject_read(*args: object, **kwargs: object) -> SelectedTextFile:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected read: {args!r} {kwargs!r}")

    monkeypatch.setattr(builder_module, "read_selected_text_file", reject_read)
    with pytest.raises(ContextFileCountLimitError) as error:
        build_context_package(
            snapshot,
            _options(
                ContextSelection(globs=("*.py", "*.py")),
                max_files=1,
            ),
        )

    assert calls == 0
    assert error.value.configured_limit == 1
    assert error.value.observed_value == 2
    assert "maximum selected files" in str(error.value)


def test_source_byte_limit_is_checked_for_a_path_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _write_files(tmp_path, {"a.txt": "aa", "b.txt": "b"})

    def reject_read(*args: object, **kwargs: object) -> SelectedTextFile:
        raise AssertionError(f"unexpected read: {args!r} {kwargs!r}")

    monkeypatch.setattr(builder_module, "read_selected_text_file", reject_read)
    with pytest.raises(ContextSourceByteLimitError) as error:
        build_context_package(
            snapshot,
            _options(max_source_bytes_per_file=1),
        )

    assert error.value.path == "a.txt"
    assert error.value.configured_limit == 1
    assert error.value.observed_value == 2
    assert "a.txt" in str(error.value)


def test_total_content_limit_accepts_equality_and_fails_during_accumulation(
    tmp_path: Path,
) -> None:
    snapshot = _write_files(tmp_path, {"a.txt": "aa", "b.txt": "bb"})

    exact = build_context_package(snapshot, _options(max_total_content_bytes=4))

    assert exact.statistics.included_content_bytes == 4
    with pytest.raises(ContextContentByteLimitError) as error:
        build_context_package(snapshot, _options(max_total_content_bytes=3))
    assert error.value.path == "b.txt"
    assert error.value.configured_limit == 3
    assert error.value.observed_value == 4


def test_ranged_content_not_raw_source_controls_total_limit(tmp_path: Path) -> None:
    snapshot = _write_files(tmp_path, {"large.txt": "a\nlong line\n"})
    selection = ContextSelection(
        line_ranges=(LineRangeRequest("large.txt", LineRange(1, 1)),)
    )

    package = build_context_package(
        snapshot,
        _options(selection, max_total_content_bytes=2),
    )

    assert package.statistics.selected_source_bytes == len(b"a\nlong line\n")
    assert package.statistics.included_content_bytes == 2


def test_changed_file_and_decode_error_abort_without_a_package(tmp_path: Path) -> None:
    changed_root = tmp_path / "changed"
    changed = _write_files(changed_root, {"file.txt": "before"})
    (changed_root / "file.txt").write_text("after!", encoding="utf-8")

    with pytest.raises(SelectedFileChangedError):
        build_context_package(changed, _options())

    decode_root = tmp_path / "decode"
    decode = _write_files(decode_root, {"file.txt": b"a" * 8192 + b"\xff"})
    with pytest.raises(SelectedFileDecodeError):
        build_context_package(decode, _options())


def test_later_failure_never_returns_a_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _write_files(tmp_path, {"a.txt": "good", "b.txt": "bad"})
    original = builder_module.read_selected_text_file  # type: ignore[attr-defined]
    calls: list[str] = []

    def fail_second(
        active_snapshot: ProjectSnapshot, project_file: ProjectFile, **kwargs: object
    ) -> SelectedTextFile:
        calls.append(project_file.path)
        if project_file.path == "b.txt":
            raise SelectedFileDecodeError("selected file is not valid UTF-8: b.txt")
        return original(active_snapshot, project_file)

    monkeypatch.setattr(builder_module, "read_selected_text_file", fail_second)

    with pytest.raises(SelectedFileDecodeError):
        ContextBuilder(snapshot, _options()).build()
    assert calls == ["a.txt", "b.txt"]


def test_order_statistics_unicode_and_empty_files_are_deterministic(
    tmp_path: Path,
) -> None:
    snapshot = _write_files(
        tmp_path,
        {
            "z-empty.txt": "",
            "src/alpha.py": "print('РїСЂРёРІРµС‚')\n",
            "README.md": "# ж–‡жЎЈ\n",
        },
    )
    first = build_context_package(snapshot, _options())
    second = ContextBuilder(options=_options()).build(snapshot)

    assert first == second
    assert _dump(first) == _dump(second)
    assert tuple(file.path for file in first.files) == (
        "README.md",
        "src/alpha.py",
        "z-empty.txt",
    )
    assert first.files[-1].blocks[0].text == ""
    assert first.files[-1].source_line_count == 0
    expected_characters = sum(
        len(block.text) for file in first.files for block in file.blocks
    )
    assert first.statistics.included_character_count == expected_characters
    assert first.statistics.included_content_bytes > expected_characters
    assert first.statistics.languages == {"Markdown": 1, "Python": 1}
    assert first.statistics.item_count == 3
    assert first.statistics.total_source_bytes == sum(
        item.source_size_bytes for item in first.items
    )
    assert (
        first.statistics.total_content_bytes == first.statistics.included_content_bytes
    )
    assert first.statistics.character_count == expected_characters
    assert first.statistics.line_count == first.statistics.included_line_count
    assert _options().task_description == "Review the repository"


def test_identical_content_at_different_absolute_roots_builds_equal_packages(
    tmp_path: Path,
) -> None:
    contents = {"src/app.py": "print('same')\n", "README.md": "same\n"}
    first_snapshot = _write_files(tmp_path / "first", contents)
    second_snapshot = _write_files(tmp_path / "second", contents)

    first = build_context_package(first_snapshot, _options())
    second = build_context_package(second_snapshot, _options())

    assert first == second
    assert _dump(first) == _dump(second)
    serialized_shape = str(_dump(first))
    assert str(first_snapshot.root) not in serialized_shape
    assert str(second_snapshot.root) not in serialized_shape


def test_line_count_semantics_are_precise() -> None:
    assert canonical_line_count("") == 0
    assert canonical_line_count("one") == 1
    assert canonical_line_count("one\n") == 1
    assert canonical_line_count("one\ntwo") == 2
    assert canonical_line_count("\n") == 1


def test_models_are_shallowly_frozen_and_forbid_unknown_fields(
    tmp_path: Path,
) -> None:
    package = build_context_package(
        _write_files(tmp_path, {"file.txt": "text"}), _options()
    )

    with pytest.raises(ValidationError):
        package.title = "changed"
    with pytest.raises(ValidationError):
        package.files[0].path = "changed.txt"
    with pytest.raises(ValidationError):
        package.statistics.selected_file_count = 0
    with pytest.raises(ValidationError):
        ContextBuildOptions(unknown=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "changes",
    [
        {"max_files": 0},
        {"max_source_bytes_per_file": -1},
        {"max_total_content_bytes": 0},
        {"max_files": True},
    ],
)
def test_build_limits_must_be_positive_integers(changes: dict[str, object]) -> None:
    values: dict[str, object] = {"title": "Task"}
    values.update(changes)
    with pytest.raises(ValidationError):
        ContextBuildOptions.model_validate(values)


def test_package_rejects_duplicate_items_nonportable_paths_and_bad_statistics(
    tmp_path: Path,
) -> None:
    package = build_context_package(
        _write_files(tmp_path, {"a.txt": "a", "b.txt": "b"}), _options()
    )
    duplicate = _dump(package)
    duplicate["files"] = [duplicate["files"][0], duplicate["files"][0]]  # type: ignore[index]
    bad_path = _dump(package)
    bad_path["files"][0]["path"] = str(tmp_path / "a.txt")  # type: ignore[index]
    bad_statistics = _dump(package)
    bad_statistics["statistics"]["included_line_count"] = 99  # type: ignore[index]

    with pytest.raises(ValidationError, match="unique paths"):
        ContextPackage.model_validate(duplicate)
    with pytest.raises(ValidationError, match="portable relative path"):
        ContextPackage.model_validate(bad_path)
    with pytest.raises(ValidationError, match="statistics"):
        ContextPackage.model_validate(bad_statistics)


def test_block_and_file_invariants_reject_tampering(tmp_path: Path) -> None:
    package = build_context_package(
        _write_files(tmp_path, {"file.txt": "one\ntwo\n"}), _options()
    )
    bad_hash = _dump(package)
    bad_hash["files"][0]["blocks"][0]["sha256"] = "a" * 64  # type: ignore[index]
    bad_lines = _dump(package)
    bad_lines["files"][0]["blocks"][0]["line_count"] = 1  # type: ignore[index]

    with pytest.raises(ValidationError, match="sha256"):
        ContextPackage.model_validate(bad_hash)
    with pytest.raises(ValidationError, match="line_count"):
        ContextPackage.model_validate(bad_lines)

    text = "one\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ValidationError, match="null line bounds"):
        ContextFile(
            path="file.txt",
            source_size_bytes=4,
            source_sha256=digest,
            source_line_count=1,
            selection="full",
            blocks=(
                ContextBlock(
                    start_line=1,
                    end_line=1,
                    text=text,
                    line_count=1,
                    size_bytes=4,
                    sha256=digest,
                ),
            ),
            included_line_count=1,
            included_content_bytes=4,
        )


def test_block_validation_covers_every_semantic_invariant() -> None:
    valid = _block("one\n").model_dump()
    cases = (
        ({**valid, "start_line": 1}, "both be set"),
        ({**valid, "start_line": 2, "end_line": 1}, "before start_line"),
        (
            {**valid, "start_line": 1, "end_line": 2},
            "inclusive line range",
        ),
        (
            {
                **valid,
                "text": "one\r\n",
                "size_bytes": 5,
                "sha256": hashlib.sha256(b"one\r\n").hexdigest(),
            },
            "canonical LF",
        ),
        ({**valid, "size_bytes": 99}, "size_bytes"),
        ({**valid, "sha256": "a" * 64}, "sha256"),
        (
            {
                **valid,
                "start_line": 1,
                "end_line": 2,
                "line_count": 2,
            },
            "block text",
        ),
    )

    for payload, message in cases:
        with pytest.raises(ValidationError, match=message):
            ContextBlock.model_validate(payload)


def test_file_validation_covers_every_semantic_invariant() -> None:
    full = _block("one\n")
    ranged_one = _block("one\n", 1, 1)
    ranged_two = _block("two\n", 2, 2)
    source_hash = hashlib.sha256(b"one\ntwo\n").hexdigest()

    def make_file(**changes: object) -> ContextFile:
        values: dict[str, object] = {
            "path": "file.txt",
            "source_size_bytes": 8,
            "source_sha256": source_hash,
            "source_line_count": 1,
            "selection": "full",
            "blocks": (full,),
            "included_line_count": 1,
            "included_content_bytes": 4,
        }
        values.update(changes)
        return ContextFile(**values)  # type: ignore[arg-type]

    cases = (
        ({"blocks": ()}, "exactly one block"),
        ({"source_line_count": 2}, "source_line_count"),
        ({"selection": "ranges", "blocks": ()}, "at least one block"),
        ({"selection": "ranges"}, "inclusive line bounds"),
        (
            {
                "selection": "ranges",
                "blocks": (ranged_one, ranged_two),
                "included_line_count": 2,
                "included_content_bytes": 8,
            },
            "non-adjacent",
        ),
        (
            {
                "selection": "ranges",
                "blocks": (_block("three\n", 3, 3),),
                "included_line_count": 1,
                "included_content_bytes": 6,
            },
            "source_line_count",
        ),
        ({"included_line_count": 2}, "included_line_count"),
        ({"included_content_bytes": 99}, "included_content_bytes"),
    )
    for changes, message in cases:
        with pytest.raises(ValidationError, match=message):
            make_file(**changes)

    ranged = make_file(
        selection="ranges",
        blocks=(ranged_one,),
        included_line_count=1,
        included_content_bytes=4,
    )
    assert ranged.selection == "ranges"


@pytest.mark.parametrize(
    "path",
    [r"src\file.py", "/root.py", "safe/../file.py", "dir//file.py", "\x00bad.py"],
)
def test_package_paths_reject_every_nonportable_shape(path: str) -> None:
    with pytest.raises(ValidationError, match="portable relative path"):
        ContextFile(
            path=path,
            source_size_bytes=0,
            source_sha256=hashlib.sha256(b"").hexdigest(),
            source_line_count=0,
            selection="full",
            blocks=(_block(""),),
            included_line_count=0,
            included_content_bytes=0,
        )


def test_language_metadata_requires_canonical_safe_keys_and_consistent_counts() -> None:
    with pytest.raises(ValidationError, match="canonical key order"):
        ContextProject(
            selectable_file_count=2,
            selectable_directory_count=0,
            selectable_source_bytes=0,
            languages={"Python": 1, "Markdown": 1},
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        ContextProject(
            selectable_file_count=1,
            selectable_directory_count=0,
            selectable_source_bytes=0,
            languages={"": 1},
        )
    with pytest.raises(ValidationError, match="control characters"):
        ContextProject(
            selectable_file_count=1,
            selectable_directory_count=0,
            selectable_source_bytes=0,
            languages={"Python\nInjected": 1},
        )
    with pytest.raises(ValidationError, match="exceed selectable_file_count"):
        ContextProject(
            selectable_file_count=1,
            selectable_directory_count=0,
            selectable_source_bytes=0,
            languages={"Python": 2},
        )


def test_package_validation_covers_title_order_tree_and_project_invariants(
    tmp_path: Path,
) -> None:
    package = build_context_package(
        _write_files(tmp_path, {"src/a.txt": "a", "b.txt": "b"}), _options()
    )

    for title in ("", "bad\ncontrol", "bad\x7fcontrol"):
        payload = _dump(package)
        payload["title"] = title
        with pytest.raises(ValidationError, match="task description"):
            ContextPackage.model_validate(payload)

    unsorted = _dump(package)
    dumped_files = unsorted["files"]
    assert isinstance(dumped_files, list)
    unsorted["files"] = list(reversed(dumped_files))
    with pytest.raises(ValidationError, match="canonical order"):
        ContextPackage.model_validate(unsorted)

    absent = _dump(package)
    absent["files"][0]["path"] = "missing.txt"  # type: ignore[index]
    with pytest.raises(ValidationError, match="absent from the project tree"):
        ContextPackage.model_validate(absent)

    wrong_file_count = _dump(package)
    wrong_file_count["project"]["selectable_file_count"] = 3  # type: ignore[index]
    wrong_file_count["statistics"]["tree_file_count"] = 3  # type: ignore[index]
    with pytest.raises(ValidationError, match="tree file count"):
        ContextPackage.model_validate(wrong_file_count)

    wrong_directory_count = _dump(package)
    wrong_directory_count["project"]["selectable_directory_count"] = 2  # type: ignore[index]
    wrong_directory_count["statistics"]["tree_directory_count"] = 2  # type: ignore[index]
    with pytest.raises(ValidationError, match="tree directory count"):
        ContextPackage.model_validate(wrong_directory_count)


def test_builder_requires_a_source() -> None:
    with pytest.raises(ContextBuildError, match="repository root"):
        ContextBuilder().build()


def test_model_validation_does_not_mutate_input_package(tmp_path: Path) -> None:
    package = build_context_package(
        _write_files(tmp_path, {"file.txt": "text"}), _options()
    )
    payload = _dump(package)
    original = copy.deepcopy(payload)

    assert ContextPackage.model_validate(payload) == package
    assert payload == original
