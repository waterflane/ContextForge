"""Synchronous, deterministic repository inventory scanning."""

from __future__ import annotations

import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from contextforge.repositories.files import (
    FileTooLargeError,
    inspect_file,
    normalize_relative_path,
)
from contextforge.repositories.ignore import IgnoreRules, load_ignore_rules
from contextforge.repositories.language import detect_language
from contextforge.repositories.models import (
    IgnoredFile,
    ProjectFile,
    ProjectSnapshot,
    ScanOptions,
    ScanSummary,
    SkippedFile,
)

__all__ = ["scan_repository"]


@dataclass(slots=True)
class _ScanState:
    """Mutable state scoped to a single scan invocation."""

    discovered_count: int = 0
    files: list[ProjectFile] = field(default_factory=list)
    ignored_files: list[IgnoredFile] = field(default_factory=list)
    skipped_files: list[SkippedFile] = field(default_factory=list)


def scan_repository(
    root: str | Path, options: ScanOptions | None = None
) -> ProjectSnapshot:
    """Scan a repository root and return an immutable inventory snapshot.

    Ignore control files are ordinary files: they are included unless an active
    rule explicitly excludes them. Symbolic links and directory junctions are
    always inventoried as skipped entries and are never followed.
    """

    resolved_root = _validate_root(root)
    active_options = options if options is not None else ScanOptions()
    rules = load_ignore_rules(
        resolved_root,
        include_gitignore=active_options.respect_gitignore,
        include_contextforgeignore=active_options.respect_contextforgeignore,
    )
    state = _ScanState()
    _scan_directories(resolved_root, rules, active_options, state)

    files = tuple(sorted(state.files, key=lambda item: item.path))
    ignored_files = tuple(sorted(state.ignored_files, key=lambda item: item.path))
    skipped_files = tuple(sorted(state.skipped_files, key=lambda item: item.path))
    summary = _build_summary(
        state.discovered_count, files, ignored_files, skipped_files
    )
    return ProjectSnapshot(
        root=resolved_root,
        files=files,
        ignored_files=ignored_files,
        skipped_files=skipped_files,
        summary=summary,
    )


def _validate_root(root: str | Path) -> Path:
    root_path = Path(root).expanduser()
    if not root_path.exists():
        raise FileNotFoundError(f"repository root does not exist: {root}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"repository root is not a directory: {root}")
    return root_path.resolve(strict=True)


def _scan_directories(
    root: Path,
    rules: IgnoreRules,
    options: ScanOptions,
    state: _ScanState,
) -> None:
    pending_directories = [(root, rules)]
    while pending_directories:
        directory, inherited_rules = pending_directories.pop()
        directory_rules = inherited_rules.for_directory(directory)
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            if directory == root:
                raise
            state.skipped_files.append(
                SkippedFile(
                    path=_relative_path(root, directory),
                    reason="unreadable",
                    detail=_error_detail(exc),
                )
            )
            continue

        child_directories: list[Path] = []
        for path in sorted(entries, key=lambda entry: entry.name):
            relative_path = _relative_path(root, path)
            try:
                mode = path.stat(follow_symlinks=False).st_mode
                is_junction = path.is_junction()
            except OSError as exc:
                state.discovered_count += 1
                state.skipped_files.append(
                    SkippedFile(
                        path=relative_path,
                        reason="unreadable",
                        detail=_error_detail(exc),
                    )
                )
                continue

            protected_match = directory_rules.match(relative_path)
            if protected_match is not None and protected_match.source == "protected":
                if not stat.S_ISDIR(mode) and not is_junction:
                    state.discovered_count += 1
                state.ignored_files.append(
                    IgnoredFile(
                        path=relative_path,
                        source=protected_match.source,
                        pattern=protected_match.pattern,
                        is_directory=stat.S_ISDIR(mode) or is_junction,
                    )
                )
            elif stat.S_ISLNK(mode) or is_junction:
                state.discovered_count += 1
                state.skipped_files.append(
                    SkippedFile(
                        path=relative_path,
                        reason="symlink",
                        detail=(
                            "symbolic links and directory junctions are not followed"
                        ),
                    )
                )
            elif stat.S_ISDIR(mode):
                ignore_match = directory_rules.match(relative_path, is_directory=True)
                if ignore_match is not None:
                    state.ignored_files.append(
                        IgnoredFile(
                            path=relative_path,
                            source=ignore_match.source,
                            pattern=ignore_match.pattern,
                            is_directory=True,
                        )
                    )
                else:
                    child_directories.append(path)
            elif stat.S_ISREG(mode):
                state.discovered_count += 1
                _scan_file(path, relative_path, directory_rules, options, state)
            else:
                state.discovered_count += 1
                state.skipped_files.append(
                    SkippedFile(
                        path=relative_path,
                        reason="unsupported",
                        detail=(
                            "entry is not a regular file, directory, symbolic link, "
                            "or junction"
                        ),
                    )
                )

        pending_directories.extend(
            (child, directory_rules) for child in reversed(child_directories)
        )


def _scan_file(
    path: Path,
    relative_path: str,
    rules: IgnoreRules,
    options: ScanOptions,
    state: _ScanState,
) -> None:
    ignore_match = rules.match(relative_path)
    if ignore_match is not None:
        state.ignored_files.append(
            IgnoredFile(
                path=relative_path,
                source=ignore_match.source,
                pattern=ignore_match.pattern,
            )
        )
        return

    try:
        inspection = inspect_file(
            path,
            max_size_bytes=options.max_file_size_bytes,
        )
        if inspection.is_binary:
            state.skipped_files.append(
                SkippedFile(
                    path=relative_path,
                    reason=inspection.binary_reason or "binary",
                )
            )
            return
    except FileTooLargeError as exc:
        state.skipped_files.append(
            SkippedFile(
                path=relative_path,
                reason="too_large",
                detail=str(exc),
            )
        )
        return
    except OSError as exc:
        state.skipped_files.append(
            SkippedFile(
                path=relative_path,
                reason="unreadable",
                detail=_error_detail(exc),
            )
        )
        return

    state.files.append(
        ProjectFile(
            path=relative_path,
            size_bytes=inspection.size_bytes,
            language=detect_language(relative_path),
            sha256=inspection.sha256,
            is_text=True,
        )
    )


def _relative_path(root: Path, path: Path) -> str:
    return normalize_relative_path(path.relative_to(root))


def _error_detail(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc}"


def _build_summary(
    discovered_count: int,
    files: tuple[ProjectFile, ...],
    ignored_files: tuple[IgnoredFile, ...],
    skipped_files: tuple[SkippedFile, ...],
) -> ScanSummary:
    languages = Counter(file.language for file in files if file.language is not None)
    protected_count = sum(item.source == "protected" for item in ignored_files)
    ignored_count = len(ignored_files) - protected_count
    reason_counts = Counter(item.reason for item in skipped_files)
    return ScanSummary(
        discovered_count=discovered_count,
        file_count=len(files),
        ignored_count=ignored_count,
        protected_count=protected_count,
        binary_count=reason_counts["binary"],
        invalid_encoding_count=reason_counts["invalid_encoding"],
        oversized_count=reason_counts["too_large"],
        failed_count=reason_counts["unreadable"],
        symlink_count=reason_counts["symlink"],
        unsupported_count=reason_counts["unsupported"],
        skipped_count=len(ignored_files) + len(skipped_files),
        total_size_bytes=sum(file.size_bytes for file in files),
        languages=dict(sorted(languages.items())),
    )
