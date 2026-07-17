"""Safe local persistence, immutable generation publication, and index locking."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import socket
import stat
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from contextforge.filesystem import read_file_stably
from contextforge.intelligence.manifest import (
    calculate_generation_id,
    canonical_json_bytes,
    compare_index_status,
)
from contextforge.intelligence.models import (
    INDEX_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    ActiveIndexPointer,
    AnalyzerIdentity,
    IndexedFileState,
    IndexManifest,
    IndexStatus,
    SchemaVersionMetadata,
    validate_portable_relative_path,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot

CONTEXTFORGE_DIRECTORY = ".contextforge"
INDEX_DIRECTORY = "index"
CONFIG_FILENAME = "config.toml"
ACTIVE_MANIFEST_FILENAME = "manifest.json"
LOCK_FILENAME = "lock.json"
STAGING_DIRECTORY = "staging"
GENERATIONS_DIRECTORY = "generations"
CONTEXTS_DIRECTORY = "contexts"
RUNS_DIRECTORY = "runs"
TEMPORARY_SUFFIX = ".contextforge-tmp"
MAX_MANIFEST_BYTES = 4_000_000
MAX_RECORD_BYTES = 16_000_000
WINDOWS_DIRECTORY_REPLACE_RETRY_DELAYS = (0.01, 0.05)
MAX_WINDOWS_DIRECTORY_REPLACE_RETRIES = len(WINDOWS_DIRECTORY_REPLACE_RETRY_DELAYS)
_WINDOWS_RETRYABLE_DIRECTORY_REPLACE_ERRORS = frozenset({5, 32, 33})

DEFAULT_CONFIG = """config_version = 1

[models]
provider = "ollama"
endpoint = "http://127.0.0.1:11434/api/chat"
# For provider = "openai-compatible" (or the CLI alias "lmstudio"):
# base_url = "http://localhost:1234/v1"
model = "qwen2.5-coder"
timeout_seconds = 120
max_response_bytes = 1000000
concurrency_limit = 2
retry_limit = 2
local_only = true
external_data_policy = "deny"
store_raw_prompts = false
store_raw_responses = false
# credential_env = "LM_STUDIO_API_KEY"

