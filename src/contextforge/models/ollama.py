"""Local Ollama structured-output adapter using bounded standard-library HTTP."""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from contextforge.logging import LogLevel, emit, sanitize_url
from contextforge.models.providers import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderConfigurationError,
    ProviderRuntime,
    ProviderTransportResponse,
    ProviderUnavailableError,
)

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api/chat"
MAX_HTTP_HEADER_BYTES = 64 * 1024

OllamaTransport = Callable[[str, bytes, Mapping[str, str], int], Awaitable[bytes]]


class OllamaModelProvider:
    """Provider adapter for Ollama's non-streaming ``/api/chat`` contract."""

    def __init__(
        self,
        configuration: ProviderConfiguration,
        *,
        transport: OllamaTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if configuration.provider_id != "ollama":
            raise ValueError("Ollama configuration must use provider_id='ollama'")
        _validate_ollama_endpoint(configuration)
        self.configuration = configuration
        self._transport = transport or _post_json
        self._runtime = ProviderRuntime(configuration, environment=environment)

    @property
    def provider_id(self) -> str:
        return "ollama"

    def capabilities(self) -> ProviderCapabilities:
        hostname = urlsplit(self.configuration.endpoint).hostname
        return ProviderCapabilities(
            structured_responses=True,
            cancellation=True,
            token_usage=True,
            local=hostname is not None and _is_loopback_host(hostname),
        )

    async def complete_structured(
        self,
        request: ModelRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> ModelResponse:
        return await self._runtime.execute(
            request, self._complete_once, cancellation=cancellation
        )

    async def close(self) -> None:
        await self._runtime.close()

    async def _complete_once(
        self, request: ModelRequest, credential: SecretStr | None
    ) -> ProviderTransportResponse:
        messages = [
            message.model_dump(mode="json")
            for message in request.messages(include_response_schema=False)
        ]
        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        payload = json.dumps(
            {
                "model": self.configuration.model_id,
                "messages": messages,
                "stream": False,
                "format": (
                    request.response_schema
                    if request.schema_mode == "json_schema"
                    else "json"
                ),
                "options": options,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential.get_secret_value()
        transport_limit = self.configuration.max_response_bytes + MAX_HTTP_HEADER_BYTES
        started = time.monotonic()
        emit(
            "provider",
            "provider.http.dispatch",
            "Dispatching a bounded Ollama HTTP request.",
            level=LogLevel.TRACE,
            request_id=request.operation_id,
            data={
                "http_method": "POST",
                "endpoint": sanitize_url(self.configuration.endpoint),
                "request_body_bytes": len(payload),
                "authorization_configured": credential is not None,
            },
        )
        try:
            raw = await self._transport(
                self.configuration.endpoint,
                payload,
                headers,
                transport_limit,
            )
            emit(
                "provider",
                "provider.http.response_body_received",
                "Received complete bounded Ollama response body.",
                level=LogLevel.TRACE,
                request_id=request.operation_id,
                duration_ms=round((time.monotonic() - started) * 1_000),
                data={"response_byte_length": len(raw)},
            )
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            emit(
                "provider",
                "provider.http.failed",
                "Ollama HTTP transport failed.",
                level=LogLevel.DEBUG,
                request_id=request.operation_id,
                duration_ms=round((time.monotonic() - started) * 1_000),
                error=exc,
                error_code="provider_connection_error",
                transient=True,
                retryable=True,
                data={"endpoint": sanitize_url(self.configuration.endpoint)},
            )
            raise ProviderUnavailableError("Ollama request failed") from exc
        return _parse_ollama_envelope(raw)


def _validate_ollama_endpoint(configuration: ProviderConfiguration) -> None:
    parsed = urlsplit(configuration.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("Ollama endpoint must be an HTTP URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ProviderConfigurationError(
            "Ollama endpoint must not contain credentials or a fragment"
        )
    is_loopback = _is_loopback_host(parsed.hostname)
    if configuration.local_only and not is_loopback:
        raise ProviderConfigurationError(
            "local-only policy requires a loopback Ollama endpoint"
        )
    if not is_loopback and configuration.external_data_policy != "allow_repository":
        raise ProviderConfigurationError(
            "an external Ollama endpoint requires "
            "external_data_policy='allow_repository'"
        )


def _is_loopback_host(hostname: str) -> bool:
    return hostname.lower() in {"127.0.0.1", "::1", "localhost"}


def _parse_ollama_envelope(data: bytes) -> ProviderTransportResponse:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderUnavailableError("Ollama returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailableError("Ollama returned an invalid response")
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ProviderUnavailableError("Ollama response did not contain model content")
    prompt_tokens = _optional_non_negative_int(payload.get("prompt_eval_count"))
    output_tokens = _optional_non_negative_int(payload.get("eval_count"))
    finish_reason = payload.get("done_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderUnavailableError("Ollama returned an invalid finish reason")
    usage = (
        ModelUsage(input_tokens=prompt_tokens, output_tokens=output_tokens)
        if prompt_tokens is not None or output_tokens is not None
        else None
    )
    return ProviderTransportResponse(
        text=message["content"],
        finish_reason=finish_reason,
        usage=usage,
    )


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ProviderUnavailableError("Ollama returned invalid token usage")
    return value


async def _post_json(
    endpoint: str,
    body: bytes,
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> bytes:
    parsed = urlsplit(endpoint)
    assert parsed.hostname is not None
    secure = parsed.scheme == "https"
    port = parsed.port or (443 if secure else 80)
    ssl_context = ssl.create_default_context() if secure else None
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    reader, writer = await asyncio.open_connection(
        parsed.hostname,
        port,
        ssl=ssl_context,
        server_hostname=parsed.hostname if secure else None,
    )
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        host = parsed.hostname
        if parsed.port is not None:
            host += f":{parsed.port}"
        request_headers = {
            "Host": host,
            "Content-Length": str(len(body)),
            "Connection": "close",
            **headers,
        }
        head = (
            f"POST {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
            + "\r\n"
        ).encode("ascii")
        writer.write(head + body)
        await writer.drain()
        raw_headers = await reader.readuntil(b"\r\n\r\n")
        if len(raw_headers) > MAX_HTTP_HEADER_BYTES:
            raise ProviderUnavailableError("Ollama response headers are too large")
        status, response_headers = _parse_http_headers(raw_headers)
        if not 200 <= status < 300:
            raise ProviderUnavailableError(f"Ollama returned HTTP status {status}")
        if response_headers.get("transfer-encoding", "").lower() == "chunked":
            return await _read_chunked(reader, max_response_bytes)
        length_text = response_headers.get("content-length")
        if length_text is not None:
            try:
                length = int(length_text)
            except ValueError as exc:
                raise ProviderUnavailableError(
                    "Ollama returned an invalid content length"
                ) from exc
            if length < 0 or length > max_response_bytes:
                raise ProviderUnavailableError("Ollama response is too large")
            return await reader.readexactly(length)
        data = await reader.read(max_response_bytes + 1)
        if len(data) > max_response_bytes:
            raise ProviderUnavailableError("Ollama response is too large")
        return data
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def _parse_http_headers(data: bytes) -> tuple[int, dict[str, str]]:
    try:
        lines = data.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers = {
            key.strip().lower(): value.strip()
            for line in lines[1:]
            if line
            for key, value in (line.split(":", 1),)
        }
    except (IndexError, ValueError) as exc:
        raise ProviderUnavailableError("Ollama returned invalid HTTP headers") from exc
    return status, headers


async def _read_chunked(reader: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while True:
        line = await reader.readline()
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise ProviderUnavailableError(
                "Ollama returned invalid chunked data"
            ) from exc
        if size == 0:
            await reader.readuntil(b"\r\n")
            return bytes(result)
        if size < 0 or len(result) + size > limit:
            raise ProviderUnavailableError("Ollama response is too large")
        result.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise ProviderUnavailableError("Ollama returned invalid chunked data")


__all__ = ["DEFAULT_OLLAMA_ENDPOINT", "OllamaModelProvider", "OllamaTransport"]
