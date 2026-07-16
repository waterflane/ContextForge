import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

import contextforge.cli.mcp_commands as mcp_cli
import contextforge.mcp.server as server_module
from contextforge.cli.main import app
from contextforge.context import ContextBuildOptions, build_context_package
from contextforge.mcp import (
    MCP_TOOL_SCHEMAS,
    MCPServer,
    ReadOnlyMCPFoundation,
    ReadOnlyToolError,
)
from contextforge.models import ProviderConfiguration
from contextforge.project_config import create_model_provider

runner = CliRunner()


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _provider() -> Any:
    return create_model_provider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="fixture",
            timeout_seconds=2,
            retry_limit=0,
        )
    )


def _foundation(root: Path, *, provider: bool = False) -> ReadOnlyMCPFoundation:
    return ReadOnlyMCPFoundation(
        root,
        provider=_provider() if provider else None,
        discovery_mode="fresh",  # type: ignore[arg-type]
    )


def test_mcp_tool_schema_is_exactly_read_only() -> None:
    assert tuple(MCP_TOOL_SCHEMAS) == (
        "repository_overview",
        "list_tree",
        "search_index",
        "search_symbols",
        "search_text",
        "get_file_summary",
        "get_symbol_summary",
        "find_imports",
        "find_importers",
        "find_references",
        "find_related_tests",
        "read_file",
        "read_lines",
        "get_git_diff",
        "suggest_context",
        "build_context_package",
        "inspect_context_package",
    )
    forbidden = {
        "shell",
        "execute",
        "write_file",
        "apply_patch",
        "git_commit",
        "index_build",
        "add_to_context",
        "remove_from_context",
    }
    assert forbidden.isdisjoint(MCP_TOOL_SCHEMAS)
    for schema in MCP_TOOL_SCHEMAS.values():
        assert schema["type"] == "object"


def test_mcp_queries_reads_ranges_relationships_and_rejects_paths(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "core.py", "def target():\n    return 1\n")
    _write(
        tmp_path,
        "use.py",
        "from core import target\ndef use():\n    return target()\n",
    )
    _write(
        tmp_path,
        "tests/test_core.py",
        "from core import target\ndef test_target():\n    assert target() == 1\n",
    )
    foundation = _foundation(tmp_path)

    overview = asyncio.run(foundation.call_tool("repository_overview", {}))
    tree = asyncio.run(foundation.call_tool("list_tree", {"limit": 10}))
    symbols = asyncio.run(foundation.call_tool("search_symbols", {"query": "target"}))
    text = asyncio.run(foundation.call_tool("search_text", {"query": "return target"}))
    summary = asyncio.run(foundation.call_tool("get_file_summary", {"path": "use.py"}))
    imports = asyncio.run(foundation.call_tool("find_imports", {"path": "use.py"}))
    importers = asyncio.run(foundation.call_tool("find_importers", {"path": "core.py"}))
    related = asyncio.run(
        foundation.call_tool("find_related_tests", {"path": "core.py"})
    )
    read = asyncio.run(foundation.call_tool("read_file", {"path": "core.py"}))
    lines = asyncio.run(
        foundation.call_tool(
            "read_lines", {"path": "use.py", "start_line": 2, "end_line": 3}
        )
    )

    assert overview["file_count"] == 3
    assert tree["items"]
    assert symbols["items"]
    symbol_id = symbols["items"][0]["symbol_id"]
    symbol_summary = asyncio.run(
        foundation.call_tool("get_symbol_summary", {"symbol_id": symbol_id})
    )
    references = asyncio.run(
        foundation.call_tool("find_references", {"symbol_id": symbol_id})
    )
    assert symbol_summary["fact"]["symbol_id"] == symbol_id
    assert isinstance(references["items"], list)
    assert text["items"]
    assert summary["facts"]["path"] == "use.py"
    assert imports["items"]
    assert importers["items"]
    assert related["items"]
    assert read["text"].startswith("def target")
    assert "def use" in lines["text"]

    for path in ("../secret", "/etc/passwd", "C:/secret", "C:secret", "\\\\x\\y"):
        with pytest.raises(ReadOnlyToolError) as error:
            asyncio.run(foundation.call_tool("read_file", {"path": path}))
        assert error.value.code == "invalid_input"


