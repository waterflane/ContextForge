"""Strict project configuration and provider construction for entry points."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contextforge.models import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    OPENAI_COMPATIBLE_PROVIDER_ID,
    FakeModelProvider,
    ModelProvider,
    ModelRequest,
    OllamaModelProvider,
    OpenAICompatibleModelProvider,
    ProviderConfiguration,
    ProviderConfigurationError,
)

DEFAULT_MODEL_ID = "qwen2.5-coder"
MAX_CONFIG_BYTES = 256 * 1024


class ProjectConfigError(ValueError):
    """Raised when project model configuration is missing or invalid."""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ProjectModelSettings(_ConfigModel):
    """Secret-free provider settings accepted from ``config.toml``."""

    provider: str = "ollama"
    endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    base_url: str | None = None
    model_id: str = Field(default=DEFAULT_MODEL_ID, alias="model")
    timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS
    context_window: int = Field(
        default=DEFAULT_CONTEXT_WINDOW_TOKENS,
        ge=1_024,
        le=2_000_000,
        strict=True,
    )
    context_safety_margin: int = Field(
        default=DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
        ge=64,
        le=16_384,
        strict=True,
    )
    max_response_bytes: int = 1_000_000
    concurrency_limit: int = 2
    retry_limit: int = 2
    semantic_max_output_tokens: int = Field(default=512, ge=96, le=32_768, strict=True)
    local_only: bool = True
    external_data_policy: Literal["deny", "allow_selected", "allow_repository"] = "deny"
    store_raw_prompts: bool = False
    store_raw_responses: bool = False
    credential_env: str | None = None


class RetentionSettings(_ConfigModel):
    """Documented retention preferences; enforcement remains a future policy."""

    runs: int = Field(default=10, ge=0, le=10_000, strict=True)
    index_generations: int = Field(default=2, ge=1, le=1_000, strict=True)


class ProjectConfiguration(_ConfigModel):
    """Closed version-one project configuration."""

    config_version: Literal[1] = 1
    models: ProjectModelSettings = Field(default_factory=ProjectModelSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)


def load_project_configuration(
    repository_root: str | Path,
    *,
    config_path: Path | None = None,
    require_file: bool = False,
    environment: Mapping[str, str] | None = None,
) -> ProjectConfiguration:
    """Load bounded TOML without creating or rewriting project configuration."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    requested = (
        config_path.expanduser()
        if config_path is not None
        else root / ".contextforge" / "config.toml"
    )
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    payload = _read_toml(requested, required=require_file or config_path is not None)
    if config_path is None:
        local_payload = _read_toml(
            root / ".contextforge" / "config.local.toml", required=False
        )
        payload = _merge_config(payload, local_payload)
    payload = _apply_model_environment(
        payload, os.environ if environment is None else environment
    )
    try:
        return ProjectConfiguration.model_validate(payload)
    except ValidationError as exc:
        raise ProjectConfigError("project configuration is invalid") from exc


