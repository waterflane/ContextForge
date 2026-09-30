import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

import contextforge.models.codex_cli as codex_module
from contextforge.models import (
    ContextWindowExceededError,
    ModelRequest,
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderConfiguration,
    ProviderConfigurationError,
    ProviderModelNotFoundError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
    UntrustedSource,
)
from contextforge.models.codex_cli import CodexCLIModelProvider
from contextforge.project_config import (
    ProjectConfiguration,
    ProjectModelSettings,
    resolve_provider_configuration,
)


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    summary: str


def _configuration(**values: object) -> ProviderConfiguration:
    return ProviderConfiguration(
        provider_id="codex",
        endpoint="codex://subscription",
        model_id="gpt-6-sol",
        local_only=False,
        external_data_policy="allow_repository",
        retry_limit=0,
        max_json_repair_attempts=0,
        **values,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        operation_id="codex-test",
        purpose="semantic-card",
        system_instructions="Describe the source.",
        analysis_task="Summarize the supplied file.",
        trusted_code_map_facts={"path": "src/app.py"},
        untrusted_sources=(
            UntrustedSource.from_text("src/app.py", "def run():\n    return 1\n"),
        ),
        response_model=_Answer,
        max_output_tokens=128,
    )


def _events(*, tool: bool = False) -> bytes:
    items = [
        {"type": "thread.started", "thread_id": "test"},
        {"type": "turn.started"},
    ]
    if tool:
        items.append(
            {
                "type": "item.started",
                "item": {"id": "tool", "type": "command_execution"},
            }
        )
    items.extend(
        [
            {
                "type": "item.completed",
                "item": {
                    "id": "answer",
                    "type": "agent_message",
                    "text": '{"schema_version":1,"summary":"Runs a function."}',
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 55, "output_tokens": 11},
            },
        ]
    )
    return b"\n".join(json.dumps(item).encode() for item in items)


def test_codex_exec_uses_subscription_schema_stdin_and_usage() -> None:
    calls: list[tuple[str, ...]] = []

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        calls.append(args)
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        assert prompt is not None
        assert b"def run()" in prompt
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert "--ignore-rules" in args
        schema = Path(args[args.index("--output-schema") + 1])
        assert schema.parent == directory
        assert json.loads(schema.read_text(encoding="utf-8"))["type"] == "object"
        return 0, _events(), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    result = asyncio.run(provider.complete_structured(_request()))
    assert result.value.summary == "Runs a function."
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (55, 11)
    assert len(calls) == 2


def test_codex_accepts_chatgpt_login_status_on_stderr() -> None:
    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:3] == ("login", "status"):
            return 0, b"", b"Logged in using ChatGPT\n"
        return 0, _events(), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    response = asyncio.run(provider.complete_structured(_request()))
    assert response.value.summary == "Runs a function."


@pytest.mark.parametrize("status", [b"Not logged in", b"Logged in using an API key"])
def test_codex_rejects_missing_or_api_key_auth(status: bytes) -> None:
    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        assert args[1:3] == ("login", "status")
        return 0, status, b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(ProviderAuthenticationError):
        asyncio.run(provider.complete_structured(_request()))


def test_codex_rejects_tool_actions_and_classifies_quota() -> None:
    async def tool_runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        return 0, _events(tool=True), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=tool_runner, executable="codex-test"
    )
    with pytest.raises(ProviderRequestError, match="tool action"):
        asyncio.run(provider.complete_structured(_request()))

    async def quota_runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        return 1, b'{"type":"error","error":"usage limit reached"}', b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=quota_runner, executable="codex-test"
    )
    with pytest.raises(ProviderQuotaError):
        asyncio.run(provider.complete_structured(_request()))


def test_codex_classifies_context_limit_without_switching_provider() -> None:
    calls: list[tuple[str, ...]] = []

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        calls.append(args)
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        return 1, b'{"type":"turn.failed","error":"context window limit exceeded"}', b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(ContextWindowExceededError):
        asyncio.run(provider.complete_structured(_request()))
    assert len(calls) == 2
    assert all(args[0] == "codex-test" for args in calls)


