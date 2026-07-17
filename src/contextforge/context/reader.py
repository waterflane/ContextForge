"""Verified decoding and line extraction for selected snapshot files."""

from __future__ import annotations

import codecs
import errno
import hashlib
import hmac
import stat
from dataclasses import dataclass
from pathlib import Path

from contextforge.context.selection import (
    InvalidLineRangeError,
    LineRange,
    LineRangeRequest,
    canonicalize_line_ranges,
)
from contextforge.filesystem import (
    DEFAULT_READ_CHUNK_SIZE,
    FileIdentityChangedError,
    FileNotRegularError,
    FileSizeChangedError,
    FileTooLargeError,
    read_file_stably,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot
from contextforge.repositories.files import normalize_relative_path


class ContextReaderError(ValueError):
    """Base class for deterministic selected-file read failures."""


class FileChangedError(ContextReaderError):
    """Base class for failures showing a file changed after scanning."""


class SelectedFileMissingError(FileChangedError):
    """Raised when a selected snapshot file no longer exists."""


class SelectedFileChangedError(FileChangedError):
    """Raised when selected-file identity, size, or hash changed."""


class SelectedFileNotRegularError(FileChangedError):
    """Raised for symlinks, junctions, directories, and special files."""


class SelectedFileOutsideRootError(ContextReaderError):
    """Raised when a selected path cannot remain beneath its snapshot root."""


class SelectedFileNotInSnapshotError(ContextReaderError):
    """Raised when a caller supplies an entry not owned by the snapshot."""


class TextDecodingError(ContextReaderError):
    """Base class for strict source decoding failures."""


class SelectedFileDecodeError(TextDecodingError):
    """Raised when verified bytes are not strict UTF-8."""


class ContextLimitError(ContextReaderError):
    """Base class for configured context-limit failures."""


class SelectedFileTooLargeError(ContextLimitError):
    """Raised when raw or included bytes exceed a reader limit."""


class SelectedFileReadError(ContextReaderError):
    """Raised for an operational filesystem read failure."""


class LineRangeBoundsError(InvalidLineRangeError):
    """Raised when a structurally valid range exceeds decoded source lines."""

    def __init__(self, path: str, line_range: LineRange, line_count: int) -> None:
        self.path = path
        self.line_range = line_range
        self.line_count = line_count
        super().__init__(
            line_range,
            f"range for {path!r} exceeds source line count {line_count}",
        )


@dataclass(frozen=True, slots=True)
class ReaderLimits:
    """Per-read limits used before a future multi-file builder exists."""

    max_files: int = 100
    max_source_bytes: int = 1_000_000
    max_content_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_source_bytes", self.max_source_bytes),
            ("max_content_bytes", self.max_content_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SelectedTextBlock:
    """One full-file or inclusive line-range text block."""

    line_range: LineRange | None
    text: str
    line_count: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SelectedTextFile:
    """Verified, canonical text ready for later package construction."""

    project_file: ProjectFile
    source_line_count: int
    blocks: tuple[SelectedTextBlock, ...]
    included_line_count: int
    included_content_bytes: int


def read_selected_text_files(
    snapshot: ProjectSnapshot,
    project_files: tuple[ProjectFile, ...],
    *,
    line_ranges: tuple[LineRangeRequest, ...] = (),
    limits: ReaderLimits | None = None,
    chunk_size: int = DEFAULT_READ_CHUNK_SIZE,
) -> tuple[SelectedTextFile, ...]:
    """Read a canonical selected set and return no result if any file fails."""

    active_limits = limits if limits is not None else ReaderLimits()
    if len(project_files) > active_limits.max_files:
        raise SelectedFileTooLargeError(
            f"selection has {len(project_files)} files; "
            f"limit is {active_limits.max_files}"
        )
    if any(not isinstance(item, ProjectFile) for item in project_files):
        raise SelectedFileNotInSnapshotError(
            "selected entries must be ProjectFile instances"
        )
    ordered_files = tuple(sorted(project_files, key=lambda item: item.path))
    if len({id(item) for item in ordered_files}) != len(ordered_files):
        raise SelectedFileNotInSnapshotError("selected entries must be unique")
    selected_paths = {item.path for item in ordered_files}
    ranges_by_path: dict[str, list[LineRange]] = {}
    for request in line_ranges:
        if not isinstance(request, LineRangeRequest):
            raise InvalidLineRangeError(request, "expected a LineRangeRequest")
        if request.path not in selected_paths:
            raise InvalidLineRangeError(
                request.path, "range target is not in the selected file set"
            )
        ranges_by_path.setdefault(request.path, []).append(request.range)

    results: list[SelectedTextFile] = []
    total_content_bytes = 0
    for project_file in ordered_files:
        result = read_selected_text_file(
            snapshot,
            project_file,
            line_ranges=canonicalize_line_ranges(
                ranges_by_path.get(project_file.path, ())
            ),
            limits=active_limits,
            chunk_size=chunk_size,
        )
        total_content_bytes += result.included_content_bytes
        if total_content_bytes > active_limits.max_content_bytes:
            raise SelectedFileTooLargeError(
                f"selected content has {total_content_bytes} bytes; "
                f"limit is {active_limits.max_content_bytes}"
            )
        results.append(result)
    return tuple(results)


def read_selected_text_file(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    line_ranges: tuple[LineRange, ...] = (),
    limits: ReaderLimits | None = None,
    chunk_size: int = DEFAULT_READ_CHUNK_SIZE,
) -> SelectedTextFile:
    """Safely re-open, verify, decode, and optionally range one snapshot file."""

    active_limits = limits if limits is not None else ReaderLimits()
    path, root, candidate = _authorize_candidate(snapshot, project_file)
    if project_file.size_bytes > active_limits.max_source_bytes:
        raise SelectedFileTooLargeError(
            f"selected file {path!r} has {project_file.size_bytes} source bytes; "
            f"limit is {active_limits.max_source_bytes}"
        )

    _validate_candidate_chain(root, candidate, path)
    try:
        raw = read_file_stably(
            candidate,
            max_size_bytes=active_limits.max_source_bytes,
            expected_size_bytes=project_file.size_bytes,
            chunk_size=chunk_size,
        )
    except FileNotFoundError as exc:
        raise SelectedFileMissingError(f"selected file is missing: {path}") from exc
    except FileNotRegularError as exc:
        raise SelectedFileNotRegularError(
            f"selected file is not regular: {path}"
        ) from exc
    except FileTooLargeError as exc:
        raise SelectedFileTooLargeError(
            f"selected file exceeds its byte limit: {path}"
        ) from exc
    except (FileIdentityChangedError, FileSizeChangedError) as exc:
        raise SelectedFileChangedError(f"selected file changed: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise SelectedFileMissingError(f"selected file is missing: {path}") from exc
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise SelectedFileNotRegularError(
                f"selected file is not regular: {path}"
            ) from exc
        raise SelectedFileReadError(f"could not read selected file: {path}") from exc

    _validate_candidate_chain(root, candidate, path)
    if not hmac.compare_digest(raw.sha256, project_file.sha256):
        raise SelectedFileChangedError(f"selected file hash changed: {path}")
    canonical_text = _decode_canonical_text(path, raw.content)
    lines = _canonical_lines(canonical_text)
    canonical_ranges = canonicalize_line_ranges(line_ranges)
    blocks = _build_blocks(path, canonical_text, lines, canonical_ranges)
    included_content_bytes = sum(block.size_bytes for block in blocks)
    if included_content_bytes > active_limits.max_content_bytes:
        raise SelectedFileTooLargeError(
            f"selected content for {path!r} has {included_content_bytes} bytes; "
            f"limit is {active_limits.max_content_bytes}"
        )
    return SelectedTextFile(
        project_file=project_file,
        source_line_count=len(lines),
        blocks=blocks,
        included_line_count=sum(block.line_count for block in blocks),
        included_content_bytes=included_content_bytes,
    )


def _authorize_candidate(
    snapshot: ProjectSnapshot, project_file: ProjectFile
) -> tuple[str, Path, Path]:
    if not isinstance(snapshot, ProjectSnapshot):
        raise SelectedFileNotInSnapshotError("expected a ProjectSnapshot")
    if not isinstance(project_file, ProjectFile) or not any(
        item is project_file for item in snapshot.files
    ):
        path = getattr(project_file, "path", project_file)
        raise SelectedFileNotInSnapshotError(
            f"selected entry does not originate from snapshot: {path!r}"
        )
    if sum(item.path == project_file.path for item in snapshot.files) != 1:
        raise SelectedFileNotInSnapshotError(
            f"snapshot path is not unique: {project_file.path!r}"
        )
    if project_file.is_text is not True:
        raise SelectedFileNotInSnapshotError(
            f"snapshot entry is not selectable text: {project_file.path!r}"
        )

    path = _strict_snapshot_path(project_file.path)
    snapshot_root = Path(snapshot.root)
    if not snapshot_root.is_absolute():
        raise SelectedFileOutsideRootError(
            f"snapshot root is not absolute for selected file: {path}"
        )
    try:
        root_metadata = snapshot_root.stat(follow_symlinks=False)
        root_is_junction = snapshot_root.is_junction()
        root = snapshot_root.resolve(strict=True)
    except OSError as exc:
        raise SelectedFileOutsideRootError(
            f"snapshot root is unavailable for selected file: {path}"
        ) from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or root_is_junction
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root != snapshot_root
    ):
        raise SelectedFileOutsideRootError(
            f"snapshot root is not a directory for selected file: {path}"
        )
    candidate = root.joinpath(*path.split("/"))
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # pragma: no cover - strict path check prevents this
        raise SelectedFileOutsideRootError(
            f"selected file is outside snapshot root: {path}"
        ) from exc
    return path, root, candidate


def _strict_snapshot_path(path: object) -> str:
    if (
        not isinstance(path, str)
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise SelectedFileOutsideRootError(
            f"selected snapshot path is not portable: {path!r}"
        )
    try:
        normalized = normalize_relative_path(path)
    except ValueError as exc:
        raise SelectedFileOutsideRootError(
            f"selected snapshot path is not portable: {path!r}"
        ) from exc
    if normalized != path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise SelectedFileOutsideRootError(
            f"selected snapshot path is not portable: {path!r}"
        )
    return path


def _validate_candidate_chain(root: Path, candidate: Path, path: str) -> None:
    current = root
    parts = path.split("/")
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
            is_junction = current.is_junction()
        except FileNotFoundError as exc:
            raise SelectedFileMissingError(f"selected file is missing: {path}") from exc
        except OSError as exc:
            raise SelectedFileReadError(
                f"could not inspect selected file: {path}"
            ) from exc
        is_final = index == len(parts) - 1
        expected_type = (
            stat.S_ISREG(metadata.st_mode)
            if is_final
            else stat.S_ISDIR(metadata.st_mode)
        )
        if stat.S_ISLNK(metadata.st_mode) or is_junction or not expected_type:
            raise SelectedFileNotRegularError(
                f"selected path contains a link or non-regular entry: {path}"
            )

    try:
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(root)
    except FileNotFoundError as exc:
        raise SelectedFileMissingError(f"selected file is missing: {path}") from exc
    except ValueError as exc:
        raise SelectedFileOutsideRootError(
            f"selected file resolves outside snapshot root: {path}"
        ) from exc
    except OSError as exc:
        raise SelectedFileReadError(f"could not resolve selected file: {path}") from exc


def _decode_canonical_text(path: str, raw: bytes) -> str:
    without_bom = (
        raw[len(codecs.BOM_UTF8) :] if raw.startswith(codecs.BOM_UTF8) else raw
    )
    try:
        decoded = without_bom.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SelectedFileDecodeError(
            f"selected file is not valid UTF-8: {path}"
        ) from exc
    return decoded.replace("\r\n", "\n").replace("\r", "\n")


def _canonical_lines(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    parts = text.split("\n")
    lines = [f"{part}\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return tuple(lines)


def _build_blocks(
    path: str,
    text: str,
    lines: tuple[str, ...],
    line_ranges: tuple[LineRange, ...],
) -> tuple[SelectedTextBlock, ...]:
    if not line_ranges:
        return (_make_block(None, text, len(lines)),)
    blocks: list[SelectedTextBlock] = []
    for line_range in line_ranges:
        if not lines or line_range.end > len(lines):
            raise LineRangeBoundsError(path, line_range, len(lines))
        selected = "".join(lines[line_range.start - 1 : line_range.end])
        blocks.append(
            _make_block(
                line_range,
                selected,
                line_range.end - line_range.start + 1,
            )
        )
    return tuple(blocks)


def _make_block(
    line_range: LineRange | None, text: str, line_count: int
) -> SelectedTextBlock:
    encoded = text.encode("utf-8")
    return SelectedTextBlock(
        line_range=line_range,
        text=text,
        line_count=line_count,
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )
