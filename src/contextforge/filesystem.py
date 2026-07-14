"""Shared low-level primitives for bounded, identity-checked file reads."""

from __future__ import annotations

import hashlib
import os
import stat
from builtins import open as builtin_open
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

DEFAULT_READ_CHUNK_SIZE = 64 * 1024
_ORIGINAL_PATH_OPEN = Path.open


class FileTooLargeError(ValueError):
    """Raised when a file exceeds its configured byte limit."""

    def __init__(self, size_bytes: int, max_size_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_size_bytes = max_size_bytes
        super().__init__(f"file size {size_bytes} exceeds limit {max_size_bytes}")


class StableReadError(OSError):
    """Base class for consistency failures during a stable file read."""


class FileNotRegularError(StableReadError):
    """Raised when the selected entry is not a regular file."""


class FileIdentityChangedError(StableReadError):
    """Raised when the path and opened handle do not identify one file."""


class FileSizeChangedError(StableReadError):
    """Raised when a file does not retain its expected size."""

    def __init__(self, expected_size: int, actual_size: int) -> None:
        self.expected_size = expected_size
        self.actual_size = actual_size
        super().__init__(
            f"file size changed: expected {expected_size}, found {actual_size}"
        )


@dataclass(frozen=True, slots=True)
class StableFileRead:
    """Result of one bounded file-descriptor read."""

    content: bytes
    size_bytes: int
    sha256: str
    complete: bool = True


def read_file_stably(
    path: Path,
    *,
    max_size_bytes: int,
    expected_size_bytes: int | None = None,
    chunk_size: int = DEFAULT_READ_CHUNK_SIZE,
    initial_chunk_size: int | None = None,
    stop_after_initial: Callable[[bytes], bool] | None = None,
    capture_content: bool = True,
) -> StableFileRead:
    """Read one regular file without silently mixing identities or versions.

    The read is capped at one byte beyond the expected/configured size. When an
    initial-chunk predicate requests an early stop, the returned digest and
    content describe only that sample and ``complete`` is false.
    """

    _validate_read_options(
        max_size_bytes=max_size_bytes,
        expected_size_bytes=expected_size_bytes,
        chunk_size=chunk_size,
        initial_chunk_size=initial_chunk_size,
        stop_after_initial=stop_after_initial,
    )
    read_limit = (
        expected_size_bytes if expected_size_bytes is not None else max_size_bytes
    )

    before_open = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before_open.st_mode):
        raise FileNotRegularError(f"entry is no longer a regular file: {path}")
    _check_initial_size(
        before_open.st_size,
        max_size_bytes=max_size_bytes,
        expected_size_bytes=expected_size_bytes,
    )

    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total_size = 0
    with _open_binary(path) as file:
        opened = os.fstat(file.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise FileNotRegularError(f"opened entry is not a regular file: {path}")
        if not os.path.samestat(before_open, opened):
            raise FileIdentityChangedError(f"file changed while being opened: {path}")
        _check_initial_size(
            opened.st_size,
            max_size_bytes=max_size_bytes,
            expected_size_bytes=expected_size_bytes,
        )

        if initial_chunk_size is not None:
            first_chunk = file.read(min(initial_chunk_size, read_limit + 1))
            total_size = len(first_chunk)
            _check_read_size(
                file.fileno(),
                total_size,
                max_size_bytes=max_size_bytes,
                expected_size_bytes=expected_size_bytes,
            )
            digest.update(first_chunk)
            if capture_content:
                chunks.append(first_chunk)
            if stop_after_initial is not None and stop_after_initial(first_chunk):
                after_initial = _validate_after_read(
                    path, before_open, opened, file.fileno()
                )
                if after_initial.st_size != opened.st_size:
                    raise FileSizeChangedError(opened.st_size, after_initial.st_size)
                return StableFileRead(
                    content=b"".join(chunks),
                    size_bytes=opened.st_size,
                    sha256=digest.hexdigest(),
                    complete=False,
                )

        while True:
            requested = min(chunk_size, read_limit - total_size + 1)
            chunk = file.read(requested)
            if not chunk:
                break
            total_size += len(chunk)
            _check_read_size(
                file.fileno(),
                total_size,
                max_size_bytes=max_size_bytes,
                expected_size_bytes=expected_size_bytes,
            )
            digest.update(chunk)
            if capture_content:
                chunks.append(chunk)

        after_read = _validate_after_read(path, before_open, opened, file.fileno())
        if after_read.st_size != opened.st_size:
            raise FileSizeChangedError(opened.st_size, after_read.st_size)
        if total_size != after_read.st_size:
            raise FileSizeChangedError(after_read.st_size, total_size)

    return StableFileRead(
        content=b"".join(chunks),
        size_bytes=total_size,
        sha256=digest.hexdigest(),
    )


def _validate_read_options(
    *,
    max_size_bytes: int,
    expected_size_bytes: int | None,
    chunk_size: int,
    initial_chunk_size: int | None,
    stop_after_initial: Callable[[bytes], bool] | None,
) -> None:
    if max_size_bytes <= 0:
        raise ValueError("max_size_bytes must be greater than zero")
    if expected_size_bytes is not None and expected_size_bytes < 0:
        raise ValueError("expected_size_bytes must be non-negative")
    if expected_size_bytes is not None and expected_size_bytes > max_size_bytes:
        raise FileTooLargeError(expected_size_bytes, max_size_bytes)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if initial_chunk_size is not None and initial_chunk_size <= 0:
        raise ValueError("initial_chunk_size must be greater than zero")
    if stop_after_initial is not None and initial_chunk_size is None:
        raise ValueError("stop_after_initial requires initial_chunk_size")


def _check_initial_size(
    actual_size: int,
    *,
    max_size_bytes: int,
    expected_size_bytes: int | None,
) -> None:
    if actual_size > max_size_bytes:
        raise FileTooLargeError(actual_size, max_size_bytes)
    if expected_size_bytes is not None and actual_size != expected_size_bytes:
        raise FileSizeChangedError(expected_size_bytes, actual_size)


def _check_read_size(
    file_descriptor: int,
    total_size: int,
    *,
    max_size_bytes: int,
    expected_size_bytes: int | None,
) -> None:
    limit = expected_size_bytes if expected_size_bytes is not None else max_size_bytes
    if total_size <= limit:
        return
    actual_size = max(total_size, os.fstat(file_descriptor).st_size)
    if expected_size_bytes is not None:
        raise FileSizeChangedError(expected_size_bytes, actual_size)
    raise FileTooLargeError(actual_size, max_size_bytes)


def _validate_after_read(
    path: Path,
    before_open: os.stat_result,
    opened: os.stat_result,
    file_descriptor: int,
) -> os.stat_result:
    after_read = os.fstat(file_descriptor)
    if not stat.S_ISREG(after_read.st_mode):
        raise FileNotRegularError(f"opened entry stopped being regular: {path}")
    if not os.path.samestat(opened, after_read):
        raise FileIdentityChangedError(f"opened file identity changed: {path}")

    after_path = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(after_path.st_mode):
        raise FileNotRegularError(f"entry is no longer a regular file: {path}")
    if after_path.st_size != after_read.st_size:
        raise FileSizeChangedError(after_read.st_size, after_path.st_size)
    if not os.path.samestat(before_open, after_path) or not os.path.samestat(
        after_read, after_path
    ):
        raise FileIdentityChangedError(f"file was replaced while being read: {path}")
    return after_read


def _open_no_follow(path: str, flags: int) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    return os.open(path, flags | no_follow | binary)


def _open_binary(path: Path) -> BinaryIO:
    """Open a binary path without following the final link where supported."""

    if Path.open is not _ORIGINAL_PATH_OPEN:
        return path.open("rb")
    if getattr(os, "O_NOFOLLOW", 0):
        return builtin_open(path, "rb", opener=_open_no_follow)
    return path.open("rb")
