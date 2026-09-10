"""Digest-bound registry for ContextForge artifacts written inside a repository."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal

REGISTRY_SCHEMA_VERSION: Literal[1] = 1
REGISTRY_RELATIVE_PATH = ".contextforge/generated-artifacts.json"
MAX_REGISTRY_BYTES = 1_000_000
GeneratedArtifactKind = Literal["package", "capsule", "prompt"]


class GeneratedArtifactRegistryError(OSError):
    """Raised when an in-repository artifact cannot be registered safely."""


def load_generated_artifact_digests(repository_root: Path) -> dict[str, str]:
    """Load a strict registry, treating missing or corrupt state as empty."""

    return {
        path: digest
        for path, (digest, _) in _load_registry_entries(repository_root).items()
    }


def register_generated_artifact(
    repository_root: str | Path,
    artifact_path: str | Path,
    *,
    kind: GeneratedArtifactKind,
) -> bool:
    """Register one regular output only when it resolves beneath the repository."""

    if kind not in {"package", "capsule", "prompt"}:
        raise ValueError("generated artifact kind is unsupported")
    root = Path(repository_root).resolve(strict=True)
    requested = Path(artifact_path)
    if requested.is_symlink():
        raise GeneratedArtifactRegistryError("generated artifact must not be a link")
    destination = requested.resolve(strict=True)
    try:
        relative = destination.relative_to(root).as_posix()
    except ValueError:
        return False
    if not _is_portable_relative_path(relative):
        return False
    if not destination.is_file():
        raise GeneratedArtifactRegistryError("generated artifact is not a regular file")
    digest = _sha256_file(destination)
    registry = root / ".contextforge" / "generated-artifacts.json"
    directory = registry.parent
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise GeneratedArtifactRegistryError(
                "ContextForge state path is linked or not a directory"
            )
        if directory.resolve(strict=True) != directory:
            raise GeneratedArtifactRegistryError(
                "ContextForge state path is not canonical"
            )
        existing = _load_registry_entries(root)
        existing[relative] = (digest, kind)
        artifacts = [
            {"kind": entry_kind, "path": path, "sha256": entry_digest}
            for path, (entry_digest, entry_kind) in sorted(existing.items())
        ]
        content = (
            json.dumps(
                {
                    "schema_version": REGISTRY_SCHEMA_VERSION,
                    "artifacts": artifacts,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        _write_atomic(registry, content)
    except GeneratedArtifactRegistryError:
        raise
    except OSError as exc:
        raise GeneratedArtifactRegistryError(
            "unable to update generated artifact registry"
        ) from exc
    return True


def _load_registry_entries(
    repository_root: Path,
) -> dict[str, tuple[str, GeneratedArtifactKind]]:
    path = repository_root / ".contextforge" / "generated-artifacts.json"
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        content = path.read_bytes()
    except OSError:
        return {}
    if len(content) > MAX_REGISTRY_BYTES:
        return {}
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "artifacts",
    }:
        return {}
    artifacts = payload["artifacts"]
    if payload["schema_version"] != REGISTRY_SCHEMA_VERSION or not isinstance(
        artifacts, list
    ):
        return {}
    result: dict[str, tuple[str, GeneratedArtifactKind]] = {}
    previous = ""
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"kind", "path", "sha256"}:
            return {}
        path_value = item["path"]
        digest = item["sha256"]
        kind = item["kind"]
        if (
            not isinstance(path_value, str)
            or not _is_portable_relative_path(path_value)
            or path_value <= previous
            or not isinstance(digest, str)
            or not _is_sha256(digest)
            or kind not in {"package", "capsule", "prompt"}
        ):
            return {}
        previous = path_value
        result[path_value] = (digest, kind)
    return result


def _write_atomic(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".generated-artifacts.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_portable_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and all(
        part not in {"", ".", ".."} for part in pure.parts
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


__all__ = [
    "MAX_REGISTRY_BYTES",
    "REGISTRY_RELATIVE_PATH",
    "REGISTRY_SCHEMA_VERSION",
    "GeneratedArtifactKind",
    "GeneratedArtifactRegistryError",
    "load_generated_artifact_digests",
    "register_generated_artifact",
]
