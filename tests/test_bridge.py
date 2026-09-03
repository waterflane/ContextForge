import asyncio
import gc
import io
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

import contextforge.bridge.server as bridge_module
from contextforge.bridge import MAX_JSONRPC_MESSAGE_BYTES, BridgeServer
from contextforge.bridge.models import (
    BridgeSelectionItem,
    CancelParams,
    DiscoverParams,
    ReadParams,
)


def test_bridge_protocol_schema_is_closed_and_matches_v1() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "docs/schemas/contextforge-bridge-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["$defs"]["helloRequest"]["properties"]["params"]["required"] == [
        "protocol_version"
    ]
    expand = schema["$defs"]["expandRequest"]["properties"]["params"]
    assert expand["additionalProperties"] is False
    assert "action_id" not in expand["properties"]
    assert "tool_name" not in expand["properties"]
    assert schema["$defs"]["discoverResult"]["additionalProperties"] is False


class _QueueInput:
    def __init__(self) -> None:
        self._lines: queue.Queue[bytes] = queue.Queue()

    def send(self, frame: dict[str, Any] | bytes) -> None:
        if isinstance(frame, bytes):
            self._lines.put(frame)
        else:
            self._lines.put(
                json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n"
            )

    def close(self) -> None:
        self._lines.put(b"")

    def readline(self, size: int = -1) -> bytes:
        del size
        return self._lines.get(timeout=5)