[retention]
runs = 10
index_generations = 2
"""

_STAGED_ROOT_RECORDS = {
    "symbols.jsonl": b"",
    "relationships.jsonl": b"",
    "overview.json": b"null\n",
    "architecture.json": b"null\n",
    "features.json": b"null\n",
}
_RUN_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class IndexStorageError(OSError):
    """Base class for repository-intelligence storage failures."""


class IndexPathError(IndexStorageError):
    """Raised when generated storage could escape or traverse a linked path."""


class IndexManifestNotFoundError(IndexStorageError):
    """Raised when no active index pointer has been published."""


class IndexManifestReadError(IndexStorageError):
    """Raised when an active pointer or generation manifest is malformed."""


class UnsupportedIndexSchemaError(IndexManifestReadError):
    """Raised when a persisted index uses an unknown integer schema version."""

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        super().__init__(f"unsupported index schema version: {schema_version}")


class IndexRecordWriteError(IndexStorageError):
    """Raised when a staged record cannot be atomically published."""


class IndexPublicationError(IndexStorageError):
    """Raised when a complete generation cannot be safely activated."""


class IndexLockError(IndexStorageError):
    """Base class for single-writer lock failures."""


class IndexLockActiveError(IndexLockError):
    """Raised when an apparently active writer owns the index lock."""


class IndexLockRecoveryRequiredError(IndexLockError):
    """Raised when an abandoned or unverifiable lock needs explicit recovery."""


class IndexLockOwnershipError(IndexLockError):
    """Raised when a lock token no longer owns the on-disk lock."""


@dataclass(frozen=True, slots=True)
class IndexLayout:
    """Operational absolute paths; never serialized into canonical manifests."""

    repository_root: Path
    contextforge_root: Path
    config: Path
    index: Path
    active_manifest: Path
    lock: Path
    staging: Path
    generations: Path
    contexts: Path
    runs: Path


@dataclass(slots=True)
class IndexWriteLock:
    """Ownership token for one bounded single-writer critical section."""

    layout: IndexLayout
    run_id: str
    owner_nonce: str
    _active: bool = True

    def __enter__(self) -> IndexWriteLock:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    @property
    def active(self) -> bool:
        """Return whether this in-process token has not been released."""

        return self._active

    def release(self) -> None:
        """Remove only the exact lock owned by this token."""

        if not self._active:
            return
        try:
            metadata = _read_lock_metadata(self.layout.lock)
        except FileNotFoundError:
            self._active = False
            return
        except IndexLockError as exc:
            raise IndexLockOwnershipError(
                "index lock metadata changed before release"
            ) from exc
        if metadata.get("owner_nonce") != self.owner_nonce:
            raise IndexLockOwnershipError("index lock is owned by another writer")
        try:
            self.layout.lock.unlink()
        except OSError as exc:
            raise IndexLockError("unable to release index writer lock") from exc
        self._active = False


def initialize_index(repository_root: str | Path) -> IndexLayout:
    """Create the safe storage skeleton and a config only when it is absent.

    ``config.toml`` is a user-owned input after creation. Generated index,
    context, and run directories are separate children and scanner-protected.
    """

    layout = _layout(repository_root)
    _ensure_directory(layout.contextforge_root)
    _ensure_directory(layout.index)
    _ensure_directory(layout.staging)
    _ensure_directory(layout.generations)
    _ensure_directory(layout.contexts)
    _ensure_directory(layout.runs)
    _create_user_config_once(layout.config)
    return layout


def acquire_index_lock(
    repository_root: str | Path,
    run_id: str,
    *,
    recover_stale: bool = False,
    confirm_unknown: bool = False,
    process_is_running: Callable[[int], bool] | None = None,
) -> IndexWriteLock:
    """Acquire the index writer lock without unbounded waiting.

    A same-host dead process can be recovered only with ``recover_stale``.
    Malformed or other-host metadata requires ``confirm_unknown``. An active
    same-host process is never removed by either option.
    """

    run_id = _validate_run_id(run_id)
    layout = initialize_index(repository_root)
    checker = process_is_running or _process_is_running
    owner_nonce = secrets.token_hex(16)
    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "pid": os.getpid(),
        "host_fingerprint": _host_fingerprint(),
        "started_at": datetime.now(UTC).isoformat(),
        "owner_nonce": owner_nonce,
    }
    encoded = canonical_json_bytes(metadata)

    for attempt in range(2):
        try:
            _create_lock_file(layout.lock, encoded)
            return IndexWriteLock(layout, run_id, owner_nonce)
        except FileExistsError:
            if attempt:
                raise IndexLockError("index lock changed during recovery") from None
            _recover_or_reject_lock(
                layout.lock,
                recover_stale=recover_stale,
                confirm_unknown=confirm_unknown,
                process_is_running=checker,
            )
    raise IndexLockError("unable to acquire index writer lock")


def begin_index_build(lock: IndexWriteLock) -> Path:
    """Create or reopen one resumable staging generation for the lock run ID."""

    _require_lock(lock)
    stage = lock.layout.staging / lock.run_id
    _ensure_directory(stage)
    _ensure_directory(stage / "files")
    for name, content in _STAGED_ROOT_RECORDS.items():
        destination = stage / name
        if not os.path.lexists(destination):
            _atomic_write_bytes(destination, content, error_type=IndexRecordWriteError)
        else:
            _require_regular_file(destination)
    return stage


def write_index_record(
    lock: IndexWriteLock,
    record_location: str,
    content: bytes | str,
) -> str:
    """Atomically publish one staged record and return its SHA-256 digest."""

    _require_lock(lock)
    location = _validate_record_location(record_location)
    try:
        encoded = (
            content.encode("utf-8") if isinstance(content, str) else bytes(content)
        )
    except (TypeError, UnicodeError) as exc:
        raise IndexRecordWriteError(
            "record content must be bytes or valid UTF-8 text"
        ) from exc
    if len(encoded) > MAX_RECORD_BYTES:
        raise IndexRecordWriteError("record exceeds the storage byte limit")
    stage = begin_index_build(lock)
    destination = stage.joinpath(*location.split("/"))
    _ensure_directory_chain(stage, destination.parent)
    _atomic_write_bytes(destination, encoded, error_type=IndexRecordWriteError)
    return hashlib.sha256(encoded).hexdigest()


def write_manifest(lock: IndexWriteLock, manifest: IndexManifest) -> Path:
    """Publish an immutable complete generation, then atomically switch pointer."""

    _require_lock(lock)
    _require_supported_schema_versions(manifest.schema_versions)
    expected_generation_id = calculate_generation_id(manifest)
    if manifest.generation_id != expected_generation_id:
        raise IndexPublicationError(
            "manifest generation_id does not match canonical content"
        )

    generation = lock.layout.generations / manifest.generation_id
    if os.path.lexists(generation):
        _require_directory(generation)
        _validate_generation(generation, manifest)
    else:
        stage = begin_index_build(lock)
        _validate_generation_records(stage, manifest)
        manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
        _atomic_write_bytes(
            stage / ACTIVE_MANIFEST_FILENAME,
            manifest_bytes,
            error_type=IndexPublicationError,
        )
        _fsync_directory(stage)
        try:
            _replace_directory_for_publication(stage, generation)
        except OSError as exc:
            raise IndexPublicationError(
                "unable to materialize immutable generation"
            ) from exc
        _fsync_directory(lock.layout.generations)
        _validate_generation(generation, manifest)

    pointer = ActiveIndexPointer(
        generation_id=manifest.generation_id,
        generation_manifest=(
            f"{GENERATIONS_DIRECTORY}/{manifest.generation_id}/"
            f"{ACTIVE_MANIFEST_FILENAME}"
        ),
        source_snapshot_digest=manifest.build.source_snapshot_digest,
    )
    _atomic_write_bytes(
        lock.layout.active_manifest,
        canonical_json_bytes(pointer.model_dump(mode="json")),
        error_type=IndexPublicationError,
    )
    _fsync_directory(lock.layout.index)
    return generation


def load_manifest(repository_root: str | Path) -> IndexManifest:
    """Load and validate the active pointer and its immutable generation once."""

    layout = _layout(repository_root)
    if not os.path.lexists(layout.active_manifest):
        raise IndexManifestNotFoundError("no active repository index is published")
    _require_safe_existing_chain(layout.contextforge_root, layout.active_manifest)
    pointer = _read_persisted_model(
        layout.active_manifest,
        ActiveIndexPointer,
        expected_schema=INDEX_SCHEMA_VERSION,
    )
    generation_manifest = layout.index.joinpath(*pointer.generation_manifest.split("/"))
    _require_safe_existing_chain(layout.index, generation_manifest)
    manifest = _read_persisted_model(
        generation_manifest,
        IndexManifest,
        expected_schema=MANIFEST_SCHEMA_VERSION,
    )
    _require_supported_schema_versions(manifest.schema_versions)
    if manifest.generation_id != pointer.generation_id:
        raise IndexManifestReadError(
            "active pointer generation does not match manifest"
        )
    if manifest.build.source_snapshot_digest != pointer.source_snapshot_digest:
        raise IndexManifestReadError(
            "active pointer snapshot digest does not match manifest"
        )
    if calculate_generation_id(manifest) != manifest.generation_id:
        raise IndexManifestReadError("generation manifest digest is invalid")
    return manifest


def load_generation_manifest(
    repository_root: str | Path, generation_id: str
) -> IndexManifest:
    """Load one explicitly pinned immutable generation by its content ID."""

    if (
        not isinstance(generation_id, str)
        or len(generation_id) != 64
        or any(character not in "0123456789abcdef" for character in generation_id)
    ):
        raise IndexManifestReadError("generation ID must be lowercase SHA-256")
    layout = _layout(repository_root)
    generation = layout.generations / generation_id
    manifest_path = generation / ACTIVE_MANIFEST_FILENAME
    if not os.path.lexists(manifest_path):
        raise IndexManifestReadError("requested immutable generation is unavailable")
    _require_safe_existing_chain(layout.generations, manifest_path)
    manifest = _read_persisted_model(
        manifest_path,
        IndexManifest,
        expected_schema=MANIFEST_SCHEMA_VERSION,
    )
    _require_supported_schema_versions(manifest.schema_versions)
    if (
        manifest.generation_id != generation_id
        or calculate_generation_id(manifest) != generation_id
    ):
        raise IndexManifestReadError("generation manifest digest is invalid")
    return manifest


def load_index_record(
    repository_root: str | Path,
    state: IndexedFileState,
    *,
    manifest: IndexManifest | None = None,
) -> bytes:
    """Read and digest-check one record from a caller-pinned active manifest."""

    active = manifest if manifest is not None else load_manifest(repository_root)
    indexed = next((item for item in active.files if item.path == state.path), None)
    if indexed is None or indexed != state:
        raise IndexManifestReadError(
            "record state is not present in the pinned manifest"
        )
    if state.record_location is None or state.record_sha256 is None:
        raise IndexManifestReadError("indexed state has no published record")
    layout = _layout(repository_root)
    generation = layout.generations / active.generation_id
    record = generation.joinpath(*state.record_location.split("/"))
    _require_safe_existing_chain(generation, record)
    content = _read_bounded_bytes(record, MAX_RECORD_BYTES)
    if hashlib.sha256(content).hexdigest() != state.record_sha256:
        raise IndexManifestReadError("published record digest does not match manifest")
    return content


def load_interpretation_record(
    repository_root: str | Path,
    state: IndexedFileState,
    *,
    manifest: IndexManifest | None = None,
) -> bytes:
    """Read one separately persisted, digest-checked semantic interpretation."""

    active = manifest if manifest is not None else load_manifest(repository_root)
    indexed = next((item for item in active.files if item.path == state.path), None)
    if indexed is None or indexed != state:
        raise IndexManifestReadError(
            "interpretation state is not present in the pinned manifest"
        )
    if (
        state.semantic_status != "complete"
        or state.interpretation_record_location is None
        or state.interpretation_record_sha256 is None
    ):
        raise IndexManifestReadError("indexed state has no complete interpretation")
    content = load_generation_record(
        repository_root,
        state.interpretation_record_location,
        manifest=active,
    )
    if hashlib.sha256(content).hexdigest() != state.interpretation_record_sha256:
        raise IndexManifestReadError(
            "published interpretation digest does not match manifest"
        )
    return content


def load_generation_record(
    repository_root: str | Path,
    record_location: str,
    *,
    manifest: IndexManifest,
) -> bytes:
    """Read one approved record from a caller-pinned immutable generation."""

    location = _validate_record_location(record_location)
    referenced = {
        candidate
        for state in manifest.files
        for candidate in (
            state.record_location,
            state.interpretation_record_location,
        )
        if candidate is not None
    }
    if location not in _STAGED_ROOT_RECORDS and location not in referenced:
        raise IndexManifestReadError(
            "record location is not referenced by the pinned manifest"
        )
    layout = _layout(repository_root)
    generation = layout.generations / manifest.generation_id
    record = generation.joinpath(*location.split("/"))
    _require_safe_existing_chain(generation, record)
    return _read_bounded_bytes(record, MAX_RECORD_BYTES)


def load_staged_index_record(
    lock: IndexWriteLock, record_location: str
) -> bytes | None:
    """Load a validated checkpoint from this writer's resumable staging area."""

    _require_lock(lock)
    location = _validate_record_location(record_location)
    stage = begin_index_build(lock)
    record = stage.joinpath(*location.split("/"))
    if not os.path.lexists(record):
        return None
    _require_safe_existing_chain(stage, record)
    return _read_bounded_bytes(record, MAX_RECORD_BYTES)


