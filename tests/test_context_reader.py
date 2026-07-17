import errno
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest

import contextforge.context.reader as reader_module
import contextforge.filesystem as filesystem_module
from contextforge.context import (
    InvalidLineRangeError,
    LineRange,
    LineRangeBoundsError,
    LineRangeRequest,
    ReaderLimits,
    SelectedFileChangedError,
    SelectedFileDecodeError,
    SelectedFileMissingError,
    SelectedFileNotInSnapshotError,
    SelectedFileNotRegularError,
    SelectedFileOutsideRootError,
    SelectedFileReadError,
    SelectedFileTooLargeError,
    read_selected_text_file,
    read_selected_text_files,
)
from contextforge.filesystem import (
    FileNotRegularError,
    FileTooLargeError,
)
from contextforge.repositories import (
    ProjectFile,
    ProjectSnapshot,
    ScanOptions,
    ScanSummary,
    scan_repository,
)


def _project_file(
    path: str, content: bytes, *, digest: str | None = None
) -> ProjectFile:
    return ProjectFile(
        path=path,
        size_bytes=len(content),
        sha256=digest or hashlib.sha256(content).hexdigest(),
        is_text=True,
    )


def _snapshot(tmp_path: Path, project_file: ProjectFile) -> ProjectSnapshot:
    return ProjectSnapshot(
        root=tmp_path,
        files=(project_file,),
        summary=ScanSummary(
            file_count=1,
            ignored_count=0,
            total_size_bytes=project_file.size_bytes,
        ),
    )


def _write_snapshot(
    tmp_path: Path, content: bytes, *, path: str = "source.txt"
) -> tuple[ProjectSnapshot, ProjectFile, Path]:
    candidate = tmp_path.joinpath(*path.split("/"))
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(content)
    project_file = _project_file(path, content)
    return _snapshot(tmp_path, project_file), project_file, candidate


def _create_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


class _GenericErrnoError(OSError):
    """OSError that retains its base type for errno mapping coverage."""


@pytest.mark.parametrize(
    ("content", "expected", "line_count"),
    [
        (b"", "", 0),
        (b"plain utf-8", "plain utf-8", 1),
        ("Привет, 世界\n".encode(), "Привет, 世界\n", 1),
        (b"\xef\xbb\xbfwith BOM", "with BOM", 1),
        ("start\ufeffinside".encode(), "start\ufeffinside", 1),
        (b"a\nb\n", "a\nb\n", 2),
        (b"a\r\nb\r\n", "a\nb\n", 2),
        (b"a\rb\r", "a\nb\n", 2),
        (b"a\r\nb\rc\n", "a\nb\nc\n", 3),
        (b"a\nb", "a\nb", 2),
    ],
)
def test_verified_utf8_is_bom_free_and_lf_canonical(
    tmp_path: Path, content: bytes, expected: str, line_count: int
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, content)

    result = read_selected_text_file(snapshot, project_file)

    assert result.project_file is project_file
    assert result.source_line_count == line_count
    assert result.blocks[0].line_range is None
    assert result.blocks[0].text == expected
    assert result.blocks[0].size_bytes == len(expected.encode())
    assert result.blocks[0].sha256 == hashlib.sha256(expected.encode()).hexdigest()


def test_reader_result_is_deterministic(tmp_path: Path) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, "α\r\nβ".encode())

    assert read_selected_text_file(snapshot, project_file) == read_selected_text_file(
        snapshot, project_file
    )


@pytest.mark.parametrize(
    ("line_range", "expected"),
    [
        (LineRange(1, 1), "one\n"),
        (LineRange(2, 4), "two\nthree\nfour"),
        (LineRange(1, 4), "one\ntwo\nthree\nfour"),
    ],
)
def test_inclusive_one_and_multi_line_ranges(
    tmp_path: Path, line_range: LineRange, expected: str
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"one\ntwo\nthree\nfour")

    result = read_selected_text_file(snapshot, project_file, line_ranges=(line_range,))

    assert result.blocks[0].line_range == line_range
    assert result.blocks[0].text == expected
    assert result.blocks[0].line_count == line_range.end - line_range.start + 1


