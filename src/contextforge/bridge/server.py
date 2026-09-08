"""Persistent bounded JSON-RPC 2.0 service over NDJSON streams."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import unicodedata
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from pydantic import BaseModel, ValidationError

from contextforge._metadata import __version__
from contextforge.application import (
    ApplicationError,
    IndexSourceChangedError,
    build_repository_index,
    inspect_repository_index,
)
from contextforge.context import (
    ContextLimitError,
    ContextReaderError,
    FileChangedError,
    InvalidLineRangeError,
)
from contextforge.core.validation import validate_portable_relative_path
from contextforge.discovery import (
    DiscoveryExpansionOperation,
    DiscoveryExpansionRequest,
    DiscoveryRequest,
    DiscoverySelection,
    DiscoverySelectionItem,
)
from contextforge.discovery.application import (
    DiscoveryApplicationError,
    DiscoveryPreparationMismatchError,
    expand_discovery,
    package_verified_context,
    prepare_discovery_candidates,
    read_verified_context,
)
from contextforge.discovery.models import (
    DiscoveryCandidatePreparation,
    PreparedDiscoveryCandidate,
)
from contextforge.discovery.tools import TOOL_INPUT_MODELS
from contextforge.intelligence import (
    INDEX_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    RECORD_SCHEMA_VERSION,
    GlobalMapAnalysisError,
    IndexLockError,
    IndexStorageError,
    SemanticAnalysisError,
    SemanticFailureLimitError,
    SemanticProviderCircuitError,
    calculate_source_snapshot_digest,
    canonical_json_bytes,
    load_file_code_map,
    load_file_semantic_analysis,
    load_manifest,
)
from contextforge.models import (
    ModelProvider,
    ModelProviderError,
    ProviderConfigurationError,
    RetryClassification,
    classify_retry,
    provider_error_details,
)
from contextforge.progress import PROGRESS_SCHEMA_VERSION, ProgressEvent
from contextforge.project_config import (
    ProjectConfigError,
    create_model_provider,
    load_project_configuration,
    resolve_provider_configuration,
)
from contextforge.repositories import ProjectSnapshot, ScanOptions, scan_repository

from .models import (
    BridgeSelectionItem,
    CancelParams,
    DiscoverParams,
    ExpandParams,
    ExpansionOperation,
    HelloParams,
    IndexParams,
    PackageParams,
    ReadParams,
    ShutdownParams,
    SnapshotParams,
    StatusParams,
)
from .protocol import BRIDGE_PROTOCOL_VERSION, SUPPORTED_BRIDGE_PROTOCOL_VERSIONS

JSONRPC_VERSION = "2.0"
MAX_JSONRPC_MESSAGE_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_PREPARATIONS = 128
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
FORCED_CANCELLATION_TIMEOUT_SECONDS = 0.1
MAX_PENDING_PROGRESS_EVENTS = 256
PROGRESS_BACKPRESSURE_TIMEOUT_SECONDS = 5.0

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800
SOURCE_IDENTITY_CHANGED = -32001
MESSAGE_TOO_LARGE = -32002
DUPLICATE_REQUEST_ID = -32003
REQUEST_TIMEOUT = -32004
SHUTTING_DOWN = -32005
INCOMPATIBLE_PROTOCOL_VERSION = -32006
PROTOCOL_NEGOTIATION_REQUIRED = -32007
INDEX_BUILD_FAILED = -32008
PROVIDER_FAILURE = -32009
FAILURE_LIMIT_REACHED = -32010
PROVIDER_CIRCUIT_OPEN = -32011
INDEX_STORAGE_FAILURE = -32012
INDEX_LOCKED = -32013

_METHOD_MODELS: dict[str, type[BaseModel]] = {
    "hello": HelloParams,
    "status": StatusParams,
    "snapshot": SnapshotParams,
    "index": IndexParams,
    "discover": DiscoverParams,
    "expand": ExpandParams,
    "read": ReadParams,
    "package": PackageParams,
    "$/cancelRequest": CancelParams,
    "shutdown": ShutdownParams,
}

_EXPANSION_TOOLS: dict[ExpansionOperation, DiscoveryExpansionOperation] = {
    "symbol": "search_symbols",
    "text": "search_text",
    "callers": "find_callers",
    "importers": "find_importers",
    "related_tests": "find_related_tests",
}


class BridgeFault(RuntimeError):
    """One safe typed JSON-RPC failure."""

    def __init__(
        self,
        rpc_code: int,
        typed_code: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.rpc_code = rpc_code
        self.typed_code = typed_code
        self.message = message
        self.data = data or {}


@dataclass(slots=True)
class _ActiveRequest:
    cancellation: asyncio.Event
    task: asyncio.Task[None]


class _SerializedWriter:
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = asyncio.Lock()

    async def write(self, frame: dict[str, Any]) -> None:
        payload = (
            json.dumps(
                frame,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        async with self._lock:
            await asyncio.to_thread(self._write, payload)

    def _write(self, payload: bytes) -> None:
        self._stream.write(payload)
        self._stream.flush()


class _ProgressPublisher:
    """Serialize one bounded progress stream with coalesced producer bursts."""

    def __init__(
        self,
        writer: _SerializedWriter,
        request_id: str | int,
        cancellation: asyncio.Event,
        operation_id: str,
        *,
        capacity: int = MAX_PENDING_PROGRESS_EVENTS,
        backpressure_timeout_seconds: float = PROGRESS_BACKPRESSURE_TIMEOUT_SECONDS,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if (
            not math.isfinite(backpressure_timeout_seconds)
            or backpressure_timeout_seconds <= 0
        ):
            raise ValueError("backpressure_timeout_seconds must be finite and positive")
        self.last_event: ProgressEvent | None = None
        self._writer = writer
        self._request_id = request_id
        self._cancellation = cancellation
        self._operation_id = operation_id
        self._queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue(
            maxsize=capacity
        )
        self._backpressure_timeout_seconds = backpressure_timeout_seconds
        self._pending_event: ProgressEvent | None = None
        self._pending_task: asyncio.Task[None] | None = None
        self._overflowed = False
        self._closed = False
        self._task = asyncio.create_task(self._run())

    def observe(self, event: ProgressEvent) -> None:
        self.last_event = event
        if self._closed or self._overflowed or self._cancellation.is_set():
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Progress events are cumulative snapshots. Keep only the newest event
            # while one bounded enqueue waits for the writer. This distinguishes a
            # synchronous producer burst from a client that is actually not reading.
            self._pending_event = event
            if self._pending_task is None or self._pending_task.done():
                self._pending_task = asyncio.create_task(self._enqueue_pending())

    async def close(self, *, check_overflow: bool = True) -> None:
        if not self._closed:
            self._closed = True
            if self._pending_task is not None:
                await self._pending_task
            if not self._task.done():
                stopper = asyncio.create_task(self._queue.put(None))
                try:
                    done, _ = await asyncio.wait(
                        {self._task, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if self._task in done:
                        stopper.cancel()
                finally:
                    if not stopper.done():
                        stopper.cancel()
                    await asyncio.gather(stopper, return_exceptions=True)
        await self._task
        if check_overflow and self._overflowed:
            raise BridgeFault(
                INDEX_BUILD_FAILED,
                "INDEX_BUILD_FAILED",
                "The client did not consume index progress quickly enough.",
                data={
                    "error_code": "progress_backpressure",
                    "phase": (
                        "initialize"
                        if self.last_event is None
                        else self.last_event.phase_id
                    ),
                    "reason": "The Bridge progress delivery queue reached its limit.",
                    "retryable": True,
                    "operation_id": self._operation_id,
                },
            )

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                if not self._cancellation.is_set():
                    await self._writer.write(
                        {
                            "jsonrpc": JSONRPC_VERSION,
                            "method": "$/progress",
                            "params": {
                                "request_id": self._request_id,
                                "event": event.model_dump(mode="json"),
                            },
                        }
                    )
            finally:
                self._queue.task_done()

    async def _enqueue_pending(self) -> None:
        try:
            while self._pending_event is not None and not self._overflowed:
                event = self._pending_event
                self._pending_event = None
                put_task = asyncio.create_task(self._queue.put(event))
                cancellation_task = asyncio.create_task(self._cancellation.wait())
                done: set[asyncio.Task[Any]] = set()
                try:
                    done, _ = await asyncio.wait(
                        {put_task, cancellation_task},
                        timeout=self._backpressure_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (put_task, cancellation_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(
                        put_task, cancellation_task, return_exceptions=True
                    )
                if cancellation_task in done:
                    return
                if put_task not in done:
                    self._overflowed = True
                    self._cancellation.set()
                    return
                put_task.result()
        finally:
            self._pending_task = None
            if (
                self._pending_event is not None
                and not self._overflowed
                and not self._cancellation.is_set()
            ):
                self._pending_task = asyncio.create_task(self._enqueue_pending())


class _BoundedDiagnostics:
    def __init__(self, stream: TextIO | None) -> None:
        self._stream = stream
        self._remaining = MAX_DIAGNOSTIC_BYTES

    def write(self, message: str) -> None:
        if self._stream is None or self._remaining <= 0:
            return
        safe = message.replace("\r", " ").replace("\n", " ")[:2000] + "\n"
        encoded = safe.encode("utf-8")[: self._remaining]
        self._remaining -= len(encoded)
        self._stream.write(encoded.decode("utf-8", errors="ignore"))
        self._stream.flush()


class BridgeServer:
    """Workspace-bound persistent ContextForge bridge protocol server."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        root = Path(workspace).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        if not math.isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be finite and positive")
        self.workspace = root
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.workspace_identity = _workspace_identity(root)
        self.config_digest = hashlib.sha256(
            canonical_json_bytes(
                {"scan_options": ScanOptions().model_dump(mode="json")}
            )
        ).hexdigest()
        self._snapshot_digest: str | None = None
        self._preparations: OrderedDict[str, DiscoveryCandidatePreparation] = (
            OrderedDict()
        )
        self._active: dict[tuple[str, str | int], _ActiveRequest] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._writer: _SerializedWriter | None = None
        self._diagnostics = _BoundedDiagnostics(None)
        self._shutting_down = False
        self._protocol_negotiated = False
        self._protocol_version: str | None = None

    async def serve(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        error_stream: TextIO | None = None,
    ) -> None:
        """Serve bounded NDJSON until shutdown or a clean input EOF."""

        self._writer = _SerializedWriter(output_stream)
        self._diagnostics = _BoundedDiagnostics(error_stream)
        try:
            while not self._shutting_down:
                line, oversized = await _read_bounded_line(input_stream)
                if line is None:
                    break
                if oversized:
                    await self._write_error(
                        None,
                        BridgeFault(
                            MESSAGE_TOO_LARGE,
                            "MESSAGE_TOO_LARGE",
                            "JSON-RPC message exceeds the configured byte limit.",
                            data={"max_message_bytes": MAX_JSONRPC_MESSAGE_BYTES},
                        ),
                    )
                    continue
                frame = self._decode_frame(line)
                if isinstance(frame, BridgeFault):
                    await self._write_error(None, frame)
                    continue
                if frame.get("method") == "shutdown":
                    await self._accept_request(frame, inline=True)
                    break
                await self._accept_request(frame, inline=False)
        finally:
            self._begin_shutdown()
            await self._drain_active_requests()

    def _begin_shutdown(self) -> None:
        """Stop new work and request cooperative cancellation of active work."""

        self._shutting_down = True
        current = asyncio.current_task()
        for request in tuple(self._active.values()):
            if request.task is not current:
                request.cancellation.set()

    async def _drain_active_requests(self) -> None:
        """Drain active work within the fixed graceful-shutdown budget."""

        current = asyncio.current_task()
        tasks = {
            request.task
            for request in tuple(self._active.values())
            if request.task is not current and not request.task.done()
        }
        tasks.update(
            task
            for task in tuple(self._background_tasks)
            if task is not current and not task.done()
        )
        if not tasks:
            return
        done, pending = await asyncio.wait(
            tasks,
            timeout=self.shutdown_timeout_seconds,
        )
        for task in done:
            self._consume_task_result(task)
        if not pending:
            self._forget_active_tasks(tasks)
            return
        self._diagnostics.write(
            f"bridge shutdown deadline expired with {len(pending)} active request(s)"
        )
        for task in pending:
            task.cancel()
        done, pending = await asyncio.wait(
            pending,
            timeout=FORCED_CANCELLATION_TIMEOUT_SECONDS,
        )
        for task in done:
            self._consume_task_result(task)
        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_task_result)
        self._forget_active_tasks(tasks)

    def _consume_task_result(self, task: asyncio.Task[Any]) -> None:
        """Consume a shutdown outcome and retain a safe unexpected-failure signal."""

        if task.cancelled():
            return
        if task.exception() is not None:
            self._diagnostics.write(
                "bridge active request ended unexpectedly during shutdown"
            )

    def _forget_active_tasks(self, tasks: set[asyncio.Task[Any]]) -> None:
        """Detach drained or abandoned requests from the bridge lifecycle."""

        for key, request in tuple(self._active.items()):
            if request.task in tasks:
                self._active.pop(key, None)
        self._background_tasks.difference_update(tasks)

    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        """Retain timed-out index cleanup until its writer lock is released."""

        self._background_tasks.add(task)
        task.add_done_callback(self._finish_background_task)

    def _finish_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not isinstance(error, BridgeFault):
            self._diagnostics.write(
                "bridge timed-out index cleanup ended with an internal error"
            )

    def _decode_frame(self, line: bytes) -> dict[str, Any] | BridgeFault:
        try:
            value = json.loads(
                line.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return BridgeFault(PARSE_ERROR, "MALFORMED_JSON", "Malformed JSON input.")
        if not isinstance(value, dict):
            return BridgeFault(
                INVALID_REQUEST,
                "INVALID_REQUEST",
                "JSON-RPC request must be an object.",
            )
        return value

    async def _accept_request(self, frame: dict[str, Any], *, inline: bool) -> None:
        try:
            method, request_id, params = _validate_envelope(frame)
        except BridgeFault as exc:
            await self._write_error(_safe_frame_id(frame), exc)
            return

        if self._shutting_down:
            await self._write_error(
                request_id,
                BridgeFault(
                    SHUTTING_DOWN,
                    "SHUTTING_DOWN",
                    "Bridge shutdown has started.",
                ),
            )
            return

        if method == "$/cancelRequest":
            await self._cancel_request(params, request_id)
            return

        assert request_id is not None
        key = _id_key(request_id)
        if key in self._active:
            await self._write_error(
                request_id,
                BridgeFault(
                    DUPLICATE_REQUEST_ID,
                    "DUPLICATE_REQUEST_ID",
                    "A request with this ID is already active.",
                ),
            )
            return

        cancellation = asyncio.Event()
        task = asyncio.create_task(
            self._process_request(method, request_id, params, cancellation)
        )
        self._active[key] = _ActiveRequest(cancellation=cancellation, task=task)
        if inline:
            await task

    async def _cancel_request(
        self, raw_params: dict[str, Any], request_id: str | int | None
    ) -> None:
        try:
            params = CancelParams.model_validate_json(
                json.dumps(raw_params, ensure_ascii=False, allow_nan=False)
            )
        except ValidationError as exc:
            if request_id is not None:
                await self._write_validation_error(request_id, exc)
            return
        active = self._active.get(_id_key(params.id))
        if active is not None:
            active.cancellation.set()
        if request_id is not None:
            await self._write_result(request_id, {"cancelled": active is not None})

    async def _process_request(
        self,
        method: str,
        request_id: str | int,
        raw_params: dict[str, Any],
        cancellation: asyncio.Event,
    ) -> None:
        negotiated_protocol = False
        try:
            model = _METHOD_MODELS[method]
            try:
                params = model.model_validate_json(
                    json.dumps(raw_params, ensure_ascii=False, allow_nan=False)
                )
            except ValidationError as exc:
                await self._write_validation_error(request_id, exc)
                return
            if method == "hello":
                hello = _require_type(params, HelloParams)
                if hello.protocol_version not in SUPPORTED_BRIDGE_PROTOCOL_VERSIONS:
                    raise BridgeFault(
                        INCOMPATIBLE_PROTOCOL_VERSION,
                        "INCOMPATIBLE_PROTOCOL_VERSION",
                        "The requested bridge protocol version is not supported.",
                        data={
                            "requested_protocol_version": hello.protocol_version,
                            "supported_protocol_versions": list(
                                SUPPORTED_BRIDGE_PROTOCOL_VERSIONS
                            ),
                        },
                    )
                negotiated_protocol = True
                self._protocol_version = hello.protocol_version
            elif method != "shutdown" and not self._protocol_negotiated:
                raise BridgeFault(
                    PROTOCOL_NEGOTIATION_REQUIRED,
                    "PROTOCOL_NEGOTIATION_REQUIRED",
                    "Call hello with a supported protocol_version first.",
                    data={
                        "supported_protocol_versions": list(
                            SUPPORTED_BRIDGE_PROTOCOL_VERSIONS
                        )
                    },
                )
            timeout_ms = getattr(params, "timeout_ms", None)
            operation = self._dispatch(method, request_id, params, cancellation)
            if timeout_ms is None:
                result = await operation
            else:
                operation_task = asyncio.create_task(operation)
                done, _ = await asyncio.wait(
                    {operation_task}, timeout=timeout_ms / 1000
                )
                if operation_task in done:
                    result = operation_task.result()
                else:
                    cancellation.set()
                    if method == "index":
                        self._track_background_task(operation_task)
                    else:
                        operation_task.cancel()
                        await asyncio.gather(operation_task, return_exceptions=True)
                    raise BridgeFault(
                        REQUEST_TIMEOUT,
                        "REQUEST_TIMEOUT",
                        "The request exceeded its caller-supplied timeout.",
                    ) from None
            if cancellation.is_set():
                raise asyncio.CancelledError
            if negotiated_protocol:
                self._protocol_negotiated = True
            await self._write_result(request_id, result)
        except asyncio.CancelledError:
            await self._write_error(
                request_id,
                BridgeFault(
                    REQUEST_CANCELLED,
                    "REQUEST_CANCELLED",
                    "The request was cancelled.",
                ),
            )
        except BridgeFault as exc:
            await self._write_error(request_id, exc)
        except (DiscoveryPreparationMismatchError, FileChangedError):
            await self._write_error(
                request_id,
                BridgeFault(
                    SOURCE_IDENTITY_CHANGED,
                    "SOURCE_IDENTITY_CHANGED",
                    "Repository source identity changed during the operation.",
                ),
            )
        except ContextLimitError:
            await self._write_error(
                request_id,
                BridgeFault(
                    INVALID_PARAMS,
                    "RESOURCE_LIMIT_EXCEEDED",
                    "The requested source excerpt exceeds an effective limit.",
                ),
            )
        except (ContextReaderError, InvalidLineRangeError):
            await self._write_error(
                request_id,
                BridgeFault(
                    INVALID_PARAMS,
                    "INVALID_SOURCE_RANGE",
                    "The requested source path or line range is invalid.",
                ),
            )
        except DiscoveryApplicationError:
            await self._write_error(
                request_id,
                BridgeFault(
                    INVALID_PARAMS,
                    "APPLICATION_REQUEST_REJECTED",
                    "The application operation rejected the request.",
                ),
            )
        except Exception:
            self._diagnostics.write("bridge request failed with an internal error")
            await self._write_error(
                request_id,
                BridgeFault(
                    INTERNAL_ERROR,
                    "INTERNAL_ERROR",
                    "ContextForge could not complete the request.",
                ),
            )
        finally:
            self._active.pop(_id_key(request_id), None)

    async def _dispatch(
        self,
        method: str,
        request_id: str | int,
        raw: BaseModel,
        cancellation: asyncio.Event,
    ) -> dict[str, Any]:
        if cancellation.is_set():
            raise asyncio.CancelledError
        if method == "hello":
            return self._hello()
        if method == "status":
            return await self._status(_require_type(raw, StatusParams), cancellation)
        if method == "snapshot":
            return await self._snapshot(cancellation)
        if method == "index":
            if self._protocol_version != "2.0":
                raise BridgeFault(
                    METHOD_NOT_FOUND,
                    "METHOD_NOT_FOUND",
                    "The requested method is not supported by this protocol version.",
                )
            return await self._index(
                request_id, _require_type(raw, IndexParams), cancellation
            )
        if method == "discover":
            return await self._discover(
                _require_type(raw, DiscoverParams), cancellation
            )
        if method == "expand":
            return await self._expand(_require_type(raw, ExpandParams), cancellation)
        if method == "read":
            return await self._read(_require_type(raw, ReadParams), cancellation)
        if method == "package":
            return await self._package(_require_type(raw, PackageParams), cancellation)
        if method == "shutdown":
            self._begin_shutdown()
            return {"shutdown": True}
        raise BridgeFault(
            METHOD_NOT_FOUND,
            "METHOD_NOT_FOUND",
            "The requested method is not supported.",
        )

    def _hello(self) -> dict[str, Any]:
        bridge_v2 = self._protocol_version == "2.0"
        return {
            "protocol_version": self._protocol_version or BRIDGE_PROTOCOL_VERSION,
            "supported_protocol_versions": list(SUPPORTED_BRIDGE_PROTOCOL_VERSIONS),
            "contextforge_version": __version__,
            "capabilities": {
                "methods": [
                    "hello",
                    "status",
                    "snapshot",
                    *(["index"] if bridge_v2 else []),
                    "discover",
                    "expand",
                    "read",
                    "package",
                    "$/cancelRequest",
                    "shutdown",
                ],
                "expansion_operations": sorted(_EXPANSION_TOOLS),
                "model_free_discovery": True,
                "cooperative_cancellation": True,
                "concurrent_requests": True,
                "serialized_responses": True,
                "max_message_bytes": MAX_JSONRPC_MESSAGE_BYTES,
                "expansion_candidates": self._protocol_version in {"1.1", "2.0"},
                "tracked_index_jobs": bridge_v2,
                "progress_notifications": bridge_v2,
                "schemas": {
                    "index": {
                        "current": INDEX_SCHEMA_VERSION,
                        "readable": [1, INDEX_SCHEMA_VERSION],
                    },
                    "manifest": {
                        "current": MANIFEST_SCHEMA_VERSION,
                        "readable": [1, MANIFEST_SCHEMA_VERSION],
                    },
                    "record": {
                        "current": RECORD_SCHEMA_VERSION,
                        "readable": [1, RECORD_SCHEMA_VERSION],
                    },
                    "progress": {
                        "current": PROGRESS_SCHEMA_VERSION,
                        "readable": [1, 2, PROGRESS_SCHEMA_VERSION],
                    },
                    "context_package": {"current": 1, "readable": [1]},
                },
            },
            "workspace": {
                "identity": self.workspace_identity,
            },
            "policy": {
                "repository_access": (
                    "verified_snapshot_and_atomic_index_write"
                    if bridge_v2
                    else "read_only_verified_snapshot"
                ),
                "external_data": "provider_policy" if bridge_v2 else "disabled",
                "portable_paths_only": True,
                "source_writes": False,
                "index_mutation": bridge_v2,
                "shell": False,
                "subprocess_execution": False,
            },
        }

    async def _status(
        self, params: StatusParams, cancellation: asyncio.Event
    ) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(scan_repository, self.workspace)
        if cancellation.is_set():
            raise asyncio.CancelledError
        digest = calculate_source_snapshot_digest(snapshot)
        provider_configuration = None
        try:
            project = load_project_configuration(self.workspace)
            provider_configuration = resolve_provider_configuration(project)
        except (ProjectConfigError, ValueError):
            pass
        report = await asyncio.to_thread(
            inspect_repository_index,
            self.workspace,
            provider_configuration=provider_configuration,
        )
        if cancellation.is_set():
            raise asyncio.CancelledError
        result: dict[str, Any] = {
            "ready": not self._shutting_down,
            "snapshot_digest": self._snapshot_digest,
            "current_snapshot_digest": digest,
            "source_identity_changed": (
                params.expected_snapshot_digest is not None
                and params.expected_snapshot_digest != digest
            ),
            "index": {
                "initialized": report.initialized,
                "index_schema": report.index_schema,
                "active_generation_id": report.active_generation_id,
                "indexed_files": report.indexed_files,
                "stale_files": list(report.stale_files),
                "failed_files": list(report.failed_files),
                "deleted_records": list(report.deleted_records),
                "added_files": list(report.added_files),
                "changed_files": list(report.changed_files),
                "provider_id": report.provider_id,
                "model_id": report.model_id,
                "prompt_versions": list(report.prompt_versions),
                "global_maps": {
                    "overview": report.overview_status,
                    "architecture": report.architecture_status,
                    "features": report.feature_status,
                },
                "lock_status": report.lock_status,
            },
        }
        if self._protocol_version in {"1.1", "2.0"}:
            result["index"]["coverage"] = await asyncio.to_thread(self._index_coverage)
        return result

    async def _snapshot(self, cancellation: asyncio.Event) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(scan_repository, self.workspace)
        if cancellation.is_set():
            raise asyncio.CancelledError
        digest = calculate_source_snapshot_digest(snapshot)
        if digest != self._snapshot_digest:
            self._preparations.clear()
        self._snapshot_digest = digest
        response = {
            "snapshot_digest": digest,
            "file_count": len(snapshot.files),
            "source_bytes": sum(item.size_bytes for item in snapshot.files),
            "languages": dict(sorted(snapshot.summary.languages.items())),
        }
        return response

    async def _index(
        self,
        request_id: str | int,
        params: IndexParams,
        cancellation: asyncio.Event,
    ) -> dict[str, Any]:
        operation_id = (
            "bridge-index-"
            + hashlib.sha256(
                f"{type(request_id).__name__}:{request_id}".encode()
            ).hexdigest()[:24]
        )
        provider: ModelProvider | None = None
        publisher = _ProgressPublisher(
            self._require_writer(), request_id, cancellation, operation_id
        )

        try:
            snapshot = await asyncio.to_thread(scan_repository, self.workspace)
            current_digest = calculate_source_snapshot_digest(snapshot)
            if current_digest != params.expected_snapshot_digest:
                raise _index_bridge_fault(
                    IndexSourceChangedError(
                        "repository source identity differs from expected snapshot"
                    ),
                    publisher.last_event,
                    operation_id,
                )
            try:
                project = load_project_configuration(self.workspace)
                configuration = resolve_provider_configuration(
                    project,
                    provider=params.provider,
                    model=params.model,
                    base_url=params.base_url,
                    concurrency=params.concurrency,
                    timeout_seconds=params.request_timeout,
                    operation_timeout_seconds=params.request_timeout,
                    context_window=params.context_window,
                    json_repair_attempts=params.json_repair_attempts,
                    local_only=True if params.local_only else None,
                )
                if configuration is not None:
                    provider = create_model_provider(configuration)
            except (ProjectConfigError, ValueError):
                raise _index_bridge_fault(
                    ProviderConfigurationError(
                        "provider configuration could not be resolved"
                    ),
                    publisher.last_event,
                    operation_id,
                ) from None
            concurrency = (
                configuration.concurrency_limit
                if configuration is not None
                else (
                    project.models.concurrency_limit
                    if params.concurrency is None
                    else params.concurrency
                )
            )
            report = await build_repository_index(
                self.workspace,
                provider=provider,
                provider_configuration=configuration,
                update_only=params.action == "update",
                concurrency=concurrency,
                fail_on_error=params.fail_on_error,
                fail_fast=params.fail_fast,
                max_failures=params.max_failures,
                force_reanalyze=params.force_reanalyze,
                max_files=params.max_files,
                semantic_max_output_tokens=(
                    project.models.semantic_max_output_tokens
                    if params.max_output_tokens is None
                    else params.max_output_tokens
                ),
                recover_stale_lock=params.recover_stale_lock,
                confirm_unknown_lock=params.confirm_unknown_lock,
                progress=publisher.observe,
                operation_id=operation_id,
                cancellation=cancellation,
                expected_snapshot_digest=params.expected_snapshot_digest,
            )
            await publisher.close()
            self._snapshot_digest = report.manifest.build.source_snapshot_digest
            self._preparations.clear()
            return {
                "action": params.action,
                "generation_id": report.manifest.generation_id,
                "snapshot_digest": report.manifest.build.source_snapshot_digest,
                "index_schema": report.manifest.schema_versions.index_schema_version,
                "partial": report.partial,
                "statistics": report.manifest.statistics.model_dump(mode="json"),
            }
        except asyncio.CancelledError:
            await publisher.close()
            raise
        except BridgeFault:
            await publisher.close(check_overflow=False)
            raise
        except (
            ApplicationError,
            GlobalMapAnalysisError,
            IndexStorageError,
            ModelProviderError,
            ProjectConfigError,
            SemanticAnalysisError,
            ValueError,
        ) as exc:
            await publisher.close(check_overflow=False)
            raise _index_bridge_fault(exc, publisher.last_event, operation_id) from None
        finally:
            with suppress(Exception, asyncio.CancelledError):
                await publisher.close(check_overflow=False)
            if provider is not None:
                with suppress(ModelProviderError):
                    await provider.close()

    async def _discover(
        self, params: DiscoverParams, cancellation: asyncio.Event
    ) -> dict[str, Any]:
        snapshot = await self._verified_snapshot(
            params.expected_snapshot_digest, cancellation
        )
        request = DiscoveryRequest(
            task=params.task,
            mode=params.mode,
            pinned_paths=params.pinned_paths,
            excluded_paths=params.excluded_paths,
            strict=params.strict,
            budget=params.budget,
        )
        preparation = await asyncio.to_thread(
            prepare_discovery_candidates,
            snapshot,
            request,
            cancellation=cancellation,
        )
        self._remember_preparation(preparation)
        response = {
            "preparation_id": preparation.preparation_id,
            "source_snapshot_digest": preparation.source_snapshot_digest,
            "mode": preparation.mode.value,
            "index_generation_id": preparation.index_generation_id,
            "index_status": preparation.index_status,
            "candidates": [
                item.model_dump(mode="json") for item in preparation.candidates
            ],
            "total_candidate_count": preparation.total_candidate_count,
            "stale_index_paths": list(preparation.stale_index_paths),
            "warnings": [item.model_dump(mode="json") for item in preparation.warnings],
            "budget_usage": preparation.budget_usage.model_dump(mode="json"),
            "config_digest": self.config_digest,
            "model_provider_used": False,
        }
        return response

    async def _expand(
        self, params: ExpandParams, cancellation: asyncio.Event
    ) -> dict[str, Any]:
        snapshot = await self._verified_snapshot(
            params.expected_snapshot_digest, cancellation
        )
        preparation = self._require_preparation(params.preparation_id)
        tool_name = _EXPANSION_TOOLS[params.operation]
        self._validate_expansion_arguments(tool_name, params.arguments)
        expansion = DiscoveryExpansionRequest(
            preparation_id=preparation.preparation_id,
            operation=tool_name,
            arguments=params.arguments,
            budget_usage=params.budget_usage,
        )
        result = await asyncio.to_thread(
            expand_discovery,
            snapshot,
            preparation,
            expansion,
            cancellation=cancellation,
        )
        response = {
            "preparation_id": result.preparation_id,
            "operation": params.operation,
            "ok": result.ok,
            "code": result.code,
            "data": result.data,
            "truncated": result.truncated,
            "made_progress": result.made_progress,
            "budget_usage": result.budget_usage.model_dump(mode="json"),
        }
        if self._protocol_version in {"1.1", "2.0"}:
            response["candidates"] = self._register_expansion_candidates(
                snapshot, preparation, params.operation, result.data
            )
        return response

    async def _read(
        self, params: ReadParams, cancellation: asyncio.Event
    ) -> dict[str, Any]:
        snapshot = await self._verified_snapshot(
            params.expected_snapshot_digest, cancellation
        )
        preparation = self._require_preparation(params.preparation_id)
        selection = self._selection(preparation, params.items)
        verified = await asyncio.to_thread(
            read_verified_context,
            snapshot,
            preparation,
            selection,
            cancellation=cancellation,
        )
        return {
            "preparation_id": verified.preparation_id,
            "selection_id": self._selection_id(preparation, params.items),
            "source_snapshot_digest": verified.source_snapshot_digest,
            "files": [item.model_dump(mode="json") for item in verified.files],
            "budget_usage": verified.budget_usage.model_dump(mode="json"),
        }

    async def _package(
        self, params: PackageParams, cancellation: asyncio.Event
    ) -> dict[str, Any]:
        snapshot = await self._verified_snapshot(
            params.expected_snapshot_digest, cancellation
        )
        preparation = self._require_preparation(params.preparation_id)
        selection = self._selection(preparation, params.items)
        verified = await asyncio.to_thread(
            read_verified_context,
            snapshot,
            preparation,
            selection,
            cancellation=cancellation,
        )
        package = await asyncio.to_thread(
            package_verified_context,
            snapshot,
            verified,
            include_tree=params.include_tree,
            cancellation=cancellation,
        )
        return {
            "selection_id": self._selection_id(preparation, params.items),
            "package": package.model_dump(mode="json"),
        }

    async def _verified_snapshot(
        self, expected: str, cancellation: asyncio.Event
    ) -> ProjectSnapshot:
        if self._snapshot_digest is None:
            raise BridgeFault(
                INVALID_PARAMS,
                "SNAPSHOT_REQUIRED",
                "Call snapshot before repository-sensitive methods.",
            )
        snapshot = await asyncio.to_thread(scan_repository, self.workspace)
        if cancellation.is_set():
            raise asyncio.CancelledError
        current = calculate_source_snapshot_digest(snapshot)
        if expected != self._snapshot_digest or current != expected:
            raise BridgeFault(
                SOURCE_IDENTITY_CHANGED,
                "SOURCE_IDENTITY_CHANGED",
                "Repository source identity differs from the expected snapshot.",
                data={
                    "expected_snapshot_digest": expected,
                    "current_snapshot_digest": current,
                },
            )
        return snapshot

    def _require_preparation(
        self, preparation_id: str
    ) -> DiscoveryCandidatePreparation:
        preparation = self._preparations.get(preparation_id)
        if preparation is None:
            raise BridgeFault(
                INVALID_PARAMS,
                "UNKNOWN_PREPARATION",
                "The preparation is not available in this bridge process.",
            )
        self._preparations.move_to_end(preparation_id)
        return preparation

    def _register_expansion_candidates(
        self,
        snapshot: ProjectSnapshot,
        preparation: DiscoveryCandidatePreparation,
        operation: ExpansionOperation,
        data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if operation not in {"text", "symbol"}:
            return []
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return []
        files = {item.path: item for item in snapshot.files}
        candidates = {item.path: item for item in preparation.candidates}
        additions: list[PreparedDiscoveryCandidate] = []
        response: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                continue
            try:
                path = validate_portable_relative_path(raw["path"])
            except ValueError:
                continue
            if path in seen or path not in files:
                continue
            seen.add(path)
            project_file = files[path]
            line = raw.get("line")
            ranges: list[dict[str, int]] = []
            if type(line) is int and line > 0:
                line_count = len(
                    (snapshot.root / path).read_text(encoding="utf-8").splitlines()
                )
                ranges.append(
                    {
                        "start_line": max(1, line - 2),
                        "end_line": min(max(1, line_count), line + 8),
                    }
                )
            candidate = candidates.get(path)
            if candidate is None:
                identifier = (
                    "x-"
                    + hashlib.sha256(
                        f"{preparation.preparation_id}:{path}".encode()
                    ).hexdigest()[:16]
                )
                candidate = PreparedDiscoveryCandidate(
                    candidate_id=identifier,
                    path=path,
                    language=project_file.language or "text",
                    rank=len(preparation.candidates) + len(additions) + 1,
                    score=0.0,
                    ranking_signals=(f"expanded_{operation}_evidence",),
                    source_sha256=project_file.sha256,
                    source_size_bytes=project_file.size_bytes,
                    evidence_origin="fresh",
                    structural_evidence=False,
                    semantic_evidence=False,
                )
                additions.append(candidate)
                candidates[path] = candidate
            response.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "path": path,
                    "language": candidate.language,
                    "kind": "line_ranges" if ranges else "codemap",
                    "ranges": ranges,
                    "source_sha256": candidate.source_sha256,
                    "evidence_kind": (
                        "verified_symbol" if operation == "symbol" else "exact_text"
                    ),
                    "confidence": 1.0,
                }
            )
        if additions:
            updated = preparation.model_copy(
                update={
                    "candidates": preparation.candidates + tuple(additions),
                    "total_candidate_count": (
                        preparation.total_candidate_count + len(additions)
                    ),
                }
            )
            self._preparations[preparation.preparation_id] = updated
        return response

    def _index_coverage(self) -> dict[str, int]:
        coverage = {
            "total_files": 0,
            "parsed_files": 0,
            "fallback_files": 0,
            "semantic_complete_files": 0,
            "semantic_disabled_files": 0,
            "semantic_failed_files": 0,
            "semantic_partial_files": 0,
            "semantic_chunks_planned": 0,
            "semantic_chunks_completed": 0,
            "verified_symbols": 0,
            "inferred_regions": 0,
        }
        try:
            manifest = load_manifest(self.workspace)
        except Exception:
            return coverage
        coverage["total_files"] = len(manifest.files)
        for state in manifest.files:
            if state.semantic_status == "complete":
                coverage["semantic_complete_files"] += 1
            elif state.semantic_status == "partial":
                coverage["semantic_partial_files"] += 1
            elif state.semantic_status == "disabled":
                coverage["semantic_disabled_files"] += 1
            elif state.semantic_status == "failed":
                coverage["semantic_failed_files"] += 1
            try:
                code_map = load_file_code_map(
                    self.workspace, state.path, manifest=manifest
                )
            except Exception:
                continue
            if code_map.parse_status == "unsupported":
                coverage["fallback_files"] += 1
            else:
                coverage["parsed_files"] += 1
            coverage["verified_symbols"] += len(code_map.symbols)
            if state.semantic_status in {"complete", "partial"}:
                try:
                    analysis = load_file_semantic_analysis(
                        self.workspace, state.path, manifest=manifest
                    )
                except Exception:
                    continue
                coverage["inferred_regions"] += len(analysis.inferred_regions)
                coverage["semantic_chunks_planned"] += analysis.chunks_planned
                coverage["semantic_chunks_completed"] += analysis.chunks_completed
                if (
                    not analysis.coverage_complete
                    and state.semantic_status == "complete"
                ):
                    coverage["semantic_complete_files"] -= 1
                    coverage["semantic_partial_files"] += 1
        return coverage

    def _remember_preparation(self, preparation: DiscoveryCandidatePreparation) -> None:
        self._preparations[preparation.preparation_id] = preparation
        self._preparations.move_to_end(preparation.preparation_id)
        while len(self._preparations) > MAX_PREPARATIONS:
            self._preparations.popitem(last=False)

    def _selection(
        self,
        preparation: DiscoveryCandidatePreparation,
        items: tuple[BridgeSelectionItem, ...],
    ) -> DiscoverySelection:
        candidates = {item.candidate_id: item for item in preparation.candidates}
        selected: list[DiscoverySelectionItem] = []
        for item in items:
            candidate = candidates.get(item.candidate_id)
            if candidate is None:
                raise BridgeFault(
                    INVALID_PARAMS,
                    "UNKNOWN_CANDIDATE",
                    "A selected candidate was not returned by discover.",
                )
            if item.path is not None and item.path != candidate.path:
                raise BridgeFault(
                    SOURCE_IDENTITY_CHANGED,
                    "SOURCE_IDENTITY_CHANGED",
                    "Selected path does not match the prepared candidate.",
                )
            if (
                item.source_sha256 is not None
                and item.source_sha256 != candidate.source_sha256
            ):
                raise BridgeFault(
                    SOURCE_IDENTITY_CHANGED,
                    "SOURCE_IDENTITY_CHANGED",
                    "Selected source identity does not match the prepared candidate.",
                )
            selected.append(
                DiscoverySelectionItem(
                    candidate_id=item.candidate_id,
                    ranges=item.ranges,
                )
            )
        return DiscoverySelection(
            preparation_id=preparation.preparation_id,
            items=tuple(selected),
        )

    def _selection_id(
        self,
        preparation: DiscoveryCandidatePreparation,
        items: tuple[BridgeSelectionItem, ...],
    ) -> str:
        candidates = {item.candidate_id: item for item in preparation.candidates}
        normalized_ranges = [
            {
                "path": candidates[item.candidate_id].path,
                "ranges": [
                    [line_range.start_line, line_range.end_line]
                    for line_range in item.ranges
                ],
            }
            for item in items
        ]
        normalized_task = unicodedata.normalize(
            "NFC",
            preparation.task.replace("\r\n", "\n").replace("\r", "\n").strip(),
        )
        payload = {
            "workspace": self.workspace_identity,
            "snapshot_digest": preparation.source_snapshot_digest,
            "config_digest": self.config_digest,
            "task_digest": hashlib.sha256(normalized_task.encode("utf-8")).hexdigest(),
            "discovery_mode": preparation.mode.value,
            "effective_budget": preparation.budget.model_dump(mode="json"),
            "selected_ranges": normalized_ranges,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def _validate_expansion_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        model = TOOL_INPUT_MODELS[tool_name]
        try:
            model.model_validate(arguments)
            for key in ("path", "path_prefix"):
                value = arguments.get(key)
                if value is not None:
                    validate_portable_relative_path(value)
        except (ValidationError, ValueError) as exc:
            raise BridgeFault(
                INVALID_PARAMS,
                "INVALID_PARAMS",
                "Expansion arguments are invalid.",
                data={"validation": _validation_details(exc)},
            ) from None

    async def _write_validation_error(
        self, request_id: str | int, exc: ValidationError
    ) -> None:
        await self._write_error(
            request_id,
            BridgeFault(
                INVALID_PARAMS,
                "INVALID_PARAMS",
                "Method parameters are invalid.",
                data={"validation": _validation_details(exc)},
            ),
        )

    async def _write_result(
        self, request_id: str | int, result: dict[str, Any]
    ) -> None:
        await self._require_writer().write(
            {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
        )

    async def _write_error(
        self, request_id: str | int | None, fault: BridgeFault
    ) -> None:
        data = {"code": fault.typed_code, **fault.data}
        await self._require_writer().write(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "error": {
                    "code": fault.rpc_code,
                    "message": fault.message,
                    "data": data,
                },
            }
        )

    def _require_writer(self) -> _SerializedWriter:
        if self._writer is None:
            raise RuntimeError("bridge writer is not initialized")
        return self._writer


async def serve_stdio_bridge(
    workspace: str | Path,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    error_stream: TextIO | None = None,
) -> None:
    """Create and serve one workspace-bound stdio bridge."""

    await BridgeServer(workspace).serve(input_stream, output_stream, error_stream)


def run_stdio_bridge(
    workspace: str | Path,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    error_stream: TextIO | None = None,
) -> None:
    """Run stdio without re-awaiting requests abandoned by bounded shutdown."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            serve_stdio_bridge(workspace, input_stream, output_stream, error_stream)
        )
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
            # The bridge has already applied both bounded cancellation phases.
            # Loop closure is intentional for these lifecycle-abandoned tasks.
            task_with_lifecycle_flag: Any = task
            task_with_lifecycle_flag._log_destroy_pending = False
        loop.close()
        asyncio.set_event_loop(None)


async def _read_bounded_line(stream: BinaryIO) -> tuple[bytes | None, bool]:
    chunk = await asyncio.to_thread(stream.readline, MAX_JSONRPC_MESSAGE_BYTES + 1)
    if not chunk:
        return None, False
    oversized = len(chunk) > MAX_JSONRPC_MESSAGE_BYTES
    if oversized and not chunk.endswith(b"\n"):
        while True:
            remainder = await asyncio.to_thread(
                stream.readline, MAX_JSONRPC_MESSAGE_BYTES + 1
            )
            if not remainder or remainder.endswith(b"\n"):
                break
    return chunk, oversized


def _validate_envelope(
    frame: dict[str, Any],
) -> tuple[str, str | int | None, dict[str, Any]]:
    allowed = {"jsonrpc", "method", "params", "id"}
    if set(frame) - allowed or frame.get("jsonrpc") != JSONRPC_VERSION:
        raise BridgeFault(
            INVALID_REQUEST,
            "INVALID_REQUEST",
            "Invalid JSON-RPC request envelope.",
        )
    method = frame.get("method")
    if not isinstance(method, str) or not method:
        raise BridgeFault(
            INVALID_REQUEST,
            "INVALID_REQUEST",
            "JSON-RPC method must be a non-empty string.",
        )
    if method not in _METHOD_MODELS:
        raise BridgeFault(
            METHOD_NOT_FOUND,
            "METHOD_NOT_FOUND",
            "The requested method is not supported.",
        )
    request_id = frame.get("id")
    if request_id is not None and not _valid_rpc_id(request_id):
        raise BridgeFault(
            INVALID_REQUEST,
            "INVALID_REQUEST",
            "JSON-RPC id must be a string or integer.",
        )
    if method != "$/cancelRequest" and request_id is None:
        raise BridgeFault(
            INVALID_REQUEST,
            "INVALID_REQUEST",
            "Requests require a non-null correlation id.",
        )
    if "params" not in frame:
        raise BridgeFault(
            INVALID_PARAMS,
            "INVALID_PARAMS",
            "Method parameters must be present as an object.",
        )
    params = frame["params"]
    if not isinstance(params, dict):
        raise BridgeFault(
            INVALID_PARAMS,
            "INVALID_PARAMS",
            "Method parameters must be an object.",
        )
    return method, request_id, params


def _safe_frame_id(frame: dict[str, Any]) -> str | int | None:
    value = frame.get("id")
    if not _valid_rpc_id(value):
        return None
    return value


def _valid_rpc_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return 0 < len(value) <= 200
    return isinstance(value, int)


def _id_key(value: str | int) -> tuple[str, str | int]:
    return ("string", value) if isinstance(value, str) else ("integer", value)


def _workspace_identity(root: Path) -> str:
    value = unicodedata.normalize("NFC", os.path.normcase(str(root))).replace("\\", "/")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_type[T: BaseModel](value: BaseModel, expected: type[T]) -> T:
    if not isinstance(value, expected):
        raise TypeError("unexpected bridge parameter model")
    return value


def _validation_details(exc: ValidationError | ValueError) -> list[dict[str, Any]]:
    if isinstance(exc, ValidationError):
        return [
            {
                "location": [str(item) for item in error["loc"]],
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors(include_url=False, include_input=False)
        ][:20]
    return [{"location": [], "message": str(exc), "type": "value_error"}]


def _index_bridge_fault(
    error: BaseException,
    event: ProgressEvent | None,
    operation_id: str,
) -> BridgeFault:
    error_code = "index_build_failed"
    reason = "ContextForge could not complete the index operation."
    retryable = False
    provider_failure: ModelProviderError | None = None
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModelProviderError):
            provider_failure = current
            error_code, reason = provider_error_details(current)
            retryable = (
                classify_retry(current) is RetryClassification.RETRYABLE
                and not current.circuit_opened
            )
            break
        typed_code = getattr(current, "error_code", None)
        safe_reason = getattr(current, "safe_reason", None)
        if isinstance(typed_code, str) and isinstance(safe_reason, str):
            error_code = typed_code
            reason = safe_reason[:1_000]
            break
        current = current.__cause__
    rpc_code = INDEX_BUILD_FAILED
    typed_rpc_code = "INDEX_BUILD_FAILED"
    if isinstance(error, IndexSourceChangedError):
        rpc_code = SOURCE_IDENTITY_CHANGED
        typed_rpc_code = "SOURCE_IDENTITY_CHANGED"
        error_code = "source_identity_changed"
        reason = "Repository source identity changed before index publication."
        retryable = True
    elif isinstance(error, (ProjectConfigError, ProviderConfigurationError)):
        rpc_code = PROVIDER_FAILURE
        typed_rpc_code = "PROVIDER_CONFIGURATION_ERROR"
        error_code = "provider_configuration_error"
        reason = "Project provider configuration is invalid."
    elif isinstance(error, SemanticFailureLimitError):
        rpc_code = FAILURE_LIMIT_REACHED
        typed_rpc_code = "FAILURE_LIMIT_REACHED"
        error_code = "failure_limit_reached"
        reason = "The configured semantic failure limit was reached."
    elif isinstance(error, SemanticProviderCircuitError):
        rpc_code = PROVIDER_CIRCUIT_OPEN
        typed_rpc_code = "PROVIDER_CIRCUIT_OPEN"
        error_code = "provider_circuit_open"
        reason = "The provider circuit breaker opened during indexing."
        retryable = False
    elif provider_failure is not None:
        if provider_failure.circuit_opened:
            rpc_code = PROVIDER_CIRCUIT_OPEN
            typed_rpc_code = "PROVIDER_CIRCUIT_OPEN"
            retryable = False
        else:
            rpc_code = PROVIDER_FAILURE
            typed_rpc_code = "PROVIDER_FAILURE"
    elif isinstance(error, IndexLockError):
        rpc_code = INDEX_LOCKED
        typed_rpc_code = "INDEX_LOCKED"
        error_code = "index_lock_unavailable"
        reason = "Another writer owns the index lock or lock recovery is required."
        retryable = True
    elif isinstance(error, IndexStorageError):
        rpc_code = INDEX_STORAGE_FAILURE
        typed_rpc_code = "INDEX_STORAGE_ERROR"
        error_code = "index_storage_error"
        reason = "ContextForge could not safely access index storage."
        retryable = True
    return BridgeFault(
        rpc_code,
        typed_rpc_code,
        "ContextForge index operation failed.",
        data={
            "error_code": error_code,
            "phase": "initialize" if event is None else event.phase_id,
            "reason": reason,
            "retryable": retryable,
            "operation_id": operation_id,
        },
    )


__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "JSONRPC_VERSION",
    "MAX_JSONRPC_MESSAGE_BYTES",
    "BridgeServer",
    "run_stdio_bridge",
    "serve_stdio_bridge",
]
