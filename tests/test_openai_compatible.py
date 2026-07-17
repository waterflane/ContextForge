import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

import contextforge.models.openai_compatible as openai_module
from contextforge.cli.main import app
from contextforge.models import (
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleHTTPResponse,
    OpenAICompatibleModelProvider,
    ProviderCancelledError,
    ProviderConfiguration,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RetryClassification,
    classify_retry,
)
from contextforge.project_config import (
    ProjectConfiguration,
    create_model_provider,
    load_project_configuration,
    resolve_provider_configuration,
)


class _Answer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    answer: str


def _request() -> ModelRequest:
    return ModelRequest(
        operation_id="openai-compatible-test",
        purpose="test-analysis",
        system_instructions="Analyze the supplied facts.",
        analysis_task="Return a short answer.",
        trusted_code_map_facts={"symbol": "run"},
        untrusted_sources=(),
        response_model=_Answer,
        max_output_tokens=123,
    )


def _configuration(**overrides: Any) -> ProviderConfiguration:
    values: dict[str, Any] = {
        "provider_id": "openai-compatible",
        "endpoint": DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
        "model_id": "publisher/exact-model-id",
        "timeout_seconds": 1.0,
        "retry_limit": 0,
    }
    values.update(overrides)
    return ProviderConfiguration.model_validate(values)


def _models(*model_ids: str) -> OpenAICompatibleHTTPResponse:
    return OpenAICompatibleHTTPResponse(
        status=200,
        body=json.dumps(
            {"object": "list", "data": [{"id": item} for item in model_ids]}
        ).encode(),
    )


def _completion(answer: str = "works") -> OpenAICompatibleHTTPResponse:
    return OpenAICompatibleHTTPResponse(
        status=200,
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"schema_version": 1, "answer": answer}
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        ).encode(),
    )


def test_exact_urls_model_schema_and_successful_response_parsing() -> None:
    calls: list[tuple[str, str, bytes | None, Mapping[str, str], int]] = []

    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        calls.append((method, url, body, headers, limit))
        return _models("publisher/exact-model-id") if method == "GET" else _completion()

    async def exercise() -> ModelResponse:
        provider = OpenAICompatibleModelProvider(
            _configuration(endpoint="http://localhost:1234/v1/"),
            transport=transport,
        )
        return await provider.complete_structured(_request())

    response = asyncio.run(exercise())
    assert [(item[0], item[1]) for item in calls] == [
        ("GET", "http://localhost:1234/v1/models"),
        ("POST", "http://localhost:1234/v1/chat/completions"),
    ]
    payload = json.loads(calls[1][2] or b"null")
    assert payload["model"] == "publisher/exact-model-id"
    assert payload["stream"] is False
    assert payload["max_tokens"] == 123
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "openai-compatible-test",
            "schema": _request().response_schema,
            "strict": True,
        },
    }
    assert response.value == _Answer(answer="works")
    assert response.finish_reason == "stop"
    assert response.usage == ModelUsage(input_tokens=11, output_tokens=7)


def test_list_models_parses_exact_ids_without_starting_a_completion() -> None:
    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del body, headers, limit
        assert method == "GET"
        assert url == "http://localhost:1234/v1/models"
        return _models("z-model", "publisher/model-a")

    async def exercise() -> tuple[str, ...]:
        provider = OpenAICompatibleModelProvider(_configuration(), transport=transport)
        return await provider.list_models()

    assert asyncio.run(exercise()) == ("z-model", "publisher/model-a")


@pytest.mark.parametrize("credential_env", [None, "LM_STUDIO_API_KEY"])
def test_optional_authentication_header(credential_env: str | None) -> None:
    seen_headers: list[Mapping[str, str]] = []

    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del url, body, limit
        seen_headers.append(headers)
        return _models("publisher/exact-model-id") if method == "GET" else _completion()

    async def exercise() -> None:
        provider = OpenAICompatibleModelProvider(
            _configuration(credential_env=credential_env),
            transport=transport,
            environment={"LM_STUDIO_API_KEY": "local-secret"},
        )
        await provider.complete_structured(_request())

    asyncio.run(exercise())
    expected = None if credential_env is None else "Bearer local-secret"
    assert [item.get("Authorization") for item in seen_headers] == [expected, expected]