def test_ranges_preserve_source_final_newline_status(tmp_path: Path) -> None:
    with_newline = _write_snapshot(tmp_path / "with", b"a\nb\n")
    without_newline = _write_snapshot(tmp_path / "without", b"a\nb")

    first = read_selected_text_file(
        with_newline[0], with_newline[1], line_ranges=(LineRange(2, 2),)
    )
    second = read_selected_text_file(
        without_newline[0], without_newline[1], line_ranges=(LineRange(2, 2),)
    )

    assert first.source_line_count == second.source_line_count == 2
    assert first.blocks[0].text == "b\n"
    assert second.blocks[0].text == "b"


def test_direct_duplicate_overlapping_and_adjacent_ranges_merge(tmp_path: Path) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"1\n2\n3\n4\n5\n")

    result = read_selected_text_file(
        snapshot,
        project_file,
        line_ranges=(
            LineRange(3, 4),
            LineRange(1, 2),
            LineRange(2, 3),
            LineRange(1, 2),
        ),
    )

    assert tuple(block.line_range for block in result.blocks) == (LineRange(1, 4),)
    assert result.blocks[0].text == "1\n2\n3\n4\n"


def test_disjoint_ranges_remain_separate(tmp_path: Path) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"1\n2\n3\n4\n")

    result = read_selected_text_file(
        snapshot,
        project_file,
        line_ranges=(LineRange(1, 1), LineRange(3, 4)),
    )

    assert tuple(block.text for block in result.blocks) == ("1\n", "3\n4\n")
    assert result.included_line_count == 3


def test_range_beyond_eof_and_empty_file_ranges_fail(tmp_path: Path) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path / "short", b"one\ntwo")
    empty_snapshot, empty_file, _ = _write_snapshot(tmp_path / "empty", b"")

    with pytest.raises(LineRangeBoundsError) as beyond:
        read_selected_text_file(snapshot, project_file, line_ranges=(LineRange(2, 3),))
    with pytest.raises(LineRangeBoundsError) as empty:
        read_selected_text_file(
            empty_snapshot, empty_file, line_ranges=(LineRange(1, 1),)
        )

    assert beyond.value.line_count == 2
    assert empty.value.line_count == 0


def test_only_the_snapshot_owned_project_file_instance_is_accepted(
    tmp_path: Path,
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"same")
    equal_but_external = project_file.model_copy()

    with pytest.raises(SelectedFileNotInSnapshotError, match="originate"):
        read_selected_text_file(snapshot, equal_but_external)
    with pytest.raises(SelectedFileNotInSnapshotError, match="ProjectSnapshot"):
        read_selected_text_file(object(), project_file)  # type: ignore[arg-type]


def test_duplicate_and_non_text_snapshot_entries_fail_closed(tmp_path: Path) -> None:
    project_file = _project_file("file.txt", b"")
    duplicate_snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(project_file, project_file),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=2, ignored_count=0, total_size_bytes=0),
    )
    non_text = project_file.model_copy(update={"is_text": False})
    non_text_snapshot = _snapshot(tmp_path, non_text)

    with pytest.raises(SelectedFileNotInSnapshotError, match="unique"):
        read_selected_text_file(duplicate_snapshot, project_file)
    with pytest.raises(SelectedFileNotInSnapshotError, match="selectable text"):
        read_selected_text_file(non_text_snapshot, non_text)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "safe/../secret.txt",
        "/rooted.txt",
        r"\rooted.txt",
        r"\\server\share\file.txt",
        r"C:\repo\file.txt",
        "C:/repo/file.txt",
        r"C:file.txt",
        "bad\x00.txt",
        "bad\x1b[2J.txt",
        "dir//file.txt",
        "./file.txt",
    ],
)
def test_forged_traversal_absolute_and_drive_paths_are_rejected(
    tmp_path: Path, path: str
) -> None:
    project_file = ProjectFile.model_construct(
        path=path,
        size_bytes=0,
        language=None,
        sha256=hashlib.sha256(b"").hexdigest(),
        is_text=True,
    )
    snapshot = ProjectSnapshot.model_construct(
        root=tmp_path,
        files=(project_file,),
        ignored_files=(),
        skipped_files=(),
        summary=ScanSummary(file_count=1, ignored_count=0, total_size_bytes=0),
    )

    with pytest.raises(SelectedFileOutsideRootError, match="not portable"):
        read_selected_text_file(snapshot, project_file)