def test_mcp_byte_limits_and_no_write_guarantee(tmp_path: Path) -> None:
    _write(tmp_path, "large.txt", "x" * (300 * 1024))
    _write(tmp_path, "small.txt", "small\n")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    foundation = _foundation(tmp_path)

    with pytest.raises(ReadOnlyToolError) as error:
        asyncio.run(foundation.call_tool("read_file", {"path": "large.txt"}))
    assert error.value.code == "limit_exceeded"

    package = asyncio.run(
        foundation.call_tool(
            "build_context_package",
            {
                "task": "Package small",
                "include": ["small.txt"],
                "max_context_bytes": 100,
            },
        )
    )
    assert package["files"][0]["path"] == "small.txt"
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / ".contextforge").exists()


def test_mcp_suggest_build_and_portable_inspection(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "one\ntwo\nthree\n")
    provider = _provider()
    foundation = ReadOnlyMCPFoundation(
        tmp_path,
        provider=provider,
        discovery_mode="fresh",  # type: ignore[arg-type]
    )
    suggestion = asyncio.run(
        foundation.call_tool(
            "suggest_context",
            {"task": "Review app", "discovery": "fresh"},
        )
    )
    package = asyncio.run(
        foundation.call_tool(
            "build_context_package",
            {
                "task": "Ranged package",
                "ranges": [{"path": "app.py", "start_line": 2, "end_line": 2}],
            },
        )
    )
    inspected = asyncio.run(
        foundation.call_tool(
            "inspect_context_package",
            {"package_json": json.dumps(package)},
        )
    )

    assert suggestion["selected"][0]["path"] == "app.py"
    assert package["files"][0]["blocks"][0]["text"] == "two\n"
    assert inspected["inspection"]["selected_file_count"] == 1
    with pytest.raises(ReadOnlyToolError) as unavailable:
        asyncio.run(_foundation(tmp_path).call_tool("suggest_context", {"task": "x"}))
    assert unavailable.value.code == "unavailable"
    asyncio.run(provider.close())


def test_mcp_protocol_initialize_lists_calls_resources_and_no_write_capability(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "VALUE = 1\n")
    server = MCPServer(_foundation(tmp_path))

    initialized = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
    )
    assert initialized is not None
    result = cast(dict[str, Any], initialized["result"])
    assert set(result["capabilities"]) == {"tools", "resources"}
    assert "sampling" not in result["capabilities"]
    assert "prompts" not in result["capabilities"]

    notification = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
    )
    assert notification is None
    assert server.initialized is True

    listed = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
    )
    assert listed is not None
    tools = listed["result"]["tools"]
    assert {item["name"] for item in tools} == set(MCP_TOOL_SCHEMAS)
    assert all(item["annotations"]["readOnlyHint"] for item in tools)

    called = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "app.py"}},
            }
        )
    )
    assert called is not None
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["text"] == "VALUE = 1\n"

    rejected = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_file",
                    "arguments": {"path": "../secret"},
                },
            }
        )
    )
    assert rejected is not None
    assert rejected["result"]["isError"] is True

    resources = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 5, "method": "resources/list", "params": {}}
        )
    )
    assert resources is not None
    assert len(resources["result"]["resources"]) == 4
    overview = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": "contextforge://repository/overview"},
            }
        )
    )
    assert overview is not None
    assert overview["result"]["contents"][0]["mimeType"] == "application/json"


def test_mcp_protocol_errors_are_structured(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "pass\n")
    server = MCPServer(_foundation(tmp_path))

    invalid = asyncio.run(server.handle_message({"jsonrpc": "1.0", "id": 1}))
    unknown = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "write_file", "params": {}}
        )
    )
    bad_params = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": []}
        )
    )

    assert invalid is not None and invalid["error"]["code"] == -32600
    assert unknown is not None and unknown["error"]["data"]["code"] == (
        "method_not_found"
    )
    assert bad_params is not None and bad_params["error"]["code"] == -32602


def test_mcp_inspection_rejects_malformed_package(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "pass\n")
    foundation = _foundation(tmp_path)

    with pytest.raises(ReadOnlyToolError) as error:
        asyncio.run(
            foundation.call_tool("inspect_context_package", {"package_json": "{"})
        )
    assert error.value.code == "invalid_input"

    # A normal package produced outside MCP remains portable input.
    package = build_context_package(tmp_path, ContextBuildOptions())
    inspected = asyncio.run(
        foundation.call_tool(
            "inspect_context_package",
            {"package_json": package.model_dump_json()},
        )
    )
    assert inspected["package"]["schema_version"] == 1