def test_exact_model_id_must_appear_in_model_diagnostics() -> None:
    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del method, url, body, headers, limit
        return _models("publisher/similar-model-id")

    async def exercise() -> None:
        provider = OpenAICompatibleModelProvider(_configuration(), transport=transport)
        with pytest.raises(ProviderRequestError, match="exact returned ID"):
            await provider.complete_structured(_request())

    asyncio.run(exercise())


def test_http_404_reports_model_error_body_when_safe() -> None:
    call_count = 0

    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        nonlocal call_count
        del url, body, headers, limit
        call_count += 1
        if method == "GET":
            return _models("publisher/exact-model-id")
        return OpenAICompatibleHTTPResponse(
            status=404,
            body=b'{"error":{"message":"model was unloaded"}}',
        )

    async def exercise() -> None:
        provider = OpenAICompatibleModelProvider(_configuration(), transport=transport)
        with pytest.raises(ProviderRequestError, match="model was unloaded"):
            await provider.complete_structured(_request())

    asyncio.run(exercise())
    assert call_count == 2


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{", "malformed JSON"),
        (b'{"choices":[]}', "completion choice"),
        (b'{"choices":[{"message":{"content":1}}]}', "model content"),
    ],
)
def test_malformed_openai_compatible_response_is_clear(
    body: bytes, message: str
) -> None:
    async def transport(
        method: str,
        url: str,
        request_body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del url, request_body, headers, limit
        return (
            _models("publisher/exact-model-id")
            if method == "GET"
            else OpenAICompatibleHTTPResponse(status=200, body=body)
        )

    async def exercise() -> None:
        provider = OpenAICompatibleModelProvider(_configuration(), transport=transport)
        with pytest.raises(ProviderUnavailableError, match=message):
            await provider.complete_structured(_request())

    asyncio.run(exercise())


def test_timeout_and_explicit_cancellation_are_typed() -> None:
    async def blocked_transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del method, url, body, headers, limit
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def timeout_exercise() -> None:
        provider = OpenAICompatibleModelProvider(
            _configuration(timeout_seconds=0.01), transport=blocked_transport
        )
        with pytest.raises(ProviderTimeoutError):
            await provider.complete_structured(_request())

    async def cancellation_exercise() -> None:
        provider = OpenAICompatibleModelProvider(
            _configuration(), transport=blocked_transport
        )
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            provider.complete_structured(_request(), cancellation=cancellation)
        )
        await asyncio.sleep(0)
        cancellation.set()
        with pytest.raises(ProviderCancelledError):
            await task

    asyncio.run(timeout_exercise())
    asyncio.run(cancellation_exercise())


def test_retry_classification_and_bounded_retry() -> None:
    chat_calls = 0

    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        nonlocal chat_calls
        del url, body, headers, limit
        if method == "GET":
            return _models("publisher/exact-model-id")
        chat_calls += 1
        if chat_calls == 1:
            return OpenAICompatibleHTTPResponse(
                status=503, body=b'{"error":{"message":"loading model"}}'
            )
        return _completion("retried")

    async def exercise() -> ModelResponse:
        provider = OpenAICompatibleModelProvider(
            _configuration(retry_limit=1), transport=transport
        )
        return await provider.complete_structured(_request())

    response = asyncio.run(exercise())
    assert chat_calls == 2
    assert response.value == _Answer(answer="retried")
    assert response.diagnostic is not None
    assert response.diagnostic.retry_count == 1
    assert (
        classify_retry(ProviderUnavailableError("offline"))
        is RetryClassification.RETRYABLE
    )
    assert (
        classify_retry(ProviderRequestError("bad model"))
        is RetryClassification.NON_RETRYABLE
    )