class _RecordingOutput:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.chunks: list[bytes] = []
        self.concurrent_write = False
        self._writing = False

    def write(self, value: bytes) -> int:
        with self._condition:
            if self._writing:
                self.concurrent_write = True
            self._writing = True
        time.sleep(0.002)
        with self._condition:
            self.chunks.append(value)
            self._writing = False
            self._condition.notify_all()
        return len(value)

    def flush(self) -> None:
        pass

    def wait(self, count: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 10
        with self._condition:
            while len(self.chunks) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("timed out waiting for bridge frames")
                self._condition.wait(remaining)
            chunks = list(self.chunks[:count])
        return [cast(dict[str, Any], json.loads(chunk)) for chunk in chunks]


class _Harness:
    def __init__(
        self,
        workspace: Path,
        *,
        shutdown_timeout_seconds: float | None = None,
    ) -> None:
        self.input = _QueueInput()
        self.output = _RecordingOutput()
        self.stderr = io.StringIO()
        self.server = (
            BridgeServer(workspace)
            if shutdown_timeout_seconds is None
            else BridgeServer(
                workspace,
                shutdown_timeout_seconds=shutdown_timeout_seconds,
            )
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self, *, negotiated: bool = True) -> None:
        self.server._protocol_negotiated = negotiated
        self.task = asyncio.create_task(
            self.server.serve(
                cast(Any, self.input), cast(Any, self.output), self.stderr
            )
        )

    async def response(self, count: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.output.wait, count)

    async def close(self) -> None:
        self.input.close()
        assert self.task is not None
        await self.task


def _request(
    request_id: str | int, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }


async def _snapshot(harness: _Harness, response_count: int = 1) -> str:
    harness.input.send(_request("snapshot", "snapshot"))
    response = (await harness.response(response_count))[-1]
    return cast(str, response["result"]["snapshot_digest"])


async def _discover(
    harness: _Harness, digest: str, response_count: int = 2
) -> dict[str, Any]:
    harness.input.send(
        _request(
            "discover",
            "discover",
            {
                "expected_snapshot_digest": digest,
                "task": "Find alpha",
                "mode": "fresh",
            },
        )
    )
    return (await harness.response(response_count))[-1]


def test_bridge_handshake_protocol_purity_and_shutdown(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        harness.input.send(_request("hello", "hello", {"protocol_version": "1.0"}))
        hello = (await harness.response(1))[0]
        assert hello["jsonrpc"] == "2.0"
        assert hello["result"]["protocol_version"] == "1.0"
        assert hello["result"]["supported_protocol_versions"] == ["1.0", "1.1"]
        assert hello["result"]["capabilities"]["model_free_discovery"] is True
        assert hello["result"]["policy"]["source_writes"] is False
        assert "shell" in hello["result"]["policy"]

        harness.input.send(_request("shutdown", "shutdown"))
        frames = await harness.response(2)
        assert frames[-1]["result"] == {"shutdown": True}
        assert harness.task is not None
        await harness.task
        assert harness.stderr.getvalue() == ""
        assert all(chunk.endswith(b"\n") for chunk in harness.output.chunks)
        assert all(
            json.loads(chunk)["jsonrpc"] == "2.0" for chunk in harness.output.chunks
        )

    asyncio.run(exercise())


def test_bridge_requires_compatible_protocol_negotiation(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start(negotiated=False)

        harness.input.send(_request("early", "snapshot"))
        early = (await harness.response(1))[-1]
        assert early["error"]["data"]["code"] == "PROTOCOL_NEGOTIATION_REQUIRED"

        harness.input.send(_request("missing", "hello"))
        missing = (await harness.response(2))[-1]
        assert missing["error"]["data"]["code"] == "INVALID_PARAMS"

        harness.input.send(
            _request("incompatible", "hello", {"protocol_version": "2.0"})
        )
        incompatible = (await harness.response(3))[-1]
        assert incompatible["error"]["data"] == {
            "code": "INCOMPATIBLE_PROTOCOL_VERSION",
            "requested_protocol_version": "2.0",
            "supported_protocol_versions": ["1.0", "1.1"],
        }

        harness.input.send(_request("compatible", "hello", {"protocol_version": "1.0"}))
        compatible = (await harness.response(4))[-1]
        assert compatible["result"]["protocol_version"] == "1.0"

        harness.input.send(_request("snapshot", "snapshot"))
        snapshot = (await harness.response(5))[-1]
        assert len(snapshot["result"]["snapshot_digest"]) == 64
        await harness.close()

    asyncio.run(exercise())


def test_bridge_rejects_malformed_oversized_unknown_and_invalid_requests(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        harness.input.send(b"{broken\n")
        harness.input.send(b"{" + b"x" * MAX_JSONRPC_MESSAGE_BYTES + b"}\n")
        harness.input.send(_request("unknown", "run_shell"))
        harness.input.send(_request("invalid", "discover", {"task": "x"}))
        frames = await harness.response(4)
        codes = [frame["error"]["data"]["code"] for frame in frames]
        assert codes == [
            "MALFORMED_JSON",
            "MESSAGE_TOO_LARGE",
            "METHOD_NOT_FOUND",
            "INVALID_PARAMS",
        ]
        await harness.close()

    asyncio.run(exercise())


def test_bridge_handles_duplicate_and_concurrent_requests_with_serial_writes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    original_scan = cast(Any, bridge_module).scan_repository
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def controlled_scan(path: Path) -> Any:
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            started.set()
            assert release.wait(5)
        return original_scan(path)

    monkeypatch.setattr(bridge_module, "scan_repository", controlled_scan)

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        harness.input.send(_request("same", "status"))
        assert await asyncio.to_thread(started.wait, 5)
        harness.input.send(_request("same", "status"))
        harness.input.send(_request("other", "status"))
        duplicate = (await harness.response(1))[0]
        assert duplicate["error"]["data"]["code"] == "DUPLICATE_REQUEST_ID"
        release.set()
        frames = await harness.response(3)
        assert {frame["id"] for frame in frames[1:]} == {"same", "other"}
        assert not harness.output.concurrent_write
        assert all(len(chunk.splitlines()) == 1 for chunk in harness.output.chunks)
        await harness.close()

    asyncio.run(exercise())


def test_bridge_cancellation_reaches_application_operation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / "alpha.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    started = threading.Event()
    observed = threading.Event()

    def cancellable_prepare(
        source: Any, request: Any, *, cancellation: asyncio.Event | None = None
    ) -> Any:
        del source, request
        assert cancellation is not None
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cancellation.is_set():
                observed.set()
                raise asyncio.CancelledError
            time.sleep(0.001)
        raise AssertionError("application operation did not receive cancellation")

    monkeypatch.setattr(
        bridge_module, "prepare_discovery_candidates", cancellable_prepare
    )

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        digest = await _snapshot(harness)
        harness.input.send(
            _request(
                "slow",
                "discover",
                {
                    "expected_snapshot_digest": digest,
                    "task": "alpha",
                    "mode": "fresh",
                },
            )
        )
        assert await asyncio.to_thread(started.wait, 5)
        harness.input.send(
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": "slow"},
            }
        )
        response = (await harness.response(2))[-1]
        assert response["id"] == "slow"
        assert response["error"]["data"]["code"] == "REQUEST_CANCELLED"
        assert observed.is_set()
        await harness.close()

    asyncio.run(exercise())


def test_bridge_shutdown_cooperatively_cancels_active_operation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    started = threading.Event()
    observed = threading.Event()

    def cooperative_prepare(
        source: Any, request: Any, *, cancellation: asyncio.Event | None = None
    ) -> Any:
        del source, request
        assert cancellation is not None
        started.set()
        while not cancellation.is_set():
            time.sleep(0.001)
        observed.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        bridge_module, "prepare_discovery_candidates", cooperative_prepare
    )

    async def exercise() -> None:
        harness = _Harness(tmp_path, shutdown_timeout_seconds=0.2)
        await harness.start()
        digest = await _snapshot(harness)
        harness.input.send(
            _request(
                "slow",
                "discover",
                {
                    "expected_snapshot_digest": digest,
                    "task": "alpha",
                    "mode": "fresh",
                },
            )
        )
        assert await asyncio.to_thread(started.wait, 5)
        started_at = time.monotonic()
        harness.input.send(_request("shutdown", "shutdown"))
        assert harness.task is not None
        await asyncio.wait_for(harness.task, timeout=1)
        elapsed = time.monotonic() - started_at
        frames = [json.loads(chunk) for chunk in harness.output.chunks]
        shutdown = next(frame for frame in frames if frame["id"] == "shutdown")
        cancelled = next(frame for frame in frames if frame["id"] == "slow")
        assert shutdown["result"] == {"shutdown": True}
        assert cancelled["error"]["data"]["code"] == "REQUEST_CANCELLED"
        assert "result" not in cancelled
        assert observed.is_set()
        assert elapsed < 0.5
        assert harness.stderr.getvalue() == ""
        assert all(
            json.loads(chunk)["jsonrpc"] == "2.0" for chunk in harness.output.chunks
        )

    asyncio.run(exercise())


def test_bridge_shutdown_is_bounded_when_task_ignores_first_cancellation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path, shutdown_timeout_seconds=0.02)
        await harness.start()
        cooperative = asyncio.Event()
        first_task_cancel = asyncio.Event()
        second_task_cancel = asyncio.Event()
        blocker = asyncio.Event()

        async def stubborn_operation() -> None:
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                first_task_cancel.set()
                try:
                    await blocker.wait()
                except asyncio.CancelledError:
                    second_task_cancel.set()
                    raise

        task = asyncio.create_task(stubborn_operation())
        active_type = cast(Any, bridge_module)._ActiveRequest
        harness.server._active[("str", "stubborn")] = active_type(
            cancellation=cooperative,
            task=task,
        )
        await asyncio.sleep(0)
        started_at = time.monotonic()
        harness.input.send(_request("shutdown", "shutdown"))
        shutdown = (await harness.response(1))[0]
        assert harness.task is not None
        await asyncio.wait_for(harness.task, timeout=0.5)
        elapsed = time.monotonic() - started_at
        await asyncio.wait_for(second_task_cancel.wait(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert shutdown["result"] == {"shutdown": True}
        assert cooperative.is_set()
        assert first_task_cancel.is_set()
        assert task.done()
        assert elapsed < 0.5
        stderr = harness.stderr.getvalue()
        assert "shutdown deadline expired" in stderr
        assert "Traceback" not in stderr
        assert len(harness.output.chunks) == 1
        assert json.loads(harness.output.chunks[0])["jsonrpc"] == "2.0"

    asyncio.run(exercise())


def test_bridge_eof_cleanup_is_bounded_for_non_cooperative_task(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path, shutdown_timeout_seconds=0.02)
        await harness.start()
        cooperative = asyncio.Event()
        first_task_cancel = asyncio.Event()
        second_task_cancel = asyncio.Event()
        blocker = asyncio.Event()

        async def stubborn_operation() -> None:
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                first_task_cancel.set()
                try:
                    await blocker.wait()
                except asyncio.CancelledError:
                    second_task_cancel.set()
                    raise

        task = asyncio.create_task(stubborn_operation())
        active_type = cast(Any, bridge_module)._ActiveRequest
        harness.server._active[("str", "stubborn-eof")] = active_type(
            cancellation=cooperative,
            task=task,
        )
        await asyncio.sleep(0)
        started_at = time.monotonic()
        harness.input.close()
        assert harness.task is not None
        await asyncio.wait_for(harness.task, timeout=0.5)
        elapsed = time.monotonic() - started_at
        await asyncio.wait_for(second_task_cancel.wait(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cooperative.is_set()
        assert first_task_cancel.is_set()
        assert task.done()
        assert elapsed < 0.5
        assert harness.output.chunks == []
        stderr = harness.stderr.getvalue()
        assert "shutdown deadline expired" in stderr
        assert "Traceback" not in stderr

    asyncio.run(exercise())


def test_bridge_sync_runner_does_not_reawait_abandoned_task(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    cancellation_count = 0
    retained: list[asyncio.Task[None]] = []

    async def fake_serve(
        workspace: Any,
        input_stream: Any,
        output_stream: Any,
        error_stream: Any,
    ) -> None:
        del workspace, input_stream, output_stream, error_stream

        async def repeatedly_non_cooperative() -> None:
            nonlocal cancellation_count
            blocker = asyncio.Event()
            while True:
                try:
                    await blocker.wait()
                except asyncio.CancelledError:
                    cancellation_count += 1

        task = asyncio.create_task(repeatedly_non_cooperative())
        retained.append(task)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(bridge_module, "serve_stdio_bridge", fake_serve)
    started_at = time.monotonic()
    bridge_module.run_stdio_bridge(tmp_path, io.BytesIO(), io.BytesIO(), io.StringIO())
    elapsed = time.monotonic() - started_at
    assert cancellation_count == 2
    assert not retained[0].done()
    assert elapsed < 0.5
    retained.clear()
    gc.collect()
    captured = capsys.readouterr()
    assert "Task was destroyed" not in captured.err
    assert "Traceback" not in captured.err


def test_bridge_discover_expand_read_and_package_are_verified_and_in_memory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alpha.py"
    source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    initial = source.read_bytes()

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        digest = await _snapshot(harness)
        discovered = await _discover(harness, digest)
        result = discovered["result"]
        assert result["model_provider_used"] is False
        assert {
            "schema_version",
            "task",
            "pinned_paths",
            "excluded_paths",
            "strict",
            "budget",
        }.isdisjoint(result)
        assert [item["rank"] for item in result["candidates"]] == sorted(
            item["rank"] for item in result["candidates"]
        )
        candidate = result["candidates"][0]
        preparation_id = result["preparation_id"]

        harness.input.send(
            _request(
                "expand",
                "expand",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "operation": "text",
                    "arguments": {"query": "alpha"},
                },
            )
        )
        expanded = (await harness.response(3))[-1]
        assert expanded["result"]["operation"] == "text"
        assert expanded["result"]["ok"] is True
        assert "observation" not in expanded["result"]
        assert "tool_name" not in expanded["result"]

        item = {
            "candidate_id": candidate["candidate_id"],
            "path": candidate["path"],
            "source_sha256": candidate["source_sha256"],
            "ranges": [{"start_line": 1, "end_line": 1}],
        }
        harness.input.send(
            _request(
                "read",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [item],
                },
            )
        )
        read = (await harness.response(4))[-1]
        assert read["result"]["files"][0]["path"] == "alpha.py"
        assert read["result"]["files"][0]["blocks"][0]["text"] == "def alpha():\n"
        assert {"schema_version", "task", "mode", "index_generation_id"}.isdisjoint(
            read["result"]
        )

        harness.input.send(
            _request(
                "package",
                "package",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [item],
                },
            )
        )
        packaged = (await harness.response(5))[-1]
        assert packaged["result"]["selection_id"] == read["result"]["selection_id"]
        package = packaged["result"]["package"]
        assert package["files"][0]["source_sha256"] == candidate["source_sha256"]
        assert source.read_bytes() == initial
        assert not (tmp_path / ".contextforge").exists()
        await harness.close()

    asyncio.run(exercise())


def test_bridge_v11_expansion_candidate_can_be_read(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    (tmp_path / "beta.ts").write_text(
        "export function uniqueBeta() { return 2; }\n", encoding="utf-8"
    )

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start(negotiated=False)
        harness.input.send(_request("hello", "hello", {"protocol_version": "1.1"}))
        hello = (await harness.response(1))[-1]
        assert hello["result"]["capabilities"]["expansion_candidates"] is True
        digest = await _snapshot(harness, 2)
        harness.input.send(
            _request(
                "discover",
                "discover",
                {
                    "expected_snapshot_digest": digest,
                    "task": "Find alpha",
                    "mode": "fresh",
                    "budget": {"max_preselected_candidates": 1},
                },
            )
        )
        discovered = (await harness.response(3))[-1]
        assert "beta.ts" not in {
            item["path"] for item in discovered["result"]["candidates"]
        }
        preparation_id = discovered["result"]["preparation_id"]

        harness.input.send(
            _request(
                "expand",
                "expand",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "operation": "text",
                    "arguments": {"query": "uniqueBeta", "case_sensitive": True},
                },
            )
        )
        expanded = (await harness.response(4))[-1]["result"]
        candidate = next(
            item for item in expanded["candidates"] if item["path"] == "beta.ts"
        )
        assert candidate["evidence_kind"] == "exact_text"
        assert candidate["ranges"] == [{"start_line": 1, "end_line": 1}]

        harness.input.send(
            _request(
                "read",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "path": candidate["path"],
                            "source_sha256": candidate["source_sha256"],
                            "ranges": candidate["ranges"],
                        }
                    ],
                },
            )
        )
        read = (await harness.response(5))[-1]
        assert read["result"]["files"][0]["path"] == "beta.ts"
        assert "uniqueBeta" in read["result"]["files"][0]["blocks"][0]["text"]
        await harness.close()

    asyncio.run(exercise())


