from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.context import (
    MAX_LINE_NUMBER,
    ContextSelection,
    DuplicateSnapshotPathError,
    InvalidLineRangeError,
    InvalidSelectorError,
    LineRange,
    LineRangeRequest,
    LineRangeTargetError,
    NoFilesSelectedError,
    SelectionError,
    SelectionSelector,
    SelectorNoMatchError,
    canonicalize_line_ranges,
    parse_line_range_request,
    resolve_selection,
    select_files,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot, ScanSummary

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


def _paths(files: tuple[ProjectFile, ...]) -> tuple[str, ...]:
    return tuple(file.path for file in files)


def test_exact_root_and_nested_files_are_selected_and_explained(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, "pyproject.toml", "src/app.py", "src/model.py")

    result = resolve_selection(
        snapshot,
        ContextSelection(exact_paths=("./pyproject.toml", r"src\app.py")),
    )

    assert _paths(result.files) == ("pyproject.toml", "src/app.py")
    assert tuple(match.normalized_selector for match in result.include_matches) == (
        "pyproject.toml",
        "src/app.py",
    )
    assert tuple(_paths(match.files) for match in result.include_matches) == (
        ("pyproject.toml",),
        ("src/app.py",),
    )


def test_directory_selection_is_recursive_and_does_not_match_prefix_siblings(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        "src/context/app.py",
        "src/context/nested/model.py",
        "src/contextual/sibling.py",
        "src/other.py",
    )

    result = resolve_selection(
        snapshot,
        ContextSelection(directories=("src//context/",)),
    )

    assert _paths(result.files) == (
        "src/context/app.py",
        "src/context/nested/model.py",
    )
    assert result.include_matches[0].normalized_selector == "src/context"


def test_dot_directory_selects_the_entire_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "z", "nested/file", "a.py")

    result = resolve_selection(snapshot, ContextSelection(directories=(".\\",)))

    assert _paths(result.files) == ("a.py", "nested/file", "z")


def test_slashless_glob_matches_filenames_at_any_depth(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "app.py",
        "src/app.py",
        "src/app.txt",
        "src/nested/model.py",
    )

    result = resolve_selection(snapshot, ContextSelection(globs=("*.py",)))

    assert _paths(result.files) == (
        "app.py",
        "src/app.py",
        "src/nested/model.py",
    )


def test_rooted_recursive_glob_and_double_star_are_stable(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "src/top.py",
        "src/context/__init__.py",
        "src/context/nested/__init_extra.py",
        "tests/test_app.py",
    )

    source = resolve_selection(snapshot, ContextSelection(globs=("src/**/*.py",)))
    init_files = resolve_selection(
        snapshot, ContextSelection(globs=("**/**init**.py",))
    )

    assert _paths(source.files) == (
        "src/context/__init__.py",
        "src/context/nested/__init_extra.py",
        "src/top.py",
    )
    assert _paths(init_files.files) == (
        "src/context/__init__.py",
        "src/context/nested/__init_extra.py",
    )


def test_question_bracket_unicode_and_extensionless_globs(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "docs/file1.txt",
        "docs/filea.txt",
        "данные/пример1.py",
        "данные/примерx.py",
        "scripts/run",
    )

    result = resolve_selection(
        snapshot,
        ContextSelection(
            globs=("docs/file?.txt", "данные/пример[0-9].py", "scripts/*")
        ),
    )

    assert _paths(result.files) == (
        "docs/file1.txt",
        "docs/filea.txt",
        "scripts/run",
        "данные/пример1.py",
    )


def test_exact_selector_can_address_literal_glob_characters(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "notes/[draft]?.md", "notes/draft1.md")

    result = resolve_selection(
        snapshot, ContextSelection(exact_paths=("notes/[draft]?.md",))
    )

    assert _paths(result.files) == ("notes/[draft]?.md",)


def test_multiple_includes_are_unioned_deduplicated_and_sorted(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, "z.py", "src/b.py", "src/a.py", "README")

    result = resolve_selection(
        snapshot,
        ContextSelection(
            exact_paths=("src/b.py", "src/b.py"),
            directories=("src",),
            globs=("*.py",),
        ),
    )

    assert _paths(result.files) == ("src/a.py", "src/b.py", "z.py")
    assert len(result.include_matches) == 4