def test_auth_error_redacts_loaded_credential_and_keeps_safe_body() -> None:
    secret = "credential-that-must-not-leak"

    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del method, url, body, headers, limit
        return OpenAICompatibleHTTPResponse(
            status=401,
            body=json.dumps({"error": {"message": f"invalid token {secret}"}}).encode(),
        )

    async def exercise() -> None:
        provider = OpenAICompatibleModelProvider(
            _configuration(credential_env="LM_STUDIO_API_KEY"),
            transport=transport,
            environment={"LM_STUDIO_API_KEY": secret},
        )
        with pytest.raises(ProviderRequestError) as captured:
            await provider.complete_structured(_request())
        assert secret not in str(captured.value)
        assert "[REDACTED]" in str(captured.value)
        assert "HTTP 401" in str(captured.value)

    asyncio.run(exercise())


def test_structured_output_rejection_is_non_retryable_and_includes_detail() -> None:
    async def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del url, body, headers, limit
        if method == "GET":
            return _models("publisher/exact-model-id")
        return OpenAICompatibleHTTPResponse(
            status=400,
            body=b'{"error":{"message":"json_schema is unsupported"}}',
        )

    async def exercise() -> None:
        provider = OpenAICompatibleModelProvider(
            _configuration(retry_limit=2), transport=transport
        )
        with pytest.raises(
            ProviderRequestError, match="structured output.*json_schema is unsupported"
        ):
            await provider.complete_structured(_request())

    asyncio.run(exercise())


def test_project_configuration_and_cli_precedence_are_secret_free(
    tmp_path: Path,
) -> None:
    config_directory = tmp_path / ".contextforge"
    config_directory.mkdir()
    (config_directory / "config.toml").write_text(
        """config_version = 1

[models]
provider = "lmstudio"
model = "configured/model"
base_url = "http://localhost:1234/v1"
concurrency_limit = 3
credential_env = "LM_STUDIO_API_KEY"
""",
        encoding="utf-8",
    )
    project = load_project_configuration(tmp_path)

    configured = resolve_provider_configuration(project)
    overridden = resolve_provider_configuration(
        project,
        provider="openai-compatible",
        model="cli/model",
        base_url="http://localhost:9999/v1",
        concurrency=5,
    )

    assert configured is not None
    assert configured.provider_id == "openai-compatible"
    assert configured.model_id == "configured/model"
    assert configured.endpoint == "http://localhost:1234/v1"
    assert configured.concurrency_limit == 3
    assert configured.credential_env == "LM_STUDIO_API_KEY"
    assert overridden is not None
    assert overridden.model_id == "cli/model"
    assert overridden.endpoint == "http://localhost:9999/v1"
    assert overridden.concurrency_limit == 5
    assert "LM_STUDIO_API_KEY" in overridden.model_dump_json()
    assert "local-secret" not in overridden.model_dump_json()
    assert isinstance(create_model_provider(overridden), OpenAICompatibleModelProvider)


def test_lmstudio_alias_uses_default_base_url_but_requires_an_exact_model() -> None:
    project = ProjectConfiguration()
    resolved = resolve_provider_configuration(
        project, provider="lmstudio", model="exact/id"
    )

    assert resolved is not None
    assert resolved.provider_id == "openai-compatible"
    assert resolved.endpoint == DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    assert resolved.model_id == "exact/id"


def test_cli_help_and_provider_selection_include_base_url(tmp_path: Path) -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["index", "build", "--help"])
    selected = runner.invoke(
        app,
        [
            "index",
            "build",
            str(tmp_path),
            "--provider",
            "lmstudio",
            "--model",
            "exact/id",
            "--base-url",
            "ftp://invalid.example/v1",
        ],
    )

    assert help_result.exit_code == 0
    assert "--base-url" in help_result.output
    assert selected.exit_code == 1
    assert "OpenAI-compatible base URL must be an HTTP URL" in selected.output