def test_bridge_rejects_path_traversal_source_mismatch_and_snapshot_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alpha.py"
    source.write_text("alpha = 1\n", encoding="utf-8")

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        digest = await _snapshot(harness)
        discovered = await _discover(harness, digest)
        result = discovered["result"]
        candidate = result["candidates"][0]
        preparation_id = result["preparation_id"]

        harness.input.send(
            _request(
                "traversal",
                "expand",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "operation": "importers",
                    "arguments": {"path": "../secret"},
                },
            )
        )
        traversal = (await harness.response(3))[-1]
        assert traversal["error"]["data"]["code"] == "INVALID_PARAMS"

        harness.input.send(
            _request(
                "mismatch",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "path": candidate["path"],
                            "source_sha256": "0" * 64,
                        }
                    ],
                },
            )
        )
        mismatch = (await harness.response(4))[-1]
        assert mismatch["error"]["data"]["code"] == "SOURCE_IDENTITY_CHANGED"

        harness.input.send(
            _request(
                "path-mismatch",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "path": "different.py",
                        }
                    ],
                },
            )
        )
        path_mismatch = (await harness.response(5))[-1]
        assert path_mismatch["error"]["data"]["code"] == "SOURCE_IDENTITY_CHANGED"

        source.write_text("alpha = 2\n", encoding="utf-8")
        harness.input.send(
            _request(
                "drift",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [{"candidate_id": candidate["candidate_id"]}],
                },
            )
        )
        drift = (await harness.response(6))[-1]
        assert drift["error"]["data"]["code"] == "SOURCE_IDENTITY_CHANGED"
        assert "result" not in drift
        await harness.close()

    asyncio.run(exercise())


