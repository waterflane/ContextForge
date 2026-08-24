"""Minimal synchronous client for the trusted-local ContextForge bridge v1."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO, cast

PROTOCOL_VERSION = "1.0"


class BridgeError(RuntimeError):
    """Safe JSON-RPC error returned by the bridge."""


class BridgeClient:
    def __init__(self, workspace: Path) -> None:
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "contextforge",
                "bridge",
                "--stdio",
                "--workspace",
                str(workspace),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
        )
        self._stdin = cast(TextIO, self._process.stdin)
        self._stdout = cast(TextIO, self._process.stdout)
        self._next_id = 1

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self._stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
        self._stdin.flush()
        line = self._stdout.readline()
        if not line:
            raise BridgeError("bridge closed stdout before returning a response")
        response = json.loads(line)
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise BridgeError("unexpected JSON-RPC response correlation")
        if "error" in response:
            error = response["error"]
            typed_code = error.get("data", {}).get("code", "UNKNOWN")
            raise BridgeError(f"{typed_code}: {error.get('message', 'bridge error')}")
        return cast(dict[str, Any], response["result"])

    def close(self) -> None:
        if self._process.poll() is None:
            self.request("shutdown", {})
        self._stdin.close()
        returncode = self._process.wait(timeout=10)
        if returncode != 0:
            raise BridgeError(f"bridge exited with status {returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read one verified candidate through ContextForge bridge v1."
    )
    parser.add_argument("workspace", type=Path)
    parser.add_argument("task")
    arguments = parser.parse_args()

    client = BridgeClient(arguments.workspace.resolve(strict=True))
    try:
        hello = client.request(
            "hello",
            {"protocol_version": PROTOCOL_VERSION, "client_name": "generic-example"},
        )
        if hello.get("protocol_version") != PROTOCOL_VERSION:
            raise BridgeError("bridge selected an incompatible protocol version")

        snapshot = client.request("snapshot", {})
        digest = snapshot["snapshot_digest"]
        discovery = client.request(
            "discover",
            {
                "expected_snapshot_digest": digest,
                "task": arguments.task,
                "mode": "hybrid",
            },
        )
        candidates = discovery["candidates"]
        if not candidates:
            raise BridgeError("discovery returned no selectable candidates")

        # A real consumer may ask its own model or user to choose. ContextForge
        # remains model-free and verifies whichever issued candidate is selected.
        selected = candidates[0]
        verified = client.request(
            "read",
            {
                "expected_snapshot_digest": digest,
                "preparation_id": discovery["preparation_id"],
                "items": [
                    {
                        "candidate_id": selected["candidate_id"],
                        "path": selected["path"],
                        "source_sha256": selected["source_sha256"],
                    }
                ],
            },
        )
        print(json.dumps(verified, ensure_ascii=False, indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