def test_provider_capabilities_close_and_configuration_policy() -> None:
    provider = OpenAICompatibleModelProvider(_configuration())
    assert provider.provider_id == "openai-compatible"
    assert provider.capabilities().model_dump() == {
        "structured_responses": True,
        "cancellation": True,
        "token_usage": True,
        "local": True,
    }

    with pytest.raises(ValueError, match="provider_id"):
        OpenAICompatibleModelProvider(
            ProviderConfiguration(
                provider_id="fake",
                endpoint="http://localhost:1234/v1",
                model_id="model",
            )
        )
    with pytest.raises(ValueError):
        _configuration(endpoint="http://user@localhost:1234/v1")
    for endpoint in (
        "http://localhost:1234/v1?key=value",
        "http://localhost:1234/v1#fragment",
    ):
        with pytest.raises(ProviderConfigurationError, match="credentials, a query"):
            OpenAICompatibleModelProvider(_configuration(endpoint=endpoint))
    with pytest.raises(ProviderConfigurationError, match="loopback"):
        OpenAICompatibleModelProvider(
            _configuration(endpoint="https://example.test/v1")
        )

    async def exercise_close() -> None:
        await provider.close()
        with pytest.raises(ProviderRequestError, match="closed"):
            await provider.list_models()

    asyncio.run(exercise_close())


@pytest.mark.parametrize(
    ("status", "operation", "error_type", "message"),
    [
        (403, "model diagnostics", ProviderRequestError, "authentication"),
        (404, "model diagnostics", ProviderRequestError, "model diagnostics"),
        (418, "chat completion", ProviderRequestError, "chat completion"),
        (408, "chat completion", ProviderUnavailableError, "HTTP 408"),
        (429, "chat completion", ProviderUnavailableError, "HTTP 429"),
        (500, "chat completion", ProviderUnavailableError, "HTTP 500"),
        (422, "chat completion", ProviderRequestError, "structured output"),
    ],
)
def test_http_status_classification(
    status: int,
    operation: str,
    error_type: type[ProviderRequestError | ProviderUnavailableError],
    message: str,
) -> None:
    response = OpenAICompatibleHTTPResponse(
        status=status, body=b'{"message":"safe detail"}'
    )
    with pytest.raises(error_type, match=message):
        openai_module._raise_for_status(
            response, operation=operation, model_id="exact/model"
        )


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{}",
        b'{"data":[1]}',
        b'{"data":[{"id":""}]}',
        b'{"data":[{"id":"same"},{"id":"same"}]}',
        b"\xff",
    ],
)
def test_malformed_model_lists_are_rejected(body: bytes) -> None:
    with pytest.raises(ProviderUnavailableError):
        openai_module._parse_model_list(body)


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [1]},
        {"choices": [{"message": {"content": "{}"}, "finish_reason": 1}]},
        {"choices": [{"message": {"content": "{}"}}], "usage": 1},
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": -1},
        },
    ],
)
def test_malformed_completion_metadata_is_rejected(payload: object) -> None:
    with pytest.raises(ProviderUnavailableError):
        openai_module._parse_chat_completion(json.dumps(payload).encode())


def test_transport_exception_and_list_diagnostic_redaction() -> None:
    secret = "diagnostic-secret"

    async def broken_transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del method, url, body, headers, limit
        raise OSError("connection refused")

    async def auth_transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del method, url, body, headers, limit
        return OpenAICompatibleHTTPResponse(
            status=403,
            body=json.dumps({"detail": f"bad {secret}"}).encode(),
        )

    async def exercise() -> None:
        unavailable = OpenAICompatibleModelProvider(
            _configuration(), transport=broken_transport
        )
        with pytest.raises(ProviderUnavailableError, match="unavailable"):
            await unavailable.list_models()
        authenticated = OpenAICompatibleModelProvider(
            _configuration(credential_env="LM_STUDIO_API_KEY"),
            transport=auth_transport,
            environment={"LM_STUDIO_API_KEY": secret},
        )
        with pytest.raises(ProviderRequestError) as captured:
            await authenticated.list_models()
        assert secret not in str(captured.value)
        assert "[REDACTED]" in str(captured.value)

    asyncio.run(exercise())