def test_multiple_exclusions_apply_after_all_includes(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "src/app.py",
        "src/generated.py",
        "tests/test_app.py",
        "README.md",
    )

    result = resolve_selection(
        snapshot,
        ContextSelection(
            exact_paths=("tests/test_app.py",),
            directories=("src",),
            exclusions=("**/generated.py", "tests/**"),
        ),
    )

    assert _paths(result.files) == ("src/app.py",)
    assert _paths(result.excluded_files) == (
        "src/generated.py",
        "tests/test_app.py",
    )
    assert tuple(_paths(match.files) for match in result.exclusion_matches) == (
        ("src/generated.py",),
        ("tests/test_app.py",),
    )


def test_exclusion_only_request_starts_with_all_snapshot_files(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py", "tests/test_app.py", "README")

    result = resolve_selection(
        snapshot, ContextSelection(exclusions=("tests/**", "missing/**"))
    )

    assert _paths(result.files) == ("README", "src/app.py")
    assert result.include_matches == ()
    assert result.unmatched_exclusions == ("missing/**",)


def test_exclusion_matching_unincluded_file_is_reported_but_not_removed(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py", "tests/test_app.py")

    result = resolve_selection(
        snapshot,
        ContextSelection(exact_paths=("src/app.py",), exclusions=("tests/**",)),
    )

    assert _paths(result.files) == ("src/app.py",)
    assert _paths(result.exclusion_matches[0].files) == ("tests/test_app.py",)
    assert result.excluded_files == ()


def test_exclusion_wins_and_final_empty_selection_is_an_error(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py")

    with pytest.raises(NoFilesSelectedError) as error:
        resolve_selection(
            snapshot,
            ContextSelection(exact_paths=("src/app.py",), exclusions=("src/app.py",)),
        )

    assert _paths(error.value.excluded_files) == ("src/app.py",)


@pytest.mark.parametrize(
    "selection",
    [
        ContextSelection(exact_paths=("missing.py",)),
        ContextSelection(directories=("missing",)),
        ContextSelection(globs=("missing/**/*.py",)),
    ],
)
def test_each_unmatched_include_is_an_error(
    tmp_path: Path, selection: ContextSelection
) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py")

    with pytest.raises(SelectorNoMatchError) as error:
        resolve_selection(snapshot, selection)

    assert error.value.selector.value.startswith("missing")
    assert "matched no snapshot file" in str(error.value)


def test_include_validation_happens_before_exclusion_precedence(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py")

    with pytest.raises(SelectorNoMatchError):
        resolve_selection(
            snapshot,
            ContextSelection(exact_paths=("missing.py",), exclusions=("missing.py",)),
        )


@pytest.mark.parametrize(
    "selector",
    [
        "",
        ".",
        "\x00bad.py",
        "\x1b[2Jbad.py",
        "../secret.py",
        "safe/../secret.py",
        "/etc/passwd",
        r"\rooted\file.py",
        r"\\server\share\file.py",
        r"C:\repo\file.py",
        "C:/repo/file.py",
        r"C:file.py",
    ],
)
def test_unsafe_exact_paths_are_rejected(tmp_path: Path, selector: str) -> None:
    snapshot = _snapshot(tmp_path, "safe/file.py")

    with pytest.raises(InvalidSelectorError) as error:
        resolve_selection(snapshot, ContextSelection(exact_paths=(selector,)))

    assert error.value.selector == selector
    assert error.value.selector_type == "exact_path"


@pytest.mark.parametrize(
    "selector",
    [
        "",
        "bad\x00*.py",
        "bad\x1b[2J*.py",
        "../*.py",
        "safe/../*.py",
        "/root/*.py",
        r"\rooted\*.py",
        r"\\server\share\*.py",
        r"C:\repo\*.py",
        r"C:*.py",
        "src//*.py",
        "src/./*.py",
        "src/",
        "!*.py",
        "# ignored by gitwildmatch",
        "[z-a]",
    ],
)
def test_malformed_or_unsafe_globs_are_rejected(tmp_path: Path, selector: str) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py")

    with pytest.raises(InvalidSelectorError):
        resolve_selection(snapshot, ContextSelection(globs=(selector,)))


@pytest.mark.parametrize(
    "selector",
    [
        "",
        "../*.py",
        "/root/*",
        r"\\server\share\*",
        r"C:*.py",
        "!*.py",
        "bad\x1b[2J*",
    ],
)
def test_unsafe_exclusions_are_rejected(tmp_path: Path, selector: str) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py")

    with pytest.raises(InvalidSelectorError) as error:
        resolve_selection(snapshot, ContextSelection(exclusions=(selector,)))

    assert error.value.selector_type == "exclusion"


def test_case_sensitive_matching_is_platform_independent(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "src/App.py", "src/app.py")

    exact = resolve_selection(snapshot, ContextSelection(exact_paths=("src/App.py",)))
    glob = resolve_selection(snapshot, ContextSelection(globs=("**/A*.py",)))

    assert _paths(exact.files) == ("src/App.py",)
    assert _paths(glob.files) == ("src/App.py",)


def test_public_select_files_accepts_explicit_selector_kinds(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "README", "src/app.py", "src/model.py")
    selectors = (
        SelectionSelector(kind="exact_path", value="README"),
        SelectionSelector(kind="directory", value="src"),
        SelectionSelector(kind="glob", value="*.py"),
    )

    result = select_files(snapshot, selectors, ("**/model.py",))

    assert _paths(result.files) == ("README", "src/app.py")


def test_selection_does_not_access_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path, "src/app.py", "tests/test_app.py")

    def reject_access(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"unexpected filesystem access: {args!r} {kwargs!r}")

    for method in ("open", "iterdir", "glob", "rglob", "exists", "is_file", "stat"):
        monkeypatch.setattr(Path, method, reject_access)

    result = resolve_selection(
        snapshot,
        ContextSelection(directories=("src",), exclusions=("tests/**",)),
    )

    assert _paths(result.files) == ("src/app.py",)


def test_duplicate_snapshot_paths_are_rejected(tmp_path: Path) -> None:
    duplicate = _file("same.py")
    snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(duplicate, duplicate),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=2, ignored_count=0, total_size_bytes=0),
    )

    with pytest.raises(DuplicateSnapshotPathError, match="same.py") as error:
        resolve_selection(snapshot, ContextSelection())

    assert error.value.path == "same.py"


@pytest.mark.parametrize(
    "path", ["", "bad\x00.py", "src//app.py", r"src\app.py", "/root.py"]
)
def test_malformed_forged_snapshot_paths_are_rejected(
    tmp_path: Path, path: str
) -> None:
    snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(_forged_file(path),),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=1, ignored_count=0, total_size_bytes=0),
    )

    with pytest.raises(SelectionError, match="snapshot path is not portable"):
        resolve_selection(snapshot, ContextSelection())