def test_unavailable_or_non_directory_snapshot_root_is_rejected(tmp_path: Path) -> None:
    project_file = _project_file("file.txt", b"")
    missing = _snapshot(tmp_path / "missing", project_file)
    root_file = tmp_path / "root.txt"
    root_file.write_text("root", encoding="utf-8")
    wrong_type = _snapshot(root_file, project_file)

    with pytest.raises(SelectedFileOutsideRootError, match="unavailable"):
        read_selected_text_file(missing, project_file)
    with pytest.raises(SelectedFileOutsideRootError, match="not a directory"):
        read_selected_text_file(wrong_type, project_file)


def test_relative_and_junction_snapshot_roots_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_file = _project_file("file.txt", b"")
    relative_snapshot = _snapshot(Path("relative-root"), project_file)

    with pytest.raises(SelectedFileOutsideRootError, match="not absolute"):
        read_selected_text_file(relative_snapshot, project_file)

    candidate = tmp_path / "file.txt"
    candidate.write_bytes(b"")
    junction_snapshot = _snapshot(tmp_path, project_file)
    original_junction = Path.is_junction

    def report_root_junction(path: Path) -> bool:
        return path == tmp_path or original_junction(path)

    monkeypatch.setattr(Path, "is_junction", report_root_junction)
    with pytest.raises(SelectedFileOutsideRootError, match="not a directory"):
        read_selected_text_file(junction_snapshot, project_file)


def test_file_deleted_after_scan_is_typed_missing(tmp_path: Path) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"content")
    candidate.unlink()

    with pytest.raises(SelectedFileMissingError, match="source.txt"):
        read_selected_text_file(snapshot, project_file)


@pytest.mark.parametrize("replacement", [b"different size", b"changed"])
def test_file_modified_or_replaced_after_scan_is_changed(
    tmp_path: Path, replacement: bytes
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"original")
    candidate.unlink()
    candidate.write_bytes(replacement)

    with pytest.raises(SelectedFileChangedError, match="source.txt"):
        read_selected_text_file(snapshot, project_file)


def test_snapshot_hash_mismatch_is_rejected_after_bounded_read(tmp_path: Path) -> None:
    content = b"same-size"
    candidate = tmp_path / "source.txt"
    candidate.write_bytes(content)
    project_file = _project_file("source.txt", content, digest="a" * 64)
    snapshot = _snapshot(tmp_path, project_file)

    with pytest.raises(SelectedFileChangedError, match="hash"):
        read_selected_text_file(snapshot, project_file)


def test_replacement_between_metadata_and_open_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"original")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"replaced")
    original_open = filesystem_module._open_binary

    def replace_before_open(path: Path) -> BinaryIO:
        if path == candidate:
            candidate.unlink()
            replacement.replace(candidate)
        return original_open(path)

    monkeypatch.setattr(filesystem_module, "_open_binary", replace_before_open)

    with pytest.raises(SelectedFileChangedError):
        read_selected_text_file(snapshot, project_file)