def test_codex_cancellation_cancels_running_cli() -> None:
    stopped = asyncio.Event()
    entered = asyncio.Event()

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    async def exercise() -> None:
        provider = CodexCLIModelProvider(
            _configuration(), runner=runner, executable="codex-test"
        )
        cancellation = asyncio.Event()
        pending = asyncio.create_task(
            provider.complete_structured(_request(), cancellation=cancellation)
        )
        await entered.wait()
        cancellation.set()
        with pytest.raises(ProviderCancelledError):
            await pending
        assert stopped.is_set()

    asyncio.run(exercise())


def test_codex_configuration_requires_explicit_remote_consent_and_model() -> None:
    async def no_run(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        raise AssertionError("provider should not make a CLI call")

    project = ProjectConfiguration(
        models=ProjectModelSettings(
            provider="codex",
            model="gpt-6-sol",
            local_only=False,
            external_data_policy="allow_repository",
        )
    )
    resolved = resolve_provider_configuration(project)
    assert resolved is not None
    assert resolved.endpoint == "codex://subscription"
    assert isinstance(
        CodexCLIModelProvider(resolved, runner=no_run),
        CodexCLIModelProvider,
    )
    with pytest.raises(ProviderConfigurationError):
        CodexCLIModelProvider(
            _configuration().model_copy(update={"local_only": True}),
            runner=no_run,
        )


@pytest.mark.parametrize(
    ("detail", "error_type"),
    (
        ("rate limit reached", ProviderRateLimitError),
        ("model not found", ProviderModelNotFoundError),
        ("authentication failed", ProviderAuthenticationError),
        ("unexpected failure", ProviderRequestError),
    ),
)
def test_codex_jsonl_error_classification(
    detail: str, error_type: type[Exception]
) -> None:
    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        return 1, json.dumps({"type": "error", "error": detail}).encode(), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(error_type):
        asyncio.run(provider.complete_structured(_request()))


def test_codex_rejects_unavailable_chatgpt_model_as_model_error() -> None:
    stream = b"\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "error",
                        "message": (
                            "The gpt-6-sol model is not supported when using Codex "
                            "with a ChatGPT account."
                        ),
                    },
                }
            ).encode(),
            json.dumps(
                {"type": "turn.failed", "error": {"message": "model not supported"}}
            ).encode(),
        )
    )
    with pytest.raises(ProviderModelNotFoundError):
        codex_module._parse_cli_result(1, stream)


@pytest.mark.parametrize(
    "event_stream", [b"not-json", b"[]", b'{"type":"turn.completed"}']
)
def test_codex_rejects_invalid_or_incomplete_event_stream(event_stream: bytes) -> None:
    with pytest.raises(ProviderRequestError):
        codex_module._parse_cli_result(0, event_stream)


def test_codex_rejects_oversized_event_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_module, "_MAX_CLI_OUTPUT_BYTES", 8)

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        return 0, _events(), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(ProviderRequestError, match="byte limit"):
        asyncio.run(provider.complete_structured(_request()))


@pytest.mark.parametrize(
    "change",
    (
        {"provider_id": "ollama"},
        {"external_data_policy": "deny"},
        {"credential_env": "SOME_API_KEY"},
        {"model_id": "qwen2.5-coder:7b"},
    ),
)
def test_codex_rejects_incompatible_provider_configuration(
    change: dict[str, object],
) -> None:
    with pytest.raises(ProviderConfigurationError):
        CodexCLIModelProvider(
            _configuration().model_copy(update=change), runner=lambda *_: None
        )


def test_codex_reports_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_module.shutil, "which", lambda _: None)
    with pytest.raises(ProviderUnavailableError):
        CodexCLIModelProvider(_configuration())


def test_codex_subprocess_start_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def cannot_start(*args: object, **kwargs: object) -> None:
        raise OSError("missing")

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", cannot_start)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(codex_module._run_cli(("codex-test",), b"prompt", tmp_path))