def test_mcp_resources_git_and_unknown_tool_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        str(tmp_path.parent.resolve()),
    )
    _write(tmp_path, "app.py", "pass\n")
    foundation = _foundation(tmp_path)

    diff = asyncio.run(foundation.call_tool("get_git_diff", {"mode": "working"}))
    assert diff["truncated"] is False
    assert diff["text"] == ""

    with pytest.raises(ReadOnlyToolError) as missing_manifest:
        asyncio.run(foundation.read_resource("contextforge://index/manifest"))
    assert missing_manifest.value.code == "unavailable"
    with pytest.raises(ReadOnlyToolError) as missing_architecture:
        asyncio.run(foundation.read_resource("contextforge://architecture"))
    assert missing_architecture.value.code == "unavailable"
    with pytest.raises(ReadOnlyToolError) as missing_features:
        asyncio.run(foundation.read_resource("contextforge://features"))
    assert missing_features.value.code == "unavailable"
    with pytest.raises(ReadOnlyToolError) as unknown_resource:
        asyncio.run(foundation.read_resource("contextforge://unknown"))
    assert unknown_resource.value.code == "not_found"
    with pytest.raises(ReadOnlyToolError) as unknown_tool:
        asyncio.run(foundation.call_tool("write_file", {}))
    assert unknown_tool.value.code == "unknown_tool"
    with pytest.raises(ReadOnlyToolError) as bad_build:
        asyncio.run(
            foundation.call_tool("build_context_package", {"include": ["../secret"]})
        )
    assert bad_build.value.code == "invalid_input"


def test_mcp_server_additional_protocol_branches(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "pass\n")
    server = MCPServer(_foundation(tmp_path))

    fallback = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "unsupported"},
            }
        )
    )
    assert fallback is not None
    assert fallback["result"]["protocolVersion"] == "2025-11-25"

    ping = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
        )
    )
    assert ping is not None and ping["result"] == {}

    no_id = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "method": "tools/list", "params": {}})
    )
    assert no_id is None

    missing_name = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}
        )
    )
    assert missing_name is not None
    assert missing_name["error"]["code"] == -32602

    invalid_call = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": 4, "arguments": []},
            }
        )
    )
    assert invalid_call is not None
    assert invalid_call["error"]["code"] == -32602

    missing_resource = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": "contextforge://index/manifest"},
            }
        )
    )
    assert missing_resource is not None
    assert missing_resource["error"]["data"]["code"] == "unavailable"


class _BinaryStream:
    def __init__(self, content: bytes = b"") -> None:
        self.buffer = io.BytesIO(content)


def test_stdio_protocol_smoke_and_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "app.py", "pass\n")
    incoming = _BinaryStream(
        b"not-json\n"
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
    )
    outgoing = _BinaryStream()
    monkeypatch.setattr(sys, "stdin", incoming)
    monkeypatch.setattr(sys, "stdout", outgoing)

    asyncio.run(server_module._serve_stdio(_foundation(tmp_path)))

    messages = [
        json.loads(line)
        for line in outgoing.buffer.getvalue().decode("utf-8").splitlines()
    ]
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["result"] == {}


def test_mcp_cli_success_usage_operational_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "app.py", "pass\n")
    foundations: list[ReadOnlyMCPFoundation] = []

    def record(foundation: ReadOnlyMCPFoundation) -> None:
        foundations.append(foundation)

    monkeypatch.setattr(mcp_cli, "serve_stdio", record)
    disabled = runner.invoke(app, ["mcp", "serve", str(tmp_path), "--provider", "none"])
    enabled = runner.invoke(app, ["mcp", "serve", str(tmp_path), "--provider", "fake"])

    assert disabled.exit_code == enabled.exit_code == 0
    assert len(foundations) == 2
    assert foundations[0].provider is None
    assert foundations[1].provider is not None

    missing = runner.invoke(
        app,
        [
            "mcp",
            "serve",
            str(tmp_path),
            "--config",
            str(tmp_path / "missing.toml"),
        ],
    )
    assert missing.exit_code == 2

    def operational(foundation: ReadOnlyMCPFoundation) -> None:
        del foundation
        raise OSError("stdio unavailable")

    monkeypatch.setattr(mcp_cli, "serve_stdio", operational)
    failed = runner.invoke(app, ["mcp", "serve", str(tmp_path), "--provider", "none"])
    assert failed.exit_code == 1

    def cancelled(foundation: ReadOnlyMCPFoundation) -> None:
        del foundation
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_cli, "serve_stdio", cancelled)
    interrupted = runner.invoke(
        app, ["mcp", "serve", str(tmp_path), "--provider", "none"]
    )
    assert interrupted.exit_code == 130