class _MutatingReader:
    def __init__(
        self,
        file: BinaryIO,
        mutation: Callable[[], None],
        *,
        trigger_read: int,
        mutate_after_read: bool = False,
        requested_sizes: list[int] | None = None,
    ) -> None:
        self.file = file
        self.mutation = mutation
        self.trigger_read = trigger_read
        self.mutate_after_read = mutate_after_read
        self.requested_sizes = requested_sizes
        self.read_count = 0

    def __enter__(self) -> "_MutatingReader":
        return self

    def __exit__(self, *args: object) -> None:
        self.file.close()

    def fileno(self) -> int:
        return self.file.fileno()

    def read(self, size: int) -> bytes:
        self.read_count += 1
        if self.requested_sizes is not None:
            self.requested_sizes.append(size)
        if self.read_count == self.trigger_read and not self.mutate_after_read:
            self.mutation()
        chunk = self.file.read(size)
        if self.read_count == self.trigger_read and self.mutate_after_read:
            self.mutation()
        return chunk


@pytest.mark.parametrize("trigger_read", [1, 2])
def test_growth_during_first_or_later_chunk_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger_read: int
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"1234")
    original_open = filesystem_module._open_binary

    def grow() -> None:
        with candidate.open("ab") as writer:
            writer.write(b"5")

    def open_growing(path: Path) -> _MutatingReader:
        return _MutatingReader(original_open(path), grow, trigger_read=trigger_read)

    monkeypatch.setattr(filesystem_module, "_open_binary", open_growing)

    with pytest.raises(SelectedFileChangedError):
        read_selected_text_file(snapshot, project_file, chunk_size=2)


def test_growth_after_eof_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"1234")
    original_open = filesystem_module._open_binary

    def grow() -> None:
        with candidate.open("ab") as writer:
            writer.write(b"5")

    def open_growing(path: Path) -> _MutatingReader:
        return _MutatingReader(
            original_open(path), grow, trigger_read=3, mutate_after_read=True
        )

    monkeypatch.setattr(filesystem_module, "_open_binary", open_growing)

    with pytest.raises(SelectedFileChangedError):
        read_selected_text_file(snapshot, project_file, chunk_size=2)


def test_shrink_during_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"1234")
    original_open = filesystem_module._open_binary

    def shrink() -> None:
        with candidate.open("r+b") as writer:
            writer.truncate(2)

    def open_shrinking(path: Path) -> _MutatingReader:
        return _MutatingReader(original_open(path), shrink, trigger_read=2)

    monkeypatch.setattr(filesystem_module, "_open_binary", open_shrinking)

    with pytest.raises(SelectedFileChangedError):
        read_selected_text_file(snapshot, project_file, chunk_size=2)


def test_path_replacement_after_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"1234")
    original_stat = Path.stat
    non_following_stats = 0

    def report_replacement(
        path: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal non_following_stats
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != candidate or follow_symlinks:
            return result
        non_following_stats += 1
        if non_following_stats < 3:
            return result
        values = list(result)
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", report_replacement)

    with pytest.raises(SelectedFileChangedError):
        read_selected_text_file(snapshot, project_file, chunk_size=2)


def test_reads_are_bounded_by_chunk_and_expected_size_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"12345")
    original_open = filesystem_module._open_binary
    requested_sizes: list[int] = []

    def no_mutation() -> None:
        return None

    def open_counting(path: Path) -> _MutatingReader:
        return _MutatingReader(
            original_open(path),
            no_mutation,
            trigger_read=-1,
            requested_sizes=requested_sizes,
        )

    monkeypatch.setattr(filesystem_module, "_open_binary", open_counting)

    read_selected_text_file(snapshot, project_file, chunk_size=2)

    assert requested_sizes == [2, 2, 2, 1]


@pytest.mark.parametrize("invalid", [b"\xff", b"valid\xff", b"valid\n\xff"])
def test_invalid_utf8_never_uses_replacement_or_fallback(
    tmp_path: Path, invalid: bytes
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, invalid)

    with pytest.raises(SelectedFileDecodeError, match="valid UTF-8"):
        read_selected_text_file(snapshot, project_file)


def test_invalid_utf8_after_scanner_sample_is_rejected(tmp_path: Path) -> None:
    content = b"a" * 8192 + b"\xff"
    candidate = tmp_path / "late-invalid.txt"
    candidate.write_bytes(content)
    snapshot = scan_repository(tmp_path, ScanOptions(max_file_size_bytes=len(content)))

    assert snapshot.files[0].path == "late-invalid.txt"
    with pytest.raises(SelectedFileDecodeError):
        read_selected_text_file(
            snapshot,
            snapshot.files[0],
            limits=ReaderLimits(max_source_bytes=len(content)),
        )


