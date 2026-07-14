import os
import stat
from builtins import open as builtin_open
from pathlib import Path
from typing import BinaryIO, cast

import pytest

import contextforge.filesystem as filesystem_module
from contextforge.filesystem import (
    FileIdentityChangedError,
    FileNotRegularError,
    FileSizeChangedError,
    FileTooLargeError,
    read_file_stably,
)


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({"max_size_bytes": 0}, ValueError, "max_size_bytes"),
        (
            {"max_size_bytes": 1, "expected_size_bytes": -1},
            ValueError,
            "expected_size_bytes",
        ),
        (
            {"max_size_bytes": 1, "expected_size_bytes": 2},
            FileTooLargeError,
            "exceeds limit",
        ),
        ({"max_size_bytes": 1, "chunk_size": 0}, ValueError, "chunk_size"),
        (
            {"max_size_bytes": 1, "initial_chunk_size": 0},
            ValueError,
            "initial_chunk_size",
        ),
        (
            {"max_size_bytes": 1, "stop_after_initial": lambda _: True},
            ValueError,
            "requires initial_chunk_size",
        ),
    ],
)
def test_stable_read_validates_all_options(
    tmp_path: Path,
    arguments: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"x")

    with pytest.raises(error, match=message):
        read_file_stably(path, **arguments)  # type: ignore[arg-type]


def test_stable_read_can_capture_initial_and_complete_content(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"abcdef")

    result = read_file_stably(
        path,
        max_size_bytes=6,
        initial_chunk_size=2,
        stop_after_initial=lambda _: False,
        chunk_size=2,
    )

    assert result.content == b"abcdef"
    assert result.complete is True


def test_stable_read_can_stop_after_a_captured_initial_sample(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"abcdef")

    result = read_file_stably(
        path,
        max_size_bytes=6,
        initial_chunk_size=2,
        stop_after_initial=lambda _: True,
    )

    assert result.content == b"ab"
    assert result.size_bytes == 6
    assert result.complete is False


def test_expected_size_mismatch_is_detected_before_open(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"abc")

    with pytest.raises(FileSizeChangedError) as error:
        read_file_stably(path, max_size_bytes=4, expected_size_bytes=4)

    assert error.value.expected_size == 4
    assert error.value.actual_size == 3


class _EarlyEofReader:
    def __init__(self, file: BinaryIO) -> None:
        self.file = file
        self.read_count = 0

    def __enter__(self) -> "_EarlyEofReader":
        return self

    def __exit__(self, *args: object) -> None:
        self.file.close()

    def fileno(self) -> int:
        return self.file.fileno()

    def read(self, size: int) -> bytes:
        self.read_count += 1
        if self.read_count == 2:
            return b""
        return self.file.read(size)


def test_premature_eof_without_size_change_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"abcd")
    original_open = filesystem_module._open_binary

    def open_early_eof(candidate: Path) -> _EarlyEofReader:
        return _EarlyEofReader(original_open(candidate))

    monkeypatch.setattr(filesystem_module, "_open_binary", open_early_eof)

    with pytest.raises(FileSizeChangedError) as error:
        read_file_stably(path, max_size_bytes=4, expected_size_bytes=4, chunk_size=2)

    assert error.value.actual_size == 2


@pytest.mark.parametrize("failure", ["opened_type", "after_type", "after_identity"])
def test_opened_handle_type_and_identity_are_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"x")
    original_fstat = os.fstat
    calls = 0

    def altered_fstat(file_descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = original_fstat(file_descriptor)
        values = list(result)
        if (failure == "opened_type" and calls == 1) or (
            failure == "after_type" and calls == 2
        ):
            values[0] = stat.S_IFIFO
        elif failure == "after_identity" and calls == 2:
            values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", altered_fstat)
    expected_error = (
        FileIdentityChangedError if failure == "after_identity" else FileNotRegularError
    )

    with pytest.raises(expected_error):
        read_file_stably(path, max_size_bytes=1)


def test_path_type_is_revalidated_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"x")
    original_stat = Path.stat
    calls = 0

    def altered_stat(
        candidate: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal calls
        result = original_stat(candidate, follow_symlinks=follow_symlinks)
        if candidate != path or follow_symlinks:
            return result
        calls += 1
        if calls == 1:
            return result
        values = list(result)
        values[0] = stat.S_IFLNK
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", altered_stat)

    with pytest.raises(FileNotRegularError):
        read_file_stably(path, max_size_bytes=1)


def test_path_size_is_revalidated_after_handle_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"x")
    original_stat = Path.stat
    calls = 0

    def altered_stat(
        candidate: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal calls
        result = original_stat(candidate, follow_symlinks=follow_symlinks)
        if candidate != path or follow_symlinks:
            return result
        calls += 1
        if calls == 1:
            return result
        values = list(result)
        values[6] += 1
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", altered_stat)

    with pytest.raises(FileSizeChangedError):
        read_file_stably(path, max_size_bytes=1)


class _GrowAfterRead:
    def __init__(self, file: BinaryIO, path: Path) -> None:
        self.file = file
        self.path = path

    def __enter__(self) -> "_GrowAfterRead":
        return self

    def __exit__(self, *args: object) -> None:
        self.file.close()

    def fileno(self) -> int:
        return self.file.fileno()

    def read(self, size: int) -> bytes:
        chunk = self.file.read(size)
        with self.path.open("ab") as writer:
            writer.write(b"y")
        return chunk


def test_early_stop_still_detects_size_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"x")
    original_open = filesystem_module._open_binary

    def open_growing(candidate: Path) -> _GrowAfterRead:
        return _GrowAfterRead(original_open(candidate), candidate)

    monkeypatch.setattr(filesystem_module, "_open_binary", open_growing)

    with pytest.raises(FileSizeChangedError):
        read_file_stably(
            path,
            max_size_bytes=2,
            initial_chunk_size=1,
            stop_after_initial=lambda _: True,
        )


def test_no_follow_helpers_are_portably_exercised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"x")
    opened_flags: list[int] = []
    original_os_open = os.open

    def record_os_open(candidate: str, flags: int) -> int:
        opened_flags.append(flags)
        return original_os_open(candidate, flags)

    monkeypatch.setattr(os, "open", record_os_open)
    file_descriptor = filesystem_module._open_no_follow(str(path), os.O_RDONLY)
    os.close(file_descriptor)

    assert opened_flags

    monkeypatch.setattr(os, "O_NOFOLLOW", 1, raising=False)
    opened_with_opener = False

    def record_builtin_open(candidate: Path, mode: str, *, opener: object) -> BinaryIO:
        nonlocal opened_with_opener
        opened_with_opener = opener is filesystem_module._open_no_follow
        return cast(BinaryIO, builtin_open(candidate, mode))

    monkeypatch.setattr("contextforge.filesystem.builtin_open", record_builtin_open)
    with filesystem_module._open_binary(path) as file:
        assert file.read() == b"x"
    assert opened_with_opener is True