def test_non_project_file_and_non_string_snapshot_path_are_rejected(
    tmp_path: Path,
) -> None:
    invalid_entry = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=("file.py",),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=1, ignored_count=0, total_size_bytes=0),
    )
    invalid_path = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(_forged_file(42),),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=1, ignored_count=0, total_size_bytes=0),
    )

    with pytest.raises(SelectionError, match="ProjectFile"):
        resolve_selection(invalid_entry, ContextSelection())
    with pytest.raises(SelectionError, match="ProjectFile"):
        resolve_selection(invalid_path, ContextSelection())


def test_empty_snapshot_cannot_produce_a_selection(tmp_path: Path) -> None:
    with pytest.raises(NoFilesSelectedError):
        resolve_selection(_snapshot(tmp_path), ContextSelection())


def test_runtime_invalid_include_and_exclusion_types_fail_closed(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, "app.py")
    forged = SelectionSelector.model_construct(kind="exact_path", value=None)

    with pytest.raises(InvalidSelectorError):
        select_files(snapshot, (object(),))  # type: ignore[arg-type]
    with pytest.raises(InvalidSelectorError):
        select_files(snapshot, (forged,))
    with pytest.raises(InvalidSelectorError):
        select_files(snapshot, (), (None,))  # type: ignore[arg-type]


def test_selection_models_are_frozen_and_forbid_unknown_fields(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "app.py")
    selection = ContextSelection(exact_paths=("app.py",))
    result = resolve_selection(snapshot, selection)

    with pytest.raises(ValidationError):
        selection.exact_paths = ()
    with pytest.raises(ValidationError):
        result.files = ()
    with pytest.raises(ValidationError):
        SelectionSelector(kind="glob", value="*.py", unknown=True)  # type: ignore[call-arg]