def inspect_index_status(
    repository_root: str | Path,
    current: ProjectSnapshot | Iterable[ProjectFile],
    *,
    expected_analyzer: AnalyzerIdentity,
    build_options_digest: str,
    schema_versions: SchemaVersionMetadata | None = None,
) -> IndexStatus:
    """Inspect current snapshot differences without building or calling a model."""

    layout = _layout(repository_root)
    initialized = layout.index.is_dir() and not _is_link_or_junction(layout.index)
    try:
        manifest = load_manifest(repository_root)
    except IndexManifestNotFoundError:
        manifest = None
    return compare_index_status(
        manifest,
        current,
        expected_analyzer=expected_analyzer,
        build_options_digest=build_options_digest,
        initialized=initialized,
        schema_versions=schema_versions,
    )


def cleanup_stale_temporary_files(lock: IndexWriteLock) -> tuple[str, ...]:
    """Remove only identifiable sibling temporary files beneath generated index data."""

    _require_lock(lock)
    removed: list[str] = []
    for root in (lock.layout.staging, lock.layout.generations):
        _collect_and_remove_temporaries(lock.layout.index, root, removed)
    return tuple(sorted(removed))


def clean_generated_index(
    repository_root: str | Path,
    *,
    run_id: str = "clean",
    recover_stale_lock: bool = False,
) -> None:
    """Reset generated index truth while preserving config, contexts, and runs."""

    with acquire_index_lock(
        repository_root,
        run_id,
        recover_stale=recover_stale_lock,
    ) as lock:
        if os.path.lexists(lock.layout.active_manifest):
            _require_regular_file(lock.layout.active_manifest)
            lock.layout.active_manifest.unlink()
        _clear_directory(lock.layout.staging)
        _clear_directory(lock.layout.generations)
        _fsync_directory(lock.layout.index)


