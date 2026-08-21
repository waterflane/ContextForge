"""Local Ollama structured-output adapter using bounded standard-library HTTP."""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
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
    ProviderRequestError,
    ProviderRuntime,
    ProviderTransportResponse,
    ProviderUnavailableError,
    StructuredOutputSchemaUnsupportedError,
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
            request, self._complete_with_schema_fallback, cancellation=cancellation
        )

    async def close(self) -> None:
        await self._runtime.close()

    async def _complete_with_schema_fallback(
        self, request: ModelRequest, credential: SecretStr | None
    ) -> ProviderTransportResponse:
        try:
            return await self._complete_once(request, credential)
        except StructuredOutputSchemaUnsupportedError as exc:
            if request.schema_mode != "json_schema":
                raise
            emit(
                "provider",
                "provider.structured_output_mode.rejected",
                "Provider rejected a structured-output mode; continuing safely.",
                level=LogLevel.WARNING,
                request_id=request.operation_id,
                top_level_operation_id=request.top_level_operation_id,
                parent_operation_id=request.parent_operation_id,
                phase_id="provider_http_response",
                error=exc,
                error_code="structured_output_schema_unsupported",
                fallback_selected=True,
                data={
                    "rejected_parameter": exc.rejected_parameter,
                    "rejected_value": exc.rejected_value,
                    "safe_error_code": "structured_output_schema_unsupported",
                    "fallback_mode": "plain_json",
                },
            )
            fallback_request = replace(
                request,
                schema_mode="plain_json",
                metadata={**request.metadata, "schema_mode": "plain_json"},
            )
            try:
                response = await self._complete_once(fallback_request, credential)
            except (ProviderRequestError, ProviderUnavailableError) as fallback_error:
                fallback_error.add_http_accounting(
                    transport_attempts=1, total_provider_http_calls=1
                )
                raise
            return ProviderTransportResponse(
                text=response.text,
                finish_reason=response.finish_reason,
                usage=response.usage,
                provider_discovery_calls=response.provider_discovery_calls,
                provider_capability_calls=response.provider_capability_calls,
                transport_attempts=response.transport_attempts + 1,
                provider_http_calls=response.provider_http_calls + 1,
            )

    async def _complete_once(
        self, request: ModelRequest, credential: SecretStr | None
    ) -> ProviderTransportResponse:
        messages = [
            message.model_dump(mode="json")
            for message in request.messages(
                include_response_schema=request.schema_mode != "json_schema"
            )
        ]
        options: dict[str, Any] = {
            "num_ctx": self.configuration.context_window,
            "temperature": request.temperature,
        }
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        native_schema = _ollama_native_schema(request.response_schema)
        tool_schemas = request.trusted_code_map_facts.get("tool_schemas")
        if request.purpose == "repository-discovery" and isinstance(tool_schemas, dict):
            native_schema = _ollama_discovery_action_schema(native_schema, tool_schemas)
        payload = json.dumps(
            {
                "model": self.configuration.model_id,
                "messages": messages,
                "stream": False,
                "format": (
                    native_schema if request.schema_mode == "json_schema" else "json"
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
                "provider_call_kind": "model_transport",
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
        except (ProviderRequestError, ProviderUnavailableError):
            raise
        except StructuredOutputSchemaUnsupportedError:
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
        if response_headers.get("transfer-encoding", "").lower() == "chunked":
            data = await _read_chunked(reader, max_response_bytes)
        else:
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
                data = await reader.readexactly(length)
            else:
                data = await reader.read(max_response_bytes + 1)
                if len(data) > max_response_bytes:
                    raise ProviderUnavailableError("Ollama response is too large")
        _raise_for_ollama_http_error(status, data)
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


def _raise_for_ollama_http_error(status: int, data: bytes) -> None:
    if 200 <= status < 300:
        return
    detail = _safe_ollama_error_detail(data)
    lowered = "" if detail is None else detail.casefold()
    if status in {400, 422} and any(
        marker in lowered
        for marker in (
            "failed to initialize samplers",
            "failed to parse grammar",
            "json schema",
            "sane defaults",
        )
    ):
        raise StructuredOutputSchemaUnsupportedError(
            "Ollama rejected the structured output schema"
        )
    if status in {400, 401, 403, 404, 422}:
        raise ProviderRequestError(f"Ollama rejected the request (HTTP {status})")
    raise ProviderUnavailableError(f"Ollama returned HTTP status {status}")


def _safe_ollama_error_detail(data: bytes) -> str | None:
    try:
        payload = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, str) or not error.strip():
        return None
    return " ".join(error.split())[:2_000]


def _ollama_native_schema(value: Any) -> Any:
    """Avoid grammar-size limits while retaining strict local validation."""

    if isinstance(value, dict):
        return {
            key: _ollama_native_schema(item)
            for key, item in value.items()
            if key not in {"maxItems", "maxLength"}
        }
    if isinstance(value, list):
        return [_ollama_native_schema(item) for item in value]
    return value


def _ollama_discovery_action_schema(
    schema: Any, tool_schemas: dict[str, object]
) -> Any:
    """Bind native Ollama grammar to each closed discovery tool contract."""

    if not isinstance(schema, dict):
        return schema
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    action = definitions.get("DiscoveryAction")
    if not isinstance(action, dict):
        return schema
    common = action.get("properties")
    if not isinstance(common, dict):
        return schema
    variants: list[dict[str, object]] = []
    for tool_name, parameters in sorted(tool_schemas.items()):
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            continue
        arguments = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        kind = "finalize" if tool_name == "finalize_context" else "call_tool"
        variant_properties = {
            "schema_version": common.get(
                "schema_version", {"type": "integer", "const": 1}
            ),
            "action_id": common.get("action_id", {"type": "string"}),
            "kind": {"type": "string", "const": kind},
            "arguments": arguments,
        }
        variant_required = ["action_id", "kind", "arguments"]
        if kind == "call_tool":
            variant_properties["tool_name"] = {
                "type": "string",
                "const": tool_name,
            }
            variant_required.append("tool_name")
        variants.append(
            {
                "type": "object",
                "properties": variant_properties,
                "required": variant_required,
                "additionalProperties": False,
            }
        )
    if not variants:
        return schema
    result = dict(schema)
    updated_definitions = dict(definitions)
    updated_definitions["DiscoveryAction"] = {"oneOf": variants}
    result["$defs"] = updated_definitions
    return result


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