def test_bridge_clean_eof_produces_no_spurious_frame(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        await harness.close()
        assert harness.output.chunks == []
        assert harness.stderr.getvalue() == ""

    asyncio.run(exercise())


def test_bridge_cli_stdio_keeps_stdout_protocol_only(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "contextforge",
            "bridge",
            "--stdio",
            "--workspace",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    output = []
    for frame in (
        _request("hello", "hello", {"protocol_version": "1.0"}),
        _request("snapshot", "snapshot"),
        _request("shutdown", "shutdown"),
    ):
        process.stdin.write(json.dumps(frame) + "\n")
        process.stdin.flush()
        output.append(json.loads(process.stdout.readline()))
    process.stdin.close()
    returncode = process.wait(timeout=10)
    assert process.stderr is not None
    stderr = process.stderr.read()

    assert returncode == 0, stderr
    assert [frame["id"] for frame in output] == ["hello", "snapshot", "shutdown"]
    assert output[0]["result"]["protocol_version"] == "1.0"
    assert len(output[1]["result"]["snapshot_digest"]) == 64
    assert all(frame["jsonrpc"] == "2.0" for frame in output)
    assert stderr == ""


def test_bridge_validates_jsonrpc_envelopes_and_cancel_params(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        invalid_frames: list[dict[str, Any]] = [
            {"jsonrpc": "1.0", "id": "version", "method": "hello", "params": {}},
            {"jsonrpc": "2.0", "id": "extra", "method": "hello", "params": {}, "x": 1},
            {"jsonrpc": "2.0", "id": "method", "method": 1, "params": {}},
            {"jsonrpc": "2.0", "id": [], "method": "hello", "params": {}},
            {"jsonrpc": "2.0", "method": "hello", "params": {}},
            {"jsonrpc": "2.0", "id": "params", "method": "hello", "params": []},
            {"jsonrpc": "2.0", "id": "missing-params", "method": "snapshot"},
            {
                "jsonrpc": "2.0",
                "id": "x" * 201,
                "method": "snapshot",
                "params": {},
            },
        ]
        for frame in invalid_frames:
            harness.input.send(frame)
        harness.input.send(b"[]\n")
        frames = await harness.response(len(invalid_frames) + 1)
        assert all("error" in frame for frame in frames)
        assert frames[0]["id"] == "version"
        assert frames[3]["id"] is None

        harness.input.send(_request("cancel", "$/cancelRequest", {"id": "not-active"}))
        cancelled = (await harness.response(10))[-1]
        assert cancelled["result"] == {"cancelled": False}
        harness.input.send(_request("bad-cancel", "$/cancelRequest", {"id": True}))
        invalid_cancel = (await harness.response(11))[-1]
        assert invalid_cancel["error"]["data"]["code"] == "INVALID_PARAMS"
        await harness.close()

    asyncio.run(exercise())


def test_bridge_repository_error_paths_are_typed_and_all_or_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        harness.input.send(
            _request(
                "premature",
                "discover",
                {
                    "expected_snapshot_digest": "0" * 64,
                    "task": "alpha",
                    "mode": "fresh",
                },
            )
        )
        premature = (await harness.response(1))[-1]
        assert premature["error"]["data"]["code"] == "SNAPSHOT_REQUIRED"

        digest = await _snapshot(harness, 2)
        harness.input.send(
            _request(
                "unknown-preparation",
                "expand",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": "0" * 64,
                    "operation": "text",
                    "arguments": {"query": "alpha"},
                },
            )
        )
        unknown = (await harness.response(3))[-1]
        assert unknown["error"]["data"]["code"] == "UNKNOWN_PREPARATION"

        harness.input.send(
            _request(
                "discover-limited",
                "discover",
                {
                    "expected_snapshot_digest": digest,
                    "task": "alpha",
                    "mode": "fresh",
                    "budget": {"max_steps": 1, "max_context_bytes": 1},
                },
            )
        )
        discovered = (await harness.response(4))[-1]["result"]
        candidate = discovered["candidates"][0]
        preparation_id = discovered["preparation_id"]

        harness.input.send(
            _request(
                "unknown-candidate",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [{"candidate_id": "missing"}],
                },
            )
        )
        unknown_candidate = (await harness.response(5))[-1]
        assert unknown_candidate["error"]["data"]["code"] == "UNKNOWN_CANDIDATE"

        harness.input.send(
            _request(
                "invalid-range",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "ranges": [{"start_line": 1, "end_line": 999}],
                        }
                    ],
                },
            )
        )
        invalid_range = (await harness.response(6))[-1]
        assert invalid_range["error"]["data"]["code"] == "INVALID_SOURCE_RANGE"

        harness.input.send(
            _request(
                "limit",
                "read",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "items": [{"candidate_id": candidate["candidate_id"]}],
                },
            )
        )
        limited = (await harness.response(7))[-1]
        assert limited["error"]["data"]["code"] == "RESOURCE_LIMIT_EXCEEDED"

        harness.input.send(
            _request(
                "expand-once",
                "expand",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "operation": "text",
                    "arguments": {"query": "alpha"},
                },
            )
        )
        expanded = (await harness.response(8))[-1]["result"]
        harness.input.send(
            _request(
                "expand-over-budget",
                "expand",
                {
                    "expected_snapshot_digest": digest,
                    "preparation_id": preparation_id,
                    "operation": "text",
                    "arguments": {"query": "alpha"},
                    "budget_usage": expanded["budget_usage"],
                },
            )
        )
        rejected = (await harness.response(9))[-1]
        assert rejected["error"]["data"]["code"] == "APPLICATION_REQUEST_REJECTED"

        harness.input.send(
            _request(
                "status",
                "status",
                {"expected_snapshot_digest": "0" * 64},
            )
        )
        status = (await harness.response(10))[-1]
        assert status["result"]["source_identity_changed"] is True
        assert {"schema_version", "repository_identity"}.isdisjoint(
            status["result"]["index"]
        )
        await harness.close()

    asyncio.run(exercise())