def test_line_range_is_one_based_inclusive_and_frozen() -> None:
    line_range = LineRange(start=10, end=50)

    assert line_range.start == 10
    assert line_range.end == 50
    with pytest.raises(AttributeError):
        line_range.start = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (-1, 1), (2, 1), (1, MAX_LINE_NUMBER + 1), (True, 1), (1, 1.5)],
)
def test_invalid_line_range_bounds_raise_typed_error(
    start: object, end: object
) -> None:
    with pytest.raises(InvalidLineRangeError):
        LineRange(start=start, end=end)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "file.py:1",
        "file.py:1-",
        "file.py:-2",
        "file.py:20-end",
        "file.py:0-1",
        "file.py:2-1",
        "file.py:+1-2",
        "file.py:1.0-2",
        f"file.py:1-{MAX_LINE_NUMBER + 1}",
        "\x00file.py:1-2",
        None,
    ],
)
def test_line_range_parser_rejects_open_ended_and_malformed_values(
    value: object,
) -> None:
    with pytest.raises(InvalidLineRangeError):
        parse_line_range_request(value)


def test_line_range_parser_uses_final_numeric_colon_suffix() -> None:
    request = parse_line_range_request("data:legacy.txt:3-9")

    assert request == LineRangeRequest(
        path="data:legacy.txt", range=LineRange(start=3, end=9)
    )


def test_duplicate_overlapping_nested_and_adjacent_ranges_are_canonical() -> None:
    ranges = (
        LineRange(10, 12),
        LineRange(1, 3),
        LineRange(3, 5),
        LineRange(2, 4),
        LineRange(6, 8),
        LineRange(10, 12),
    )

    assert canonicalize_line_ranges(ranges) == (
        LineRange(1, 8),
        LineRange(10, 12),
    )
    with pytest.raises(InvalidLineRangeError):
        canonicalize_line_ranges((object(),))  # type: ignore[arg-type]


def test_resolved_ranges_are_normalized_merged_and_path_sorted(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "z.py", "src/app.py")
    selection = ContextSelection(
        directories=(".",),
        line_ranges=(
            LineRangeRequest("z.py", LineRange(4, 5)),
            LineRangeRequest(r"src\app.py", LineRange(2, 3)),
            LineRangeRequest("src/app.py", LineRange(1, 1)),
        ),
    )

    result = resolve_selection(snapshot, selection)

    assert result.line_ranges == (
        LineRangeRequest("src/app.py", LineRange(1, 3)),
        LineRangeRequest("z.py", LineRange(4, 5)),
    )


def test_range_target_must_exist_and_remain_selected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "app.py", "excluded.py")

    with pytest.raises(LineRangeTargetError, match="absent"):
        resolve_selection(
            snapshot,
            ContextSelection(
                line_ranges=(LineRangeRequest("missing.py", LineRange(1, 1)),)
            ),
        )
    with pytest.raises(LineRangeTargetError, match="not selected"):
        resolve_selection(
            snapshot,
            ContextSelection(
                exclusions=("excluded.py",),
                line_ranges=(LineRangeRequest("excluded.py", LineRange(1, 1)),),
            ),
        )


def test_range_target_path_uses_strict_portable_validation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "safe.py")

    with pytest.raises(InvalidLineRangeError, match="portable"):
        resolve_selection(
            snapshot,
            ContextSelection(
                line_ranges=(LineRangeRequest("safe/../safe.py", LineRange(1, 1)),)
            ),
        )


def test_line_range_request_runtime_types_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(InvalidLineRangeError, match="requires a path"):
        LineRangeRequest(path=1, range=LineRange(1, 1))  # type: ignore[arg-type]

    snapshot = _snapshot(tmp_path, "safe.py")
    forged = ContextSelection.model_construct(
        exact_paths=(),
        directories=(),
        globs=(),
        exclusions=(),
        line_ranges=(object(),),
    )
    with pytest.raises(InvalidLineRangeError, match="LineRangeRequest"):
        resolve_selection(snapshot, forged)
