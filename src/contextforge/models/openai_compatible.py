"""OpenAI-compatible local model adapter, including LM Studio."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import SecretStr

from contextforge.models.providers import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCancelledError,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderRuntime,
    ProviderTimeoutError,
    ProviderTransportResponse,
    ProviderUnavailableError,
    redact_secrets,
)

DEFAULT_OPENAI_COMPATIBLE_BASE_URL = "http://localhost:1234/v1"
OPENAI_COMPATIBLE_PROVIDER_ID = "openai-compatible"
MAX_HTTP_HEADER_BYTES = 64 * 1024
MAX_ERROR_TEXT_CHARACTERS = 2_000


@dataclass(frozen=True, slots=True)
class OpenAICompatibleHTTPResponse:
    """Bounded HTTP status and body returned by an injectable transport."""

    status: int
    body: bytes


OpenAICompatibleTransport = Callable[
    [str, str, bytes | None, Mapping[str, str], int],
    Awaitable[OpenAICompatibleHTTPResponse],
]


class OpenAICompatibleModelProvider:
    """Non-streaming JSON Schema adapter for OpenAI-compatible local servers."""

    def __init__(
        self,
        configuration: ProviderConfiguration,
        *,
        transport: OpenAICompatibleTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if configuration.provider_id != OPENAI_COMPATIBLE_PROVIDER_ID:
            raise ValueError(
                "OpenAI-compatible configuration must use "
                "provider_id='openai-compatible'"
            )
        _validate_base_url(configuration)
        self.configuration = configuration
        self._transport = transport or _request_json
        self._environment = environment
        self._runtime = ProviderRuntime(configuration, environment=environment)
        self._model_verified = False
        self._model_verification_lock = asyncio.Lock()
        self._closed = False

    @property
    def provider_id(self) -> str:
        return OPENAI_COMPATIBLE_PROVIDER_ID

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_responses=True,
            cancellation=True,
            token_usage=True,
            local=True,
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

    async def list_models(
        self, *, cancellation: asyncio.Event | None = None
    ) -> tuple[str, ...]:
        """Return exact model IDs from the server's ``GET /v1/models`` response."""

        if self._closed:
            raise ProviderRequestError("provider is closed")
        credential = self.configuration.load_credential(self._environment)
        secrets = () if credential is None else (credential.get_secret_value(),)
        try:
            return await _await_bounded(
                self._list_models_once(credential),
                cancellation=cancellation,
                timeout=self.configuration.timeout_seconds,
            )
        except ProviderCancelledError:
            raise
        except ProviderTimeoutError:
            raise
        except ProviderRequestError as exc:
            raise _redact_error(exc, secrets) from exc.__cause__
        except ProviderUnavailableError as exc:
            raise _redact_error(exc, secrets) from exc.__cause__

    async def close(self) -> None:
        self._closed = True
        await self._runtime.close()

    async def _complete_once(
        self, request: ModelRequest, credential: SecretStr | None
    ) -> ProviderTransportResponse:
        await self._ensure_model_available(credential)
        messages = [message.model_dump(mode="json") for message in request.messages()]
        payload: dict[str, Any] = {
            "messages": messages,
            "model": self.configuration.model_id,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.operation_id,
                    "schema": request.response_schema,
                    "strict": True,
                },
            },
            "stream": False,
            "temperature": request.temperature,
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        response = await self._request(
            "POST",
            _endpoint(self.configuration.endpoint, "chat/completions"),
            _json_bytes(payload),
            credential,
        )
        _raise_for_status(
            response,
            operation="chat completion",
            model_id=self.configuration.model_id,
        )
        return _parse_chat_completion(response.body)

    async def _ensure_model_available(self, credential: SecretStr | None) -> None:
        async with self._model_verification_lock:
            if self._model_verified:
                return
            models = await self._list_models_once(credential)
            if self.configuration.model_id not in models:
                raise ProviderRequestError(
                    f"model ID {self.configuration.model_id!r} was not found by "
                    "GET /v1/models; select an exact returned ID"
                )
            self._model_verified = True

    async def _list_models_once(self, credential: SecretStr | None) -> tuple[str, ...]:
        response = await self._request(
            "GET",
            _endpoint(self.configuration.endpoint, "models"),
            None,
            credential,
        )
        _raise_for_status(
            response,
            operation="model diagnostics",
            model_id=self.configuration.model_id,
        )
        return _parse_model_list(response.body)

    async def _request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        credential: SecretStr | None,
    ) -> OpenAICompatibleHTTPResponse:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential.get_secret_value()
        limit = self.configuration.max_response_bytes + MAX_HTTP_HEADER_BYTES
        try:
            return await self._transport(method, url, body, headers, limit)
        except (ProviderRequestError, ProviderUnavailableError):
            raise
        except Exception as exc:
            raise ProviderUnavailableError(
                "LM Studio/OpenAI-compatible server is unavailable"
            ) from exc