def resolve_provider_configuration(
    project: ProjectConfiguration,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    concurrency: int | None = None,
    timeout_seconds: float | None = None,
    context_window: int | None = None,
    connect_timeout_seconds: float | None = None,
    read_timeout_seconds: float | None = None,
    operation_timeout_seconds: float | None = None,
    local_only: bool | None = None,
) -> ProviderConfiguration | None:
    """Apply command overrides and return a secret-free provider configuration."""

    settings = project.models
    requested_provider = provider or settings.provider
    provider_id = (
        OPENAI_COMPATIBLE_PROVIDER_ID
        if requested_provider == "lmstudio"
        else requested_provider
    )
    if provider_id == "none":
        return None
    if (
        provider_id == OPENAI_COMPATIBLE_PROVIDER_ID
        and model is None
        and "model_id" not in settings.model_fields_set
    ):
        raise ProjectConfigError(
            "an exact --model ID from GET /v1/models is required for "
            "the OpenAI-compatible provider"
        )
    model_id = model or settings.model_id
    endpoint = settings.endpoint
    if provider_id == OPENAI_COMPATIBLE_PROVIDER_ID:
        endpoint = base_url or settings.base_url or DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    elif base_url is not None:
        raise ProjectConfigError(
            "--base-url is only supported by the OpenAI-compatible provider"
        )
    values = {
        "provider_id": provider_id,
        "endpoint": endpoint,
        "model_id": model_id,
        "timeout_seconds": (
            settings.timeout_seconds if timeout_seconds is None else timeout_seconds
        ),
        "connect_timeout_seconds": (
            settings.connect_timeout_seconds
            if connect_timeout_seconds is None
            else connect_timeout_seconds
        ),
        "read_timeout_seconds": (
            settings.read_timeout_seconds
            if read_timeout_seconds is None
            else read_timeout_seconds
        ),
        "operation_timeout_seconds": (
            settings.operation_timeout_seconds
            if operation_timeout_seconds is None
            else operation_timeout_seconds
        ),
        "context_window": (
            settings.context_window if context_window is None else context_window
        ),
        "context_safety_margin": settings.context_safety_margin,
        "max_response_bytes": settings.max_response_bytes,
        "concurrency_limit": (
            settings.concurrency_limit if concurrency is None else concurrency
        ),
        "retry_limit": settings.retry_limit,
        "local_only": settings.local_only if local_only is None else local_only,
        "external_data_policy": settings.external_data_policy,
        "credential_env": settings.credential_env,
    }
    if provider_id == "fake":
        values["endpoint"] = "fake://offline"
        values["local_only"] = True
    try:
        return ProviderConfiguration.model_validate(values)
    except ValidationError as exc:
        raise ProjectConfigError("provider configuration is invalid") from exc


def _read_toml(path: Path, *, required: bool) -> dict[str, object]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise ProjectConfigError(
                f"configuration file does not exist: {path}"
            ) from None
        return {}
    except OSError as exc:
        raise ProjectConfigError("unable to read project configuration") from exc
    if len(data) > MAX_CONFIG_BYTES:
        raise ProjectConfigError("project configuration exceeds its byte limit")
    try:
        value = tomllib.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError("project configuration is invalid") from exc
    return dict(value)


