"""Persistent bounded JSON-RPC 2.0 service over NDJSON streams."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from pydantic import BaseModel, ValidationError

from contextforge._metadata import __version__
from contextforge.application import inspect_repository_index
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
from contextforge.discovery.models import DiscoveryCandidatePreparation
from contextforge.discovery.tools import TOOL_INPUT_MODELS
from contextforge.intelligence import (
    calculate_source_snapshot_digest,
    canonical_json_bytes,
)
from contextforge.repositories import ProjectSnapshot, ScanOptions, scan_repository

from .models import (
    BridgeSelectionItem,
    CancelParams,
    DiscoverParams,
    ExpandParams,
    ExpansionOperation,
    HelloParams,
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

_METHOD_MODELS: dict[str, type[BaseModel]] = {
    "hello": HelloParams,
    "status": StatusParams,
    "snapshot": SnapshotParams,
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
    """Workspace-bound persistent ContextForge bridge protocol v1 server."""

    def __init__(self, workspace: str | Path) -> None:
        root = Path(workspace).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        self.workspace = root
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
        self._writer: _SerializedWriter | None = None
        self._diagnostics = _BoundedDiagnostics(None)
        self._shutting_down = False
        self._protocol_negotiated = False

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
            self._shutting_down = True
            active = tuple(self._active.values())
            for request in active:
                request.cancellation.set()
            if active:
                await asyncio.gather(
                    *(request.task for request in active), return_exceptions=True
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
            operation = self._dispatch(method, params, cancellation)
            if timeout_ms is None:
                result = await operation
            else:
                try:
                    result = await asyncio.wait_for(
                        operation, timeout=timeout_ms / 1000
                    )
                except TimeoutError:
                    cancellation.set()
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
        self, method: str, raw: BaseModel, cancellation: asyncio.Event
    ) -> dict[str, Any]:
        if cancellation.is_set():
            raise asyncio.CancelledError
        if method == "hello":
            return self._hello()
        if method == "status":
            return await self._status(_require_type(raw, StatusParams), cancellation)
        if method == "snapshot":
            return await self._snapshot(cancellation)
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
            self._shutting_down = True
            return {"shutdown": True}
        raise BridgeFault(
            METHOD_NOT_FOUND,
            "METHOD_NOT_FOUND",
            "The requested method is not supported.",
        )

    def _hello(self) -> dict[str, Any]:
        return {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "supported_protocol_versions": list(SUPPORTED_BRIDGE_PROTOCOL_VERSIONS),
            "contextforge_version": __version__,
            "capabilities": {
                "methods": [
                    "hello",
                    "status",
                    "snapshot",
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
            },
            "workspace": {
                "identity": self.workspace_identity,
            },
            "policy": {
                "repository_access": "read_only_verified_snapshot",
                "external_data": "disabled",
                "portable_paths_only": True,
                "source_writes": False,
                "index_mutation": False,
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
        report = await asyncio.to_thread(
            inspect_repository_index,
            self.workspace,
            provider_configuration=None,
        )
        if cancellation.is_set():
            raise asyncio.CancelledError
        return {
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

    async def _snapshot(self, cancellation: asyncio.Event) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(scan_repository, self.workspace)
        if cancellation.is_set():
            raise asyncio.CancelledError
        digest = calculate_source_snapshot_digest(snapshot)
        if digest != self._snapshot_digest:
            self._preparations.clear()
        self._snapshot_digest = digest
        return {
            "snapshot_digest": digest,
            "file_count": len(snapshot.files),
            "source_bytes": sum(item.size_bytes for item in snapshot.files),
            "languages": dict(sorted(snapshot.summary.languages.items())),
        }

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
        return {
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
        return {
            "preparation_id": result.preparation_id,
            "operation": params.operation,
            "ok": result.ok,
            "code": result.code,
            "data": result.data,
            "truncated": result.truncated,
            "made_progress": result.made_progress,
            "budget_usage": result.budget_usage.model_dump(mode="json"),
        }

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


__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "JSONRPC_VERSION",
    "MAX_JSONRPC_MESSAGE_BYTES",
    "BridgeServer",
    "serve_stdio_bridge",
]