def _validate_base_url(configuration: ProviderConfiguration) -> None:
    parsed = urlsplit(configuration.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError(
            "OpenAI-compatible base URL must be an HTTP URL"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderConfigurationError(
            "OpenAI-compatible base URL must not contain credentials, a query, "
            "or a fragment"
        )
    if configuration.local_only and parsed.hostname.lower() not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ProviderConfigurationError(
            "local-only policy requires a loopback OpenAI-compatible base URL"
        )


def _endpoint(base_url: str, resource: str) -> str:
    return base_url.rstrip("/") + "/" + resource


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _parse_chat_completion(data: bytes) -> ProviderTransportResponse:
    payload = _json_object(data, "chat completion")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderUnavailableError(
            "OpenAI-compatible response did not contain a completion choice"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderUnavailableError("OpenAI-compatible response is malformed")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ProviderUnavailableError(
            "OpenAI-compatible response did not contain model content"
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderUnavailableError(
            "OpenAI-compatible response contained an invalid finish reason"
        )
    usage_payload = payload.get("usage")
    usage: ModelUsage | None = None
    if usage_payload is not None:
        if not isinstance(usage_payload, dict):
            raise ProviderUnavailableError(
                "OpenAI-compatible response contained invalid token usage"
            )
        usage = ModelUsage(
            input_tokens=_optional_non_negative_int(
                usage_payload.get("prompt_tokens"), "prompt token usage"
            ),
            output_tokens=_optional_non_negative_int(
                usage_payload.get("completion_tokens"), "completion token usage"
            ),
        )
    return ProviderTransportResponse(
        text=cast(str, message["content"]),
        finish_reason=finish_reason,
        usage=usage,
    )


def _parse_model_list(data: bytes) -> tuple[str, ...]:
    payload = _json_object(data, "model list")
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise ProviderUnavailableError(
            "OpenAI-compatible GET /v1/models response is malformed"
        )
    model_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ProviderUnavailableError(
                "OpenAI-compatible GET /v1/models response is malformed"
            )
        model_id = cast(str, entry["id"])
        if (
            not model_id
            or len(model_id) > 128
            or any(ord(char) < 32 for char in model_id)
        ):
            raise ProviderUnavailableError(
                "OpenAI-compatible GET /v1/models returned an invalid model ID"
            )
        model_ids.append(model_id)
    if len(model_ids) != len(set(model_ids)):
        raise ProviderUnavailableError(
            "OpenAI-compatible GET /v1/models returned duplicate model IDs"
        )
    return tuple(model_ids)


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderUnavailableError(
            f"OpenAI-compatible {label} response is malformed JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailableError(
            f"OpenAI-compatible {label} response is malformed"
        )
    return cast(dict[str, Any], payload)


def _optional_non_negative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ProviderUnavailableError(
            f"OpenAI-compatible response contained invalid {label}"
        )
    return value


def _raise_for_status(
    response: OpenAICompatibleHTTPResponse,
    *,
    operation: str,
    model_id: str,
) -> None:
    status = response.status
    if 200 <= status < 300:
        return
    detail = _safe_error_detail(response.body)
    suffix = "" if detail is None else f": {detail}"
    if status in {401, 403}:
        raise ProviderRequestError(
            f"OpenAI-compatible authentication failed with HTTP {status}{suffix}"
        )
    if status == 404 and operation == "chat completion":
        raise ProviderRequestError(
            f"model ID {model_id!r} was not found (HTTP 404){suffix}"
        )
    if status in {400, 422} and operation == "chat completion":
        raise ProviderRequestError(
            f"OpenAI-compatible server rejected structured output "
            f"(HTTP {status}){suffix}"
        )
    if status in {408, 429} or 500 <= status < 600:
        raise ProviderUnavailableError(
            f"OpenAI-compatible server failed {operation} with HTTP {status}{suffix}"
        )
    raise ProviderRequestError(
        f"OpenAI-compatible server rejected {operation} with HTTP {status}{suffix}"
    )


def _safe_error_detail(data: bytes) -> str | None:
    try:
        payload = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    candidates: list[object] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("message"), error.get("detail")))
        else:
            candidates.append(error)
        candidates.extend((payload.get("message"), payload.get("detail")))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            cleaned = " ".join(candidate.split())
            return cleaned[:MAX_ERROR_TEXT_CHARACTERS]
    return None


def _redact_error[ErrorType: (ProviderRequestError, ProviderUnavailableError)](
    error: ErrorType, secrets: Sequence[str]
) -> ErrorType:
    message = redact_secrets(str(error), secrets)
    if message == str(error):
        return error
    return type(error)(message)


async def _await_bounded[ResultType](
    awaitable: Awaitable[ResultType],
    *,
    cancellation: asyncio.Event | None,
    timeout: float,
) -> ResultType:
    if cancellation is not None and cancellation.is_set():
        if hasattr(awaitable, "close"):
            cast(Any, awaitable).close()
        raise ProviderCancelledError("provider request was cancelled")
    task = asyncio.ensure_future(awaitable)
    cancellation_task: asyncio.Task[bool] | None = None
    if cancellation is not None:
        cancellation_task = asyncio.create_task(cancellation.wait())
    try:
        waiting: set[asyncio.Future[Any]] = {task}
        if cancellation_task is not None:
            waiting.add(cancellation_task)
        done, _ = await asyncio.wait(
            waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            try:
                return task.result()
            except asyncio.CancelledError as exc:
                raise ProviderCancelledError("provider request was cancelled") from exc
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if cancellation_task is not None and cancellation_task in done:
            raise ProviderCancelledError("provider request was cancelled")
        raise ProviderTimeoutError("provider request timed out")
    except asyncio.CancelledError as exc:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise ProviderCancelledError("provider request was cancelled") from exc
    finally:
        if cancellation_task is not None:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)


async def _request_json(
    method: str,
    url: str,
    body: bytes | None,
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> OpenAICompatibleHTTPResponse:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    secure = parsed.scheme == "https"
    port = parsed.port or (443 if secure else 80)
    ssl_context = ssl.create_default_context() if secure else None
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
            "Connection": "close",
            **headers,
        }
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        head = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
            + "\r\n"
        ).encode("ascii")
        writer.write(head + (body or b""))
        await writer.drain()
        raw_headers = await reader.readuntil(b"\r\n\r\n")
        if len(raw_headers) > MAX_HTTP_HEADER_BYTES:
            raise ProviderUnavailableError(
                "OpenAI-compatible response headers are too large"
            )
        status, response_headers = _parse_http_headers(raw_headers)
        if response_headers.get("transfer-encoding", "").lower() == "chunked":
            response_body = await _read_chunked(reader, max_response_bytes)
        else:
            response_body = await _read_body(
                reader, response_headers, max_response_bytes
            )
        return OpenAICompatibleHTTPResponse(status=status, body=response_body)
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
        raise ProviderUnavailableError(
            "OpenAI-compatible server returned invalid HTTP headers"
        ) from exc
    return status, headers


async def _read_body(
    reader: asyncio.StreamReader,
    headers: Mapping[str, str],
    limit: int,
) -> bytes:
    length_text = headers.get("content-length")
    if length_text is not None:
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ProviderUnavailableError(
                "OpenAI-compatible server returned an invalid content length"
            ) from exc
        if length < 0 or length > limit:
            raise ProviderUnavailableError(
                "OpenAI-compatible server response is too large"
            )
        return await reader.readexactly(length)
    data = await reader.read(limit + 1)
    if len(data) > limit:
        raise ProviderUnavailableError("OpenAI-compatible server response is too large")
    return data


async def _read_chunked(reader: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while True:
        line = await reader.readline()
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise ProviderUnavailableError(
                "OpenAI-compatible server returned invalid chunked data"
            ) from exc
        if size == 0:
            await reader.readuntil(b"\r\n")
            return bytes(result)
        if size < 0 or len(result) + size > limit:
            raise ProviderUnavailableError(
                "OpenAI-compatible server response is too large"
            )
        result.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise ProviderUnavailableError(
                "OpenAI-compatible server returned invalid chunked data"
            )


__all__ = [
    "DEFAULT_OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_PROVIDER_ID",
    "OpenAICompatibleHTTPResponse",
    "OpenAICompatibleModelProvider",
    "OpenAICompatibleTransport",
]