def _merge_config(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _merge_config(existing, value)
        else:
            result[key] = value
    return result


def _apply_model_environment(
    payload: Mapping[str, object], environment: Mapping[str, str]
) -> dict[str, object]:
    result = dict(payload)
    raw_models = result.get("models")
    models: dict[str, object] = dict(raw_models) if isinstance(raw_models, dict) else {}
    numeric: tuple[tuple[str, str, type[int] | type[float]], ...] = (
        ("CONTEXTFORGE_MODEL_CONTEXT_WINDOW", "context_window", int),
        ("CONTEXTFORGE_MODEL_CONNECT_TIMEOUT", "connect_timeout_seconds", float),
        ("CONTEXTFORGE_MODEL_READ_TIMEOUT", "read_timeout_seconds", float),
        ("CONTEXTFORGE_MODEL_OPERATION_TIMEOUT", "operation_timeout_seconds", float),
    )
    for environment_name, field_name, converter in numeric:
        raw = environment.get(environment_name)
        if raw is None:
            continue
        try:
            models[field_name] = converter(raw)
        except ValueError as exc:
            raise ProjectConfigError(
                f"environment variable {environment_name} is invalid"
            ) from exc
    result["models"] = models
    return result


def create_model_provider(configuration: ProviderConfiguration) -> ModelProvider:
    """Construct one approved provider adapter from closed configuration."""

    if configuration.provider_id == "ollama":
        return OllamaModelProvider(configuration)
    if configuration.provider_id == OPENAI_COMPATIBLE_PROVIDER_ID:
        return OpenAICompatibleModelProvider(configuration)
    if configuration.provider_id == "fake":
        return FakeModelProvider(configuration, responder=_fixture_response)
    raise ProviderConfigurationError(
        f"unsupported model provider: {configuration.provider_id}"
    )


def _fixture_response(request: ModelRequest, call_index: int) -> str:
    """Return deterministic schema-valid fixture data for offline CLI testing."""

    purpose = request.purpose
    if purpose == "repository-discovery":
        selected = request.trusted_code_map_facts.get("selected", [])
        allowed = request.trusted_code_map_facts.get("all_allowed_paths", [])
        actions: list[dict[str, object]] = []
        if not selected and isinstance(allowed, list) and allowed:
            actions.append(
                {
                    "schema_version": 1,
                    "action_id": f"fixture-add-{call_index}",
                    "kind": "call_tool",
                    "tool_name": "add_to_context",
                    "arguments": {
                        "path": allowed[0],
                        "reason": "Deterministic offline fixture selection.",
                        "confidence": 0.5,
                    },
                }
            )
        actions.append(
            {
                "schema_version": 1,
                "action_id": f"fixture-finalize-{call_index}",
                "kind": "finalize",
                "arguments": {
                    "summary": "Deterministic offline fixture discovery.",
                    "unknowns": ["Fixture output is not a semantic relevance claim."],
                    "completeness_claims": [],
                    "confidence": 0.5,
                },
            }
        )
        return json.dumps({"schema_version": 1, "actions": actions})
    if purpose == "task-refinement":
        return json.dumps({"schema_version": 1})
    if purpose == "file-semantics":
        analyzer_kind = request.metadata.get("analyzer_kind")
        category = request.trusted_code_map_facts.get("file_category")
        if analyzer_kind == "generic-text-semantic":
            if category == "readme":
                return json.dumps(
                    {
                        "schema_version": 1,
                        "project_purpose": "Offline fixture project documentation.",
                    }
                )
            if category == "license":
                marker = request.trusted_code_map_facts.get("known_license_marker")
                return json.dumps(
                    {
                        "schema_version": 1,
                        "license_type": marker or "Unknown",
                    }
                )
            if category == "config":
                return json.dumps(
                    {
                        "schema_version": 1,
                        "summary": "Offline fixture configuration.",
                    }
                )
            return json.dumps(
                {"schema_version": 1, "summary": "Offline fixture text analysis."}
            )
        raw_symbols = request.trusted_code_map_facts.get("symbols", [])
        symbols = []
        if isinstance(raw_symbols, list):
            for item in raw_symbols:
                if isinstance(item, dict) and item.get("kind") in {
                    "class",
                    "function",
                    "async_function",
                    "method",
                }:
                    symbols.append({"symbol_id": item.get("symbol_id")})
        return json.dumps({"schema_version": 1, "file": {}, "symbols": symbols})
    if purpose in {"package-summary", "group-synthesis"}:
        match = re.search(r"Return scope_id '([^']+)' exactly", request.analysis_task)
        scope_id = match.group(1) if match is not None else "fixture-scope"
        return json.dumps(
            {
                "schema_version": 1,
                "scope_id": scope_id,
                "title": "Offline fixture summary",
                "summary": "No model interpretation was requested from a live model.",
                "confidence": {"value": 0.5, "rationale": "Offline fixture."},
            }
        )
    if purpose in {"repository-architecture", "repository-features"}:
        match = re.search(r"Return scope_id '([^']+)' exactly", request.analysis_task)
        scope_id = match.group(1) if match is not None else "fixture-repository"
        return json.dumps(
            {
                "schema_version": 1,
                "scope_id": scope_id,
                "title": "Offline fixture repository map",
                "summary": "Deterministic offline repository overview.",
                "confidence": {"value": 0.5, "rationale": "Offline fixture."},
            }
        )
    raise ProviderConfigurationError(
        f"offline fake provider does not support request purpose: {purpose}"
    )


__all__ = [
    "MAX_CONFIG_BYTES",
    "ProjectConfigError",
    "ProjectConfiguration",
    "ProjectModelSettings",
    "RetentionSettings",
    "create_model_provider",
    "load_project_configuration",
    "resolve_provider_configuration",
]