def test_bridge_timeout_and_internal_failure_are_safe(
    tmp_path: Path, monkeypatch: Any
) -> None:
    original_scan = cast(Any, bridge_module).scan_repository

    async def timeout_exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        digest = await _snapshot(harness)

        def slow_scan(path: Path) -> Any:
            time.sleep(0.05)
            return original_scan(path)

        monkeypatch.setattr(bridge_module, "scan_repository", slow_scan)
        harness.input.send(
            _request(
                "timeout",
                "discover",
                {
                    "expected_snapshot_digest": digest,
                    "task": "alpha",
                    "mode": "fresh",
                    "timeout_ms": 1,
                },
            )
        )
        timed_out = (await harness.response(2))[-1]
        assert timed_out["error"]["data"]["code"] == "REQUEST_TIMEOUT"
        await harness.close()

    asyncio.run(timeout_exercise())

    def broken_scan(path: Path) -> Any:
        del path
        raise RuntimeError("absolute secret must not escape")

    monkeypatch.setattr(bridge_module, "scan_repository", broken_scan)

    async def failure_exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        harness.input.send(_request("failure", "status"))
        failure = (await harness.response(1))[-1]
        assert failure["error"]["data"]["code"] == "INTERNAL_ERROR"
        assert "secret" not in json.dumps(failure)
        assert "internal error" in harness.stderr.getvalue()
        await harness.close()

    asyncio.run(failure_exercise())


