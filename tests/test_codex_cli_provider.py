import asyncio
import json
import shutil
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

import contextforge.models.codex_cli as codex_module
from contextforge.intelligence.cards import _RawSemanticCard
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
    defaults = ProviderConfiguration(
        provider_id="codex",
        endpoint="codex://subscription",
        model_id="gpt-6-sol",
        local_only=False,
        external_data_policy="allow_repository",
        retry_limit=0,
        max_json_repair_attempts=0,
    )
    return ProviderConfiguration.model_validate({**defaults.model_dump(), **values})


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
    items: list[dict[str, object]] = [
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
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
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
    assert isinstance(result.value, _Answer)
    assert result.value.summary == "Runs a function."
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (55, 11)
    assert len(calls) == 3


def test_codex_accepts_chatgpt_login_status_on_stderr() -> None:
    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
        if args[1:3] == ("login", "status"):
            return 0, b"", b"Logged in using ChatGPT\n"
        return 0, _events(), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    response = asyncio.run(provider.complete_structured(_request()))
    assert isinstance(response.value, _Answer)
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
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
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
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
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
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        return 1, b'{"type":"turn.failed","error":"context window limit exceeded"}', b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(ContextWindowExceededError):
        asyncio.run(provider.complete_structured(_request()))
    assert len(calls) == 3
    assert all(args[0] == "codex-test" for args in calls)


def test_codex_cancellation_cancels_running_cli() -> None:
    stopped = asyncio.Event()
    entered = asyncio.Event()

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
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
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
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


def test_codex_projects_dynamic_card_maps_into_strict_schema() -> None:
    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["properties"]["profile_facts"]["type"] == "array"
        assert schema["$defs"]["_RawKeySymbol"]["required"] == [
            "evidence_id",
            "summary",
        ]
        answer = {
            "schema_version": 1,
            "synopsis": {"text": "Serves requests", "evidence_ids": ["e1"]},
            "concepts": [{"text": "server", "evidence_ids": ["e1"]}],
            "responsibilities": [],
            "key_symbols": [],
            "side_effects": [],
            "profile_facts": [
                {
                    "key": "apis",
                    "value": [{"text": "HTTP endpoint", "evidence_ids": ["e1"]}],
                }
            ],
            "inferred_relationships": [],
        }
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(answer)},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        ]
        return 0, b"\n".join(json.dumps(item).encode() for item in events), b""

    request = ModelRequest(
        operation_id="codex-card-schema-test",
        purpose="semantic-card",
        system_instructions="Describe the source.",
        analysis_task="Summarize the supplied file.",
        trusted_code_map_facts={"path": "src/app.py"},
        untrusted_sources=(UntrustedSource.from_text("src/app.py", "def run(): pass"),),
        response_model=_RawSemanticCard,
    )
    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    response = asyncio.run(provider.complete_structured(request))
    assert isinstance(response.value, _RawSemanticCard)
    assert response.value.profile_facts["apis"][0].text == "HTTP endpoint"


def test_codex_rejects_duplicate_dynamic_map_keys() -> None:
    schema = _RawSemanticCard.model_json_schema()
    with pytest.raises(ValueError, match="duplicate"):
        codex_module._restore_dynamic_maps(
            {"profile_facts": [{"key": "apis", "value": []}] * 2}, schema
        )


@pytest.mark.parametrize(
    "value",
    (
        {},
        ["not an entry"],
        [{"key": 7, "value": []}],
        [{"key": "apis"}],
    ),
)
def test_codex_rejects_malformed_dynamic_maps(value: object) -> None:
    with pytest.raises(ValueError, match="dynamic map"):
        codex_module._restore_dynamic_maps(
            {"profile_facts": value}, _RawSemanticCard.model_json_schema()
        )


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    (
        ("object", {}, True),
        ("object", [], False),
        ("array", [], True),
        ("array", {}, False),
        ("null", None, True),
        ("null", "x", False),
        ("string", "x", True),
        ("string", 1, False),
        ("integer", 1, True),
        ("integer", True, False),
        ("number", 1.5, True),
        ("boolean", True, True),
        ("boolean", 1, False),
        ("unknown", "x", True),
    ),
)
def test_codex_restores_only_matching_schema_branches(
    kind: str, value: object, expected: bool
) -> None:
    assert codex_module._matches_schema_shape(value, {"type": kind}) is expected


def test_codex_projection_preserves_nullable_schema_and_closes_objects() -> None:
    projected = codex_module._codex_output_schema(_RawSemanticCard.model_json_schema())
    assert projected["additionalProperties"] is False
    assert set(projected["required"]) == set(projected["properties"])
    key_symbol = projected["$defs"]["_RawKeySymbol"]
    assert key_symbol["additionalProperties"] is False
    assert key_symbol["properties"]["summary"]["anyOf"][-1] == {"type": "null"}


def test_codex_rejects_non_json_structured_message() -> None:
    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        events = (
            {"type": "item.completed", "item": {"type": "agent_message", "text": "{"}},
            {"type": "turn.completed"},
        )
        return 0, b"\n".join(json.dumps(item).encode() for item in events), b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    assert provider.provider_id == "codex"
    assert provider.capabilities().structured_responses
    with pytest.raises(ProviderRequestError, match="invalid structured value"):
        asyncio.run(provider.complete_structured(_request()))
    asyncio.run(provider.close())