def test_symlink_and_non_regular_file_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"target")
    link = tmp_path / "link.txt"
    _create_symlink(link, target)
    link_file = _project_file("link.txt", b"target")
    directory = tmp_path / "directory"
    directory.mkdir()
    directory_file = _project_file("directory", b"")

    with pytest.raises(SelectedFileNotRegularError):
        read_selected_text_file(_snapshot(tmp_path, link_file), link_file)
    with pytest.raises(SelectedFileNotRegularError):
        read_selected_text_file(_snapshot(tmp_path, directory_file), directory_file)


def test_junction_and_special_file_branches_are_portably_simulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, candidate = _write_snapshot(
        tmp_path, b"text", path="nested/file.txt"
    )
    junction = tmp_path / "nested"
    original_junction = Path.is_junction

    def report_junction(path: Path) -> bool:
        return path == junction or original_junction(path)

    monkeypatch.setattr(Path, "is_junction", report_junction)
    with pytest.raises(SelectedFileNotRegularError):
        read_selected_text_file(snapshot, project_file)

    monkeypatch.setattr(Path, "is_junction", original_junction)
    original_stat = Path.stat

    def report_fifo(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != candidate or follow_symlinks:
            return result
        values = list(result)
        values[0] = stat.S_IFIFO
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", report_fifo)
    with pytest.raises(SelectedFileNotRegularError):
        read_selected_text_file(snapshot, project_file)


def test_operational_open_error_is_path_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"text")

    def deny_open(path: Path) -> BinaryIO:
        raise PermissionError(f"denied: {path.name}")

    monkeypatch.setattr(filesystem_module, "_open_binary", deny_open)

    with pytest.raises(SelectedFileReadError, match="source.txt"):
        read_selected_text_file(snapshot, project_file)


def test_source_and_content_limits_accept_equality_and_reject_one_over(
    tmp_path: Path,
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, "é".encode())

    exact = read_selected_text_file(
        snapshot,
        project_file,
        limits=ReaderLimits(max_source_bytes=2, max_content_bytes=2),
    )

    assert exact.included_content_bytes == 2
    with pytest.raises(SelectedFileTooLargeError, match="source bytes"):
        read_selected_text_file(
            snapshot,
            project_file,
            limits=ReaderLimits(max_source_bytes=1),
        )
    with pytest.raises(SelectedFileTooLargeError, match="selected content"):
        read_selected_text_file(
            snapshot,
            project_file,
            limits=ReaderLimits(max_content_bytes=1),
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_files": 0},
        {"max_source_bytes": 0},
        {"max_content_bytes": -1},
        {"max_files": True},
    ],
)
def test_reader_limits_require_positive_integers(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ReaderLimits(**arguments)  # type: ignore[arg-type]


def test_batch_read_is_sorted_ranged_and_enforces_total_limits(tmp_path: Path) -> None:
    first_path = tmp_path / "b.txt"
    second_path = tmp_path / "a.txt"
    first_path.write_bytes(b"bb")
    second_path.write_bytes(b"aa")
    first = _project_file("b.txt", b"bb")
    second = _project_file("a.txt", b"aa")
    snapshot = ProjectSnapshot(
        root=tmp_path,
        files=(first, second),
        summary=ScanSummary(file_count=2, ignored_count=0, total_size_bytes=4),
    )

    result = read_selected_text_files(
        snapshot,
        (first, second),
        line_ranges=(LineRangeRequest("a.txt", LineRange(1, 1)),),
        limits=ReaderLimits(max_content_bytes=4),
    )

    assert tuple(item.project_file.path for item in result) == ("a.txt", "b.txt")
    assert result[0].blocks[0].line_range == LineRange(1, 1)
    with pytest.raises(SelectedFileTooLargeError, match="selected content"):
        read_selected_text_files(
            snapshot,
            (first, second),
            limits=ReaderLimits(max_content_bytes=3),
        )
    with pytest.raises(SelectedFileTooLargeError, match="selection has"):
        read_selected_text_files(
            snapshot,
            (first, second),
            limits=ReaderLimits(max_files=1),
        )


def test_batch_rejects_duplicate_and_unselected_range_targets(tmp_path: Path) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"text")

    with pytest.raises(SelectedFileNotInSnapshotError, match="unique"):
        read_selected_text_files(snapshot, (project_file, project_file))
    with pytest.raises(SelectedFileNotInSnapshotError, match="ProjectFile"):
        read_selected_text_files(snapshot, (object(),))  # type: ignore[arg-type]
    with pytest.raises(InvalidLineRangeError, match="not in the selected"):
        read_selected_text_files(
            snapshot,
            (project_file,),
            line_ranges=(LineRangeRequest("other.txt", LineRange(1, 1)),),
        )
    with pytest.raises(InvalidLineRangeError, match="LineRangeRequest"):
        read_selected_text_files(
            snapshot,
            (project_file,),
            line_ranges=(object(),),  # type: ignore[arg-type]
        )


