"""Low-level, streaming file inspection utilities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from contextforge.filesystem import (
    FileTooLargeError as FileTooLargeError,
)
from contextforge.filesystem import read_file_stably

BINARY_SAMPLE_SIZE = 8 * 1024
HASH_CHUNK_SIZE = 64 * 1024

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_ALLOWED_TEXT_CONTROLS = frozenset(b"\b\t\n\f\r")


@dataclass(frozen=True, slots=True)
class FileInspection:
    """Metadata derived from one bounded, identity-checked file read."""

    size_bytes: int
    is_binary: bool
    sha256: str
    binary_reason: Literal["binary", "invalid_encoding"] | None = None


def normalize_relative_path(path: str | Path) -> str:
    """Return a normalized, POSIX-style repository-relative path.

    Both slash styles are accepted so callers can normalize serialized paths
    independently of the host operating system. Absolute paths and paths that
    escape above their starting point are rejected.
    """

    raw_path = str(path).replace("\\", "/")
    if (
        not raw_path
        or raw_path.startswith("/")
        or _WINDOWS_DRIVE.match(raw_path)
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
    ):
        raise ValueError("path must be a non-empty relative path")

    normalized_parts: list[str] = []
    for part in raw_path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not normalized_parts:
                raise ValueError("path must not escape its repository root")
            normalized_parts.pop()
            continue
        normalized_parts.append(part)

    if not normalized_parts:
        raise ValueError("path must identify a file or directory")
    return "/".join(normalized_parts)


def deterministic_relative_path(root: Path, path: Path) -> str:
    """Return ``path`` relative to ``root`` with deterministic separators."""

    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"repository root is not a directory: {root}")

    resolved_path = path.resolve(strict=True)
    try:
        relative_path = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path is outside repository root: {path}") from exc
    return normalize_relative_path(relative_path)


def is_binary_file(path: Path, *, sample_size: int = BINARY_SAMPLE_SIZE) -> bool:
    """Detect binary-like content from a small initial byte sample.

    A NUL byte, invalid UTF-8, or a high ratio of non-text control bytes marks
    the sample as binary. Empty files are treated as text.
    """

    if sample_size <= 0:
        raise ValueError("sample_size must be greater than zero")

    with path.open("rb") as file:
        sample = file.read(sample_size)

    return _sample_is_binary(sample)


def _sample_is_binary(sample: bytes) -> bool:
    return _sample_binary_reason(sample) is not None


def _sample_binary_reason(
    sample: bytes,
) -> Literal["binary", "invalid_encoding"] | None:
    if b"\x00" in sample:
        return "binary"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_encoding"

    control_count = sum(
        byte < 32 and byte not in _ALLOWED_TEXT_CONTROLS for byte in sample
    )
    if bool(sample) and control_count / len(sample) > 0.3:
        return "binary"
    return None


def is_text_file(path: Path, *, sample_size: int = BINARY_SAMPLE_SIZE) -> bool:
    """Return whether the initial content sample looks like UTF-8 text."""

    return not is_binary_file(path, sample_size=sample_size)


def sha256_file(path: Path, *, chunk_size: int = HASH_CHUNK_SIZE) -> str:
    """Calculate a file's SHA-256 digest while reading it in bounded chunks."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_file(
    path: Path,
    *,
    max_size_bytes: int,
    sample_size: int = BINARY_SAMPLE_SIZE,
    chunk_size: int = HASH_CHUNK_SIZE,
) -> FileInspection:
    """Inspect a stable regular file in one read bounded by ``max_size_bytes``.

    The pre-open and opened-file identities must match. This prevents a path
    replaced by a symbolic link or another file between traversal and opening
    from being silently inventoried. Growth during the read is capped at one
    byte beyond the configured maximum.
    """

    if max_size_bytes <= 0:
        raise ValueError("max_size_bytes must be greater than zero")
    if sample_size <= 0:
        raise ValueError("sample_size must be greater than zero")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    binary_reason: Literal["binary", "invalid_encoding"] | None = None

    def stop_after_sample(sample: bytes) -> bool:
        nonlocal binary_reason
        binary_reason = _sample_binary_reason(sample)
        return binary_reason is not None

    result = read_file_stably(
        path,
        max_size_bytes=max_size_bytes,
        chunk_size=chunk_size,
        initial_chunk_size=sample_size,
        stop_after_initial=stop_after_sample,
        capture_content=False,
    )
    return FileInspection(
        size_bytes=result.size_bytes,
        is_binary=not result.complete,
        sha256=result.sha256,
        binary_reason=binary_reason,
    )
