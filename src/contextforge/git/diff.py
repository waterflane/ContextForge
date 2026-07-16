"""Fixed-argument, bounded, read-only Git diff collection."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.core.validation import Sha256, validate_portable_relative_path
from contextforge.repositories import ProjectSnapshot

GIT_DIFF_SCHEMA_VERSION: Literal[1] = 1
MAX_GIT_DIFF_BYTES = 1024 * 1024
MAX_GIT_TIMEOUT_SECONDS = 30.0

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+~-]{0,199}$")
_HEX_REVISION = re.compile(r"^[0-9a-f]{40,64}$")


class GitModel(BaseModel):
    """Closed immutable Git artifact base."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class GitDiffRequest(GitModel):
    """One caller-selected safe diff shape; never a command string."""

    schema_version: Literal[1] = GIT_DIFF_SCHEMA_VERSION
    mode: Literal["working", "staged", "base"] = "working"
    base_ref: str | None = None
    paths: tuple[str, ...] = ()
    context_lines: int = Field(default=3, ge=0, le=100, strict=True)
    max_bytes: int = Field(default=256 * 1024, ge=1, le=MAX_GIT_DIFF_BYTES, strict=True)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=MAX_GIT_TIMEOUT_SECONDS)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("Git diff paths must be unique and canonical")
        return paths

    @field_validator("base_ref")
    @classmethod
    def validate_base_ref(cls, value: str | None) -> str | None:
        if value is not None and (
            value.startswith("-") or not _SAFE_REF.fullmatch(value)
        ):
            raise ValueError("base_ref must be a safe bounded Git revision")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> GitDiffRequest:
        if self.mode == "base" and self.base_ref is None:
            raise ValueError("base mode requires base_ref")
        if self.mode != "base" and self.base_ref is not None:
            raise ValueError("base_ref is accepted only in base mode")
        return self