def test_list_models_timeout_pre_cancel_and_task_cancel() -> None:
    async def blocked_transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        limit: int,
    ) -> OpenAICompatibleHTTPResponse:
        del method, url, body, headers, limit
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def exercise() -> None:
        timed = OpenAICompatibleModelProvider(
            _configuration(timeout_seconds=0.01), transport=blocked_transport
        )
        with pytest.raises(ProviderTimeoutError):
            await timed.list_models()

        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(ProviderCancelledError):
            await timed.list_models(cancellation=cancellation)

        task = asyncio.create_task(timed.list_models())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(ProviderCancelledError):
            await task

    asyncio.run(exercise())


def test_safe_error_detail_only_returns_bounded_structured_messages() -> None:
    assert openai_module._safe_error_detail(b"not-json") is None
    assert openai_module._safe_error_detail(b"[]") is None
    assert openai_module._safe_error_detail(b'{"error":1}') is None
    assert (
        openai_module._safe_error_detail(b'{"error":"  safe   message  "}')
        == "safe message"
    )
    assert (
        len(
            openai_module._safe_error_detail(
                json.dumps({"error": {"detail": "x" * 3_000}}).encode()
            )
            or ""
        )
        == 2_000
    )


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_http_parsing_and_bounded_body_helpers() -> None:
    status, headers = openai_module._parse_http_headers(
        b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\n"
    )
    assert status == 201
    assert headers == {"content-length": "2"}
    with pytest.raises(ProviderUnavailableError, match="HTTP headers"):
        openai_module._parse_http_headers(b"bad\r\n\r\n")

    async def exercise() -> None:
        assert (
            await openai_module._read_body(_reader(b"ok"), {"content-length": "2"}, 2)
            == b"ok"
        )
        assert await openai_module._read_body(_reader(b"ok"), {}, 2) == b"ok"
        for length in ("bad", "-1", "3"):
            with pytest.raises(ProviderUnavailableError):
                await openai_module._read_body(
                    _reader(b"abc"), {"content-length": length}, 2
                )
        with pytest.raises(ProviderUnavailableError, match="too large"):
            await openai_module._read_body(_reader(b"abc"), {}, 2)
        assert (
            await openai_module._read_chunked(_reader(b"2\r\nok\r\n0\r\n\r\n"), 2)
            == b"ok"
        )
        for data in (b"bad\r\n", b"3\r\nabc\r\n", b"1\r\naXX"):
            with pytest.raises(ProviderUnavailableError):
                await openai_module._read_chunked(_reader(data), 2)

    asyncio.run(exercise())


def test_standard_library_transport_against_mock_loopback_http() -> None:
    requests: list[bytes] = []

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_head = await reader.readuntil(b"\r\n\r\n")
        requests.append(request_head)
        if request_head.startswith(b"GET"):
            response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
        else:
            length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in request_head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            await reader.readexactly(length)
            response = (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"2\r\n{}\r\n0\r\n\r\n"
            )
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def exercise() -> tuple[OpenAICompatibleHTTPResponse, ...]:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        socket = server.sockets[0]
        port = socket.getsockname()[1]
        try:
            first = await openai_module._request_json(
                "GET",
                f"http://127.0.0.1:{port}/v1/models?diagnostic=1",
                None,
                {"Accept": "application/json"},
                100,
            )
            second = await openai_module._request_json(
                "POST",
                f"http://127.0.0.1:{port}/v1/chat/completions",
                b"{}",
                {"Content-Type": "application/json"},
                100,
            )
            return first, second
        finally:
            server.close()
            await server.wait_closed()

    first, second = asyncio.run(exercise())
    assert first == OpenAICompatibleHTTPResponse(status=200, body=b"{}")
    assert second == OpenAICompatibleHTTPResponse(status=200, body=b"{}")
    assert requests[0].startswith(b"GET /v1/models?diagnostic=1 HTTP/1.1")
    assert requests[1].startswith(b"POST /v1/chat/completions HTTP/1.1")