def _layout(repository_root: str | Path) -> IndexLayout:
    root_path = Path(repository_root).expanduser()
    try:
        root = root_path.resolve(strict=True)
    except OSError as exc:
        raise IndexPathError("repository root is unavailable") from exc
    if not root.is_dir():
        raise IndexPathError("repository root is not a directory")
    contextforge_root = root / CONTEXTFORGE_DIRECTORY
    index = contextforge_root / INDEX_DIRECTORY
    return IndexLayout(
        repository_root=root,
        contextforge_root=contextforge_root,
        config=contextforge_root / CONFIG_FILENAME,
        index=index,
        active_manifest=index / ACTIVE_MANIFEST_FILENAME,
        lock=index / LOCK_FILENAME,
        staging=index / STAGING_DIRECTORY,
        generations=index / GENERATIONS_DIRECTORY,
        contexts=contextforge_root / CONTEXTS_DIRECTORY,
        runs=contextforge_root / RUNS_DIRECTORY,
    )


def _ensure_directory(path: Path) -> None:
    if os.path.lexists(path):
        _require_directory(path)
        return
    parent = path.parent
    if parent != path and not os.path.lexists(parent):
        _ensure_directory(parent)
    elif parent != path:
        _require_directory(parent)
    try:
        path.mkdir()
    except FileExistsError:
        _require_directory(path)
    except OSError as exc:
        raise IndexPathError(
            f"unable to create generated directory: {path.name}"
        ) from exc