def test_bridge_parameter_models_reject_noncanonical_values() -> None:
    with pytest.raises(ValidationError):
        DiscoverParams.model_validate_json(
            json.dumps(
                {
                    "expected_snapshot_digest": "0" * 64,
                    "task": "x",
                    "pinned_paths": ["z.py", "a.py"],
                }
            )
        )
    with pytest.raises(ValidationError):
        BridgeSelectionItem.model_validate(
            {
                "candidate_id": "candidate",
                "path": "..\\secret",
            }
        )
    with pytest.raises(ValidationError):
        ReadParams.model_validate_json(
            json.dumps(
                {
                    "expected_snapshot_digest": "0" * 64,
                    "preparation_id": "0" * 64,
                    "items": [
                        {"candidate_id": "b"},
                        {"candidate_id": "a"},
                    ],
                }
            )
        )
    with pytest.raises(ValidationError):
        CancelParams.model_validate({"id": True})
    with pytest.raises(ValidationError):
        BridgeSelectionItem.model_validate_json(
            json.dumps(
                {
                    "candidate_id": "candidate",
                    "path": None,
                    "ranges": [
                        {"start_line": 1, "end_line": 2},
                        {"start_line": 2, "end_line": 3},
                    ],
                }
            )
        )


def test_bridge_low_level_resource_guards(tmp_path: Path) -> None:
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("not a workspace", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        BridgeServer(regular_file)

    unstarted = BridgeServer(tmp_path)
    with pytest.raises(RuntimeError, match="writer"):
        unstarted._require_writer()

    diagnostics_type = cast(Any, bridge_module)._BoundedDiagnostics
    diagnostics = diagnostics_type(None)
    diagnostics.write("ignored")
    target = io.StringIO()
    diagnostics = diagnostics_type(target)
    diagnostics._remaining = 3
    diagnostics.write("abcdef")
    assert len(target.getvalue().encode("utf-8")) == 3
    diagnostics.write("ignored after exhaustion")

    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        harness.input.send(b"x" * (MAX_JSONRPC_MESSAGE_BYTES + 1))
        harness.input.send(b"discarded-tail\n")
        harness.input.send(
            b'{"jsonrpc":"2.0","id":"nan","method":"hello","params":{"timeout_ms":NaN}}\n'
        )
        harness.input.send(
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": True},
            }
        )
        harness.input.send(
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": "not-active"},
            }
        )
        harness.input.send(_request("shutdown", "shutdown"))
        frames = await harness.response(3)
        assert [frame["error"]["data"]["code"] for frame in frames[:2]] == [
            "MESSAGE_TOO_LARGE",
            "MALFORMED_JSON",
        ]
        assert frames[-1]["result"] == {"shutdown": True}
        assert harness.task is not None
        await harness.task

    asyncio.run(exercise())


def test_bridge_rejects_new_work_after_shutdown_starts(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path)
        await harness.start()
        await asyncio.sleep(0)
        harness.server._shutting_down = True
        await harness.server._accept_request(_request("late", "hello"), inline=False)
        late = (await harness.response(1))[-1]
        assert late["error"]["data"]["code"] == "SHUTTING_DOWN"
        harness.input.close()
        assert harness.task is not None
        await harness.task

    asyncio.run(exercise())