class GitChangedFile(GitModel):
    """Portable changed-path summary without repository content."""

    path: str
    status: Literal["modified", "added", "deleted", "untracked"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class GitDiffContext(GitModel):
    """Sanitized optional Git context suitable for a handoff artifact."""

    schema_version: Literal[1] = GIT_DIFF_SCHEMA_VERSION
    available: bool
    mode: Literal["working", "staged", "base"]
    base_revision: str | None = None
    head_revision: str | None = None
    sha256: Sha256
    text: str = ""
    changed_files: tuple[GitChangedFile, ...] = ()
    touched_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()

    @field_validator("base_revision", "head_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is not None and not _HEX_REVISION.fullmatch(value):
            raise ValueError("resolved Git revisions must be lowercase hexadecimal")
        return value

    @field_validator("touched_paths", "deleted_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("Git context paths must be unique and canonical")
        return paths

    @field_validator("diagnostics")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Git diagnostics must be unique and canonical")
        if any(not item or len(item) > 500 or "\x00" in item for item in value):
            raise ValueError("Git diagnostics must be bounded text")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> GitDiffContext:
        encoded = self.text.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.sha256:
            raise ValueError("Git diff SHA-256 does not match text")
        changed_paths = tuple(item.path for item in self.changed_files)
        if changed_paths != tuple(sorted(set(changed_paths))):
            raise ValueError("changed files must be unique and canonical")
        if self.available is False and self.text:
            raise ValueError("unavailable Git context cannot contain a diff")
        return self


class GitDiffError(RuntimeError):
    """Raised when a requested Git operation violates its bounded contract."""


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False


def collect_git_diff(
    snapshot: ProjectSnapshot,
    request: GitDiffRequest,
) -> GitDiffContext:
    """Collect a fixed-shape read-only diff, or an explicit unavailable artifact."""

    if not isinstance(snapshot, ProjectSnapshot):
        raise TypeError("Git diff collection requires a ProjectSnapshot")
    if not isinstance(request, GitDiffRequest):
        raise TypeError("Git diff collection requires a GitDiffRequest")
    allowed = {item.path for item in snapshot.files}
    if not set(request.paths) <= allowed:
        raise GitDiffError("Git diff paths must belong to the current snapshot")

    root = Path(snapshot.root)
    try:
        inside = _run_git(root, ("rev-parse", "--is-inside-work-tree"), request)
    except FileNotFoundError:
        return _unavailable(request.mode, "git-executable-unavailable")
    except (subprocess.SubprocessError, OSError):
        return _unavailable(request.mode, "git-repository-check-failed")
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        return _unavailable(request.mode, "not-a-git-repository")

    try:
        head = _resolve_revision(root, "HEAD", request)
        base = (
            _resolve_revision(root, request.base_ref or "", request)
            if request.mode == "base"
            else None
        )
        diff_args = _diff_arguments(request, names_only=False)
        diff_result = _run_git(root, diff_args, request)
        if diff_result.returncode not in {0, 1}:
            raise GitDiffError("bounded Git diff failed")
        truncated = (
            diff_result.stdout_truncated or len(diff_result.stdout) > request.max_bytes
        )
        encoded = diff_result.stdout
        if truncated:
            encoded = _truncate_utf8(encoded, request.max_bytes)
        text = _strict_utf8(encoded, "Git diff")

        touched = set(_collect_names(root, request, deleted=False))
        deleted = set(_collect_names(root, request, deleted=True))
        untracked: set[str] = set()
        if request.mode == "working":
            untracked = set(_collect_untracked(root, request))
            touched.update(untracked)
        touched = {path for path in touched if path in allowed}
        deleted = {path for path in deleted if path not in allowed or path in touched}
        changed = tuple(
            GitChangedFile(
                path=path,
                status=(
                    "untracked"
                    if path in untracked
                    else "deleted"
                    if path in deleted
                    else "modified"
                ),
            )
            for path in sorted(touched | deleted)
        )
        diagnostics = ("diff-truncated",) if truncated else ()
        return GitDiffContext(
            available=True,
            mode=request.mode,
            base_revision=base,
            head_revision=head,
            sha256=hashlib.sha256(encoded).hexdigest(),
            text=text,
            changed_files=changed,
            touched_paths=tuple(sorted(touched)),
            deleted_paths=tuple(sorted(deleted)),
            truncated=truncated,
            diagnostics=diagnostics,
        )
    except (GitDiffError, UnicodeDecodeError, subprocess.SubprocessError, OSError):
        return _unavailable(request.mode, "git-diff-unavailable")


def _diff_arguments(request: GitDiffRequest, *, names_only: bool) -> tuple[str, ...]:
    arguments = [
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        f"--unified={request.context_lines}",
    ]
    if names_only:
        arguments.extend(("--name-only", "-z"))
    if request.mode == "staged":
        arguments.append("--cached")
    elif request.mode == "base":
        assert request.base_ref is not None
        arguments.append(request.base_ref)
    arguments.append("--")
    arguments.extend(request.paths)
    return tuple(arguments)


def _collect_names(
    root: Path, request: GitDiffRequest, *, deleted: bool
) -> tuple[str, ...]:
    arguments = list(_diff_arguments(request, names_only=True))
    arguments.insert(5, "--diff-filter=D" if deleted else "--diff-filter=ACMRTUXB")
    result = _run_git(root, tuple(arguments), request)
    if result.returncode not in {0, 1}:
        raise GitDiffError("Git changed-file summary failed")
    if result.stdout_truncated or len(result.stdout) > request.max_bytes:
        raise GitDiffError("Git changed-file summary exceeded its byte limit")
    return _parse_nul_paths(result.stdout)


def _collect_untracked(root: Path, request: GitDiffRequest) -> tuple[str, ...]:
    arguments = ["ls-files", "--others", "--exclude-standard", "-z", "--"]
    arguments.extend(request.paths)
    result = _run_git(root, tuple(arguments), request)
    if result.returncode != 0:
        raise GitDiffError("Git untracked-file summary failed")
    if result.stdout_truncated or len(result.stdout) > request.max_bytes:
        raise GitDiffError("Git untracked-file summary exceeded its byte limit")
    return _parse_nul_paths(result.stdout)


def _parse_nul_paths(data: bytes) -> tuple[str, ...]:
    text = _strict_utf8(data, "Git path summary")
    values = []
    for raw in text.split("\x00"):
        if not raw:
            continue
        values.append(validate_portable_relative_path(raw))
    return tuple(sorted(set(values)))


def _resolve_revision(root: Path, revision: str, request: GitDiffRequest) -> str | None:
    result = _run_git(
        root, ("rev-parse", "--verify", f"{revision}^{{commit}}"), request
    )
    if result.returncode != 0:
        return None
    value = _strict_utf8(result.stdout, "Git revision").strip().lower()
    return value if _HEX_REVISION.fullmatch(value) else None


def _run_git(
    root: Path,
    arguments: tuple[str, ...],
    request: GitDiffRequest,
) -> _GitResult:
    command = (
        "git",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
        *arguments,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    process = subprocess.Popen(
        command,
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    stdout_state = [False]
    stdout_thread = threading.Thread(
        target=_drain_bounded,
        args=(process.stdout, request.max_bytes + 1, stdout, stdout_state),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_bounded,
        args=(process.stderr, 64 * 1024, stderr, [False]),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=request.timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        stdout_thread.join()
        stderr_thread.join()
    return _GitResult(
        returncode=returncode,
        stdout=b"".join(stdout),
        stderr=b"".join(stderr),
        stdout_truncated=stdout_state[0],
    )


def _drain_bounded(
    stream: BinaryIO,
    maximum: int,
    chunks: list[bytes],
    truncated: list[bool],
) -> None:
    stored = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        remaining = maximum - stored
        if remaining > 0:
            kept = chunk[:remaining]
            chunks.append(kept)
            stored += len(kept)
        if len(chunk) > remaining:
            truncated[0] = True


def _strict_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitDiffError(f"{label} is not valid UTF-8") from exc


def _truncate_utf8(data: bytes, maximum: int) -> bytes:
    candidate = data[:maximum]
    while candidate:
        try:
            candidate.decode("utf-8", errors="strict")
            return candidate
        except UnicodeDecodeError as exc:
            candidate = candidate[: exc.start]
    return b""


def _unavailable(
    mode: Literal["working", "staged", "base"], code: str
) -> GitDiffContext:
    return GitDiffContext(
        available=False,
        mode=mode,
        sha256=hashlib.sha256(b"").hexdigest(),
        diagnostics=(code,),
    )


__all__ = [
    "GIT_DIFF_SCHEMA_VERSION",
    "GitChangedFile",
    "GitDiffContext",
    "GitDiffError",
    "GitDiffRequest",
    "collect_git_diff",
]