def _ensure_directory_chain(base: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(base)
    except ValueError as exc:
        raise IndexPathError(
            "generated output path is outside its storage root"
        ) from exc
    current = base
    _require_directory(current)
    for part in relative.parts:
        current /= part
        _ensure_directory(current)


def _create_user_config_once(path: Path) -> None:
    if os.path.lexists(path):
        _require_regular_file(path)
        return
    encoded = DEFAULT_CONFIG.encode("utf-8")
    try:
        _atomic_create_bytes(path, encoded)
    except FileExistsError:
        _require_regular_file(path)
    except OSError as exc:
        raise IndexStorageError("unable to initialize user configuration") from exc


def _atomic_create_bytes(destination: Path, content: bytes) -> None:
    temporary = _write_temporary(destination, content)
    try:
        os.link(temporary, destination)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _replace_directory_for_publication(
    source: Path,
    destination: Path,
    *,
    replace: Callable[[Path, Path], None] | None = None,
    sleeper: Callable[[float], None] | None = None,
    retry_delays: tuple[float, ...] = WINDOWS_DIRECTORY_REPLACE_RETRY_DELAYS,
    platform: str | None = None,
) -> None:
    """Atomically rename a generation with narrowly bounded Windows retries.

    Windows can briefly deny a directory rename after all application handles
    are closed. Only documented sharing/access/lock errors are retryable, and
    the first such error remains the publication failure cause on exhaustion.
    """

    if len(retry_delays) > MAX_WINDOWS_DIRECTORY_REPLACE_RETRIES or any(
        not isinstance(delay, (int, float))
        or isinstance(delay, bool)
        or not math.isfinite(delay)
        or delay < 0
        or delay > 1
        for delay in retry_delays
    ):
        raise ValueError("directory publication retry delays are invalid")
    active_replace = os.replace if replace is None else replace
    active_sleeper = time.sleep if sleeper is None else sleeper
    active_platform = os.name if platform is None else platform
    first_error: OSError | None = None

    for attempt in range(len(retry_delays) + 1):
        try:
            active_replace(source, destination)
            return
        except OSError as exc:
            if not _is_retryable_directory_replace_error(exc, active_platform):
                raise
            if first_error is None:
                first_error = exc
            if attempt == len(retry_delays):
                if first_error is exc:
                    raise
                raise first_error from exc
            active_sleeper(retry_delays[attempt])
    raise AssertionError("bounded directory publication retry did not terminate")


def _is_retryable_directory_replace_error(error: OSError, platform: str) -> bool:
    return platform == "nt" and getattr(error, "winerror", None) in (
        _WINDOWS_RETRYABLE_DIRECTORY_REPLACE_ERRORS
    )


def _atomic_write_bytes[ErrorType: IndexStorageError](
    destination: Path,
    content: bytes,
    *,
    error_type: type[ErrorType],
) -> None:
    _require_directory(destination.parent)
    if os.path.lexists(destination) and destination.is_dir():
        raise error_type("generated output destination is a directory")
    temporary: Path | None = None
    try:
        temporary = _write_temporary(destination, content)
        _require_directory(destination.parent)
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise error_type(
            f"unable to publish generated file: {destination.name}"
        ) from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _write_temporary(destination: Path, content: bytes) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=TEMPORARY_SUFFIX,
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
    except BaseException:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise
    assert temporary_path is not None
    return temporary_path


def _validate_record_location(value: str) -> str:
    try:
        location = validate_portable_relative_path(value)
    except ValueError as exc:
        raise IndexPathError(
            "record location must be a safe generated relative path"
        ) from exc
    if location in (ACTIVE_MANIFEST_FILENAME, LOCK_FILENAME):
        raise IndexPathError("record location is reserved by index storage")
    allowed = location.startswith("files/") or location in _STAGED_ROOT_RECORDS
    if not allowed:
        raise IndexPathError("record location is outside approved generation data")
    return location


def _require_supported_schema_versions(versions: SchemaVersionMetadata) -> None:
    expected = SchemaVersionMetadata()
    for actual, supported in (
        (versions.index_schema_version, expected.index_schema_version),
        (versions.manifest_schema_version, expected.manifest_schema_version),
        (versions.record_schema_version, expected.record_schema_version),
    ):
        if actual != supported:
            raise UnsupportedIndexSchemaError(actual)


def _validate_generation_records(stage: Path, manifest: IndexManifest) -> None:
    _validate_generation_records_at(stage, manifest)


def _validate_generation(generation: Path, manifest: IndexManifest) -> None:
    manifest_path = generation / ACTIVE_MANIFEST_FILENAME
    _require_regular_file(manifest_path)
    expected = canonical_json_bytes(manifest.model_dump(mode="json"))
    actual = _read_bounded_bytes(manifest_path, MAX_MANIFEST_BYTES)
    if actual != expected:
        raise IndexPublicationError(
            "immutable generation manifest conflicts with content"
        )
    _validate_generation_records_at(generation, manifest)


def _validate_generation_records_at(root: Path, manifest: IndexManifest) -> None:
    for state in manifest.files:
        if state.record_location is None or state.record_sha256 is None:
            continue
        record = root.joinpath(*state.record_location.split("/"))
        _require_safe_existing_chain(root, record)
        content = _read_bounded_bytes(record, MAX_RECORD_BYTES)
        if hashlib.sha256(content).hexdigest() != state.record_sha256:
            raise IndexPublicationError(
                f"record digest does not match manifest for {state.path}"
            )
        if (
            state.interpretation_record_location is not None
            and state.interpretation_record_sha256 is not None
        ):
            interpretation = root.joinpath(
                *state.interpretation_record_location.split("/")
            )
            _require_safe_existing_chain(root, interpretation)
            interpretation_content = _read_bounded_bytes(
                interpretation, MAX_RECORD_BYTES
            )
            if (
                hashlib.sha256(interpretation_content).hexdigest()
                != state.interpretation_record_sha256
            ):
                raise IndexPublicationError(
                    f"interpretation digest does not match manifest for {state.path}"
                )


def _read_persisted_model[ModelType: BaseModel](
    path: Path,
    model: type[ModelType],
    *,
    expected_schema: int,
) -> ModelType:
    try:
        raw = _read_bounded_bytes(path, MAX_MANIFEST_BYTES).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IndexManifestReadError("index JSON is not valid UTF-8") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise IndexManifestReadError("index JSON is malformed") from exc
    if not isinstance(value, dict):
        raise IndexManifestReadError("index JSON root must be an object")
    schema_version = value.get("schema_version")
    if type(schema_version) is int and schema_version != expected_schema:
        raise UnsupportedIndexSchemaError(schema_version)
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise IndexManifestReadError("index JSON does not match its schema") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_bounded_bytes(path: Path, maximum: int) -> bytes:
    try:
        result = read_file_stably(path, max_size_bytes=maximum)
    except (OSError, ValueError) as exc:
        raise IndexManifestReadError(
            f"unable to read generated file: {path.name}"
        ) from exc
    return result.content


def _require_safe_existing_chain(base: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(base)
    except ValueError as exc:
        raise IndexPathError("generated path escapes its storage root") from exc
    current = base
    _require_directory(current)
    for index, part in enumerate(relative.parts):
        current /= part
        if index == len(relative.parts) - 1:
            _require_regular_file(current)
        else:
            _require_directory(current)


def _require_directory(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        junction = path.is_junction()
    except OSError as exc:
        raise IndexPathError(
            f"generated directory is unavailable: {path.name}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or junction or not stat.S_ISDIR(metadata.st_mode):
        raise IndexPathError(
            f"generated storage path is linked or not a directory: {path.name}"
        )


def _require_regular_file(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        junction = path.is_junction()
    except OSError as exc:
        raise IndexPathError(f"generated file is unavailable: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or junction or not stat.S_ISREG(metadata.st_mode):
        raise IndexPathError(
            f"generated storage path is linked or not a regular file: {path.name}"
        )


def _is_link_or_junction(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
        return stat.S_ISLNK(metadata.st_mode) or path.is_junction()
    except OSError:
        return False


def _create_lock_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise


def _recover_or_reject_lock(
    path: Path,
    *,
    recover_stale: bool,
    confirm_unknown: bool,
    process_is_running: Callable[[int], bool],
) -> None:
    before: os.stat_result | None = None
    try:
        before = path.stat(follow_symlinks=False)
        metadata = _read_lock_metadata(path)
    except FileNotFoundError:
        return
    except IndexLockError:
        if not confirm_unknown:
            raise IndexLockRecoveryRequiredError(
                "index lock metadata is unverifiable; explicit confirmation is required"
            ) from None
        _remove_recoverable_lock(path, before)
        return

    host = metadata["host_fingerprint"]
    pid = metadata["pid"]
    if host == _host_fingerprint():
        if process_is_running(pid):
            raise IndexLockActiveError(
                f"index writer is active for run {metadata['run_id']!r}"
            )
        if not recover_stale:
            raise IndexLockRecoveryRequiredError(
                "index lock belongs to a stopped same-host process; "
                "stale recovery is required"
            )
        _remove_recoverable_lock(path, before)
        return
    if not confirm_unknown:
        raise IndexLockRecoveryRequiredError(
            "index lock belongs to another host; explicit confirmation is required"
        )
    _remove_recoverable_lock(path, before)


def _read_lock_metadata(path: Path) -> dict[str, Any]:
    try:
        _require_regular_file(path)
        raw = read_file_stably(path, max_size_bytes=64 * 1024).content
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except FileNotFoundError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        IndexPathError,
    ) as exc:
        raise IndexLockError("index lock metadata is malformed") from exc
    required = {
        "schema_version",
        "run_id",
        "pid",
        "host_fingerprint",
        "started_at",
        "owner_nonce",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise IndexLockError("index lock metadata is malformed")
    if (
        value["schema_version"] != 1
        or type(value["pid"]) is not int
        or value["pid"] <= 0
    ):
        raise IndexLockError("index lock metadata is malformed")
    if not all(
        isinstance(value[key], str)
        for key in ("run_id", "host_fingerprint", "started_at", "owner_nonce")
    ):
        raise IndexLockError("index lock metadata is malformed")
    return value


def _remove_recoverable_lock(path: Path, expected: os.stat_result | None) -> None:
    if expected is not None:
        try:
            current = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        if not os.path.samestat(expected, current):
            raise IndexLockError("index lock changed during recovery")
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IndexLockError("unable to recover index lock") from exc


def _process_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _host_fingerprint() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()


def _validate_run_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or len(value) > 128
        or value[0] not in _RUN_ID_CHARACTERS
        or any(character not in _RUN_ID_CHARACTERS for character in value)
    ):
        raise IndexLockError("run_id must be a bounded portable identifier")
    return value


def _require_lock(lock: IndexWriteLock) -> None:
    if not isinstance(lock, IndexWriteLock) or not lock.active:
        raise IndexLockOwnershipError("an active index writer lock is required")
    try:
        metadata = _read_lock_metadata(lock.layout.lock)
    except (FileNotFoundError, IndexLockError) as exc:
        raise IndexLockOwnershipError(
            "index writer lock is no longer available"
        ) from exc
    if (
        metadata.get("owner_nonce") != lock.owner_nonce
        or metadata.get("run_id") != lock.run_id
    ):
        raise IndexLockOwnershipError("index writer lock is owned by another writer")


def _collect_and_remove_temporaries(base: Path, root: Path, removed: list[str]) -> None:
    _require_directory(root)
    for entry in os.scandir(root):
        path = Path(entry.path)
        if entry.is_symlink() or path.is_junction():
            continue
        if entry.is_dir(follow_symlinks=False):
            _collect_and_remove_temporaries(base, path, removed)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(
            TEMPORARY_SUFFIX
        ):
            relative = path.relative_to(base).as_posix()
            path.unlink()
            removed.append(relative)


def _clear_directory(root: Path) -> None:
    _require_directory(root)
    for entry in os.scandir(root):
        path = Path(entry.path)
        if entry.is_symlink() or path.is_junction():
            if entry.is_dir(follow_symlinks=False):
                os.rmdir(path)
            else:
                path.unlink()
        elif entry.is_dir(follow_symlinks=False):
            shutil.rmtree(path)
        else:
            path.unlink()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
