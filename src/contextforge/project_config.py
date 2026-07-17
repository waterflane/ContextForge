"""Strict project configuration and provider construction for entry points."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contextforge.models import (
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
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
    timeout_seconds: float = 120.0
    max_response_bytes: int = 1_000_000
    concurrency_limit: int = 2
    retry_limit: int = 2
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
    try:
        data = requested.read_bytes()
    except FileNotFoundError:
        if require_file or config_path is not None:
            raise ProjectConfigError(
                f"configuration file does not exist: {requested}"
            ) from None
        return ProjectConfiguration()
    except OSError as exc:
        raise ProjectConfigError("unable to read project configuration") from exc
    if len(data) > MAX_CONFIG_BYTES:
        raise ProjectConfigError("project configuration exceeds its byte limit")
    try:
        decoded = data.decode("utf-8", errors="strict")
        payload = tomllib.loads(decoded)
        return ProjectConfiguration.model_validate(payload)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ProjectConfigError("project configuration is invalid") from exc


def resolve_provider_configuration(
    project: ProjectConfiguration,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    concurrency: int | None = None,
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
        "timeout_seconds": settings.timeout_seconds,
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
    if purpose in {"file-chunk-semantics", "file-synthesis"}:
        return json.dumps({"schema_version": 1, "file": {}})
    if purpose == "symbol-semantics":
        symbol = request.trusted_code_map_facts.get("symbol", {})
        symbol_id = symbol.get("symbol_id") if isinstance(symbol, dict) else None
        return json.dumps({"schema_version": 1, "symbol": {"symbol_id": symbol_id}})
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
    if purpose == "repository-architecture":
        return json.dumps(
            {
                "schema_version": 1,
                "confidence": {"value": 0.5, "rationale": "Offline fixture."},
            }
        )
    if purpose == "repository-features":
        return json.dumps(
            {
                "schema_version": 1,
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