def test_codex_subprocess_cancellation_kills_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        entered = asyncio.Event()
        killed = asyncio.Event()

        class Process:
            returncode = None

            async def communicate(self, prompt: bytes | None) -> tuple[bytes, bytes]:
                entered.set()
                await asyncio.Future[None]()
                raise AssertionError("cancelled process continued")

            def kill(self) -> None:
                killed.set()

            async def wait(self) -> int:
                return 1

        async def start(*args: object, **kwargs: object) -> Process:
            return Process()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
        pending = asyncio.create_task(
            codex_module._run_cli(("codex-test", "exec"), b"{}", tmp_path)
        )
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert killed.is_set()

    asyncio.run(exercise())


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
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
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
            _configuration().model_copy(update=change), runner=_unexpected_runner
        )


def test_codex_reports_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ProviderUnavailableError):
        CodexCLIModelProvider(_configuration())


def test_codex_subprocess_start_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def cannot_start(*args: object, **kwargs: object) -> None:
        raise OSError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_start)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(codex_module._run_cli(("codex-test",), b"prompt", tmp_path))


async def _unexpected_runner(
    args: tuple[str, ...], prompt: bytes | None, directory: Path
) -> tuple[int, bytes, bytes]:
    raise AssertionError("runner must not be called")


@pytest.mark.parametrize(
    ("effort", "override"),
    [
        ("low", 'model_reasoning_effort="low"'),
        ("off", 'model_reasoning_effort="none"'),
        ("provider_default", None),
    ],
)
def test_codex_forwards_configured_reasoning_effort(
    effort: str,
    override: str | None,
) -> None:
    async def runner(
        args: tuple[str, ...],
        prompt: bytes | None,
        directory: Path,
    ) -> tuple[int, bytes, bytes]:
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        if override is None:
            assert "--config" not in args
        else:
            assert args[args.index("--config") + 1] == override
        return 0, _events(), b""

    provider = CodexCLIModelProvider(
        _configuration(reasoning_effort=effort),
        runner=runner,
        executable="codex-test",
    )
    result = asyncio.run(provider.complete_structured(_request()))
    assert isinstance(result.value, _Answer)


@pytest.mark.parametrize(
    ("detail", "error_type", "safe_code"),
    [
        (
            "model does not support reasoning effort; invalid reasoning",
            ProviderConfigurationError,
            "reasoning_unsupported",
        ),
        (
            "reasoning effort is not supported for model",
            ProviderConfigurationError,
            "reasoning_unsupported",
        ),
        (
            "unexpected argument --ignore-rules",
            ProviderConfigurationError,
            "cli_arguments",
        ),
        ("invalid schema for model output", ProviderRequestError, "output_schema"),
        ("model unavailable", ProviderModelNotFoundError, "model_unavailable"),
        ("authentication failed", ProviderAuthenticationError, "authentication"),
        ("usage limit reached", ProviderQuotaError, "quota"),
    ],
)
def test_cli_failure_preserves_safe_category_without_backend_secrets(
    detail: str, error_type: type[Exception], safe_code: str
) -> None:
    secret = "sk-do-not-store-this-value"
    for output, stderr in (
        (json.dumps({"type": "error", "error": detail + " " + secret}).encode(), b""),
        (b"", (detail + " " + secret).encode()),
    ):
        with pytest.raises(error_type, match=safe_code) as caught:
            codex_module._parse_cli_result(1, output, stderr)
        assert secret not in str(caught.value)
        assert detail + " " + secret not in str(caught.value)


def test_cli_flag_preflight_rejection_spends_no_dispatch() -> None:
    calls: list[tuple[str, ...]] = []

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        calls.append(args)
        assert prompt is None
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        assert args[1:] == ("exec", "--help")
        return 0, b"--json --model", b""

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(ProviderConfigurationError, match="cli_arguments") as caught:
        asyncio.run(provider.complete_structured(_request()))
    assert caught.value.transport_attempts == 0
    assert caught.value.total_provider_http_calls == 0
    assert len(calls) == 2


def test_reasoning_rejection_counts_one_dispatch_and_never_retries() -> None:
    dispatched = 0

    async def runner(
        args: tuple[str, ...], prompt: bytes | None, directory: Path
    ) -> tuple[int, bytes, bytes]:
        nonlocal dispatched
        if args[1:] == ("exec", "--help"):
            return 0, " ".join(codex_module._REQUIRED_EXEC_FLAGS).encode(), b""
        if args[1:3] == ("login", "status"):
            return 0, b"Logged in using ChatGPT", b""
        dispatched += 1
        return 1, b"", b"reasoning effort is not supported for this model"

    provider = CodexCLIModelProvider(
        _configuration(), runner=runner, executable="codex-test"
    )
    with pytest.raises(
        ProviderConfigurationError, match="reasoning_unsupported"
    ) as caught:
        asyncio.run(provider.complete_structured(_request()))
    assert caught.value.transport_attempts == 1
    assert caught.value.total_provider_http_calls == 1
    assert dispatched == 1