def test_batch_has_no_partial_success_when_later_file_fails(tmp_path: Path) -> None:
    good_path = tmp_path / "a.txt"
    bad_path = tmp_path / "b.txt"
    good_path.write_bytes(b"good")
    bad_path.write_bytes(b"bad\xff")
    good = _project_file("a.txt", b"good")
    bad = _project_file("b.txt", b"bad\xff")
    snapshot = ProjectSnapshot(
        root=tmp_path,
        files=(good, bad),
        summary=ScanSummary(file_count=2, ignored_count=0, total_size_bytes=8),
    )

    with pytest.raises(SelectedFileDecodeError):
        read_selected_text_files(snapshot, (good, bad))


def test_batch_empty_selection_is_deterministic(tmp_path: Path) -> None:
    snapshot = ProjectSnapshot(
        root=tmp_path,
        summary=ScanSummary(file_count=0, ignored_count=0, total_size_bytes=0),
    )

    assert read_selected_text_files(snapshot, ()) == ()


def test_chunk_size_validation_is_preserved(tmp_path: Path) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"text")

    with pytest.raises(ValueError, match="chunk_size"):
        read_selected_text_file(snapshot, project_file, chunk_size=0)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FileNotFoundError(), SelectedFileMissingError),
        (FileNotRegularError(), SelectedFileNotRegularError),
        (FileTooLargeError(2, 1), SelectedFileTooLargeError),
        (_GenericErrnoError(errno.ENOENT, "gone"), SelectedFileMissingError),
        (OSError(errno.ELOOP, "link loop"), SelectedFileNotRegularError),
    ],
)
def test_races_inside_stable_read_map_to_typed_reader_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: type[Exception],
) -> None:
    snapshot, project_file, _ = _write_snapshot(tmp_path, b"text")

    def fail_read(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(reader_module, "read_file_stably", fail_read)

    with pytest.raises(expected):
        read_selected_text_file(snapshot, project_file)


def test_candidate_inspection_and_resolution_errors_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"text")
    original_stat = Path.stat

    def deny_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == candidate and not follow_symlinks:
            raise PermissionError("denied")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", deny_stat)
    with pytest.raises(SelectedFileReadError, match="inspect"):
        read_selected_text_file(snapshot, project_file)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FileNotFoundError(), SelectedFileMissingError),
        (ValueError(), SelectedFileOutsideRootError),
        (PermissionError(), SelectedFileReadError),
    ],
)
def test_candidate_resolution_races_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: type[Exception],
) -> None:
    snapshot, project_file, candidate = _write_snapshot(tmp_path, b"text")
    original_resolve = Path.resolve

    def fail_candidate_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == candidate:
            raise failure
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_candidate_resolve)

    with pytest.raises(expected):
        read_selected_text_file(snapshot, project_file)
