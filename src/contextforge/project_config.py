"""Strict project configuration and provider construction for entry points."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from contextforge.logging import LogFormat, LoggingConfiguration, LogLevel
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


class LoggingSettings(_ConfigModel):
    """Secret-free project logging policy."""

    level: Literal["quiet", "error", "warning", "info", "debug", "trace"] = "warning"
    format: Literal["auto", "pretty", "json"] = "auto"
    file_enabled: bool = False
    file: str = ".contextforge/logs/contextforge.log"
    rotation_bytes: int = Field(default=10_000_000, ge=1_024, le=2_000_000_000)
    retained_files: int = Field(default=5, ge=0, le=100)
    components: dict[
        str, Literal["quiet", "error", "warning", "info", "debug", "trace"]
    ] = Field(default_factory=dict)


class ProjectConfiguration(_ConfigModel):
    """Closed version-one project configuration."""

    config_version: Literal[1] = 1
    models: ProjectModelSettings = Field(default_factory=ProjectModelSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    _value_sources: dict[str, str] = PrivateAttr(default_factory=dict)
    _value_candidates: dict[str, dict[str, object | None]] = PrivateAttr(
        default_factory=dict
    )


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
    shared_payload = _read_toml(
        requested, required=require_file or config_path is not None
    )
    payload = shared_payload
    local_payload: dict[str, object] = {}
    if config_path is None:
        local_payload = _read_toml(
            root / ".contextforge" / "config.local.toml", required=False
        )
        payload = _merge_config(payload, local_payload)
    active_environment = os.environ if environment is None else environment
    payload = _apply_model_environment(payload, active_environment)
    payload = _apply_logging_environment(payload, active_environment)
    try:
        project = ProjectConfiguration.model_validate(payload)
    except ValidationError as exc:
        raise ProjectConfigError("project configuration is invalid") from exc
    _record_configuration_sources(
        project,
        shared_payload=shared_payload,
        local_payload=local_payload,
        environment=active_environment,
        shared_name=(requested.name if config_path is not None else "config.toml"),
    )
    return project


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
        "context_window_source": (
            "CLI"
            if context_window is not None
            else project._value_sources.get("models.context_window", "built-in default")
        ),
        "cli_context_window": context_window,
        "environment_context_window": project._value_candidates.get(
            "models.context_window", {}
        ).get("environment"),
        "local_config_context_window": project._value_candidates.get(
            "models.context_window", {}
        ).get("config.local.toml"),
        "shared_config_context_window": project._value_candidates.get(
            "models.context_window", {}
        ).get("config.toml"),
        "default_context_window": DEFAULT_CONTEXT_WINDOW_TOKENS,
    }
    if provider_id == "fake":
        values["endpoint"] = "fake://offline"
        values["local_only"] = True
    try:
        return ProviderConfiguration.model_validate(values)
    except ValidationError as exc:
        raise ProjectConfigError("provider configuration is invalid") from exc


def resolve_logging_configuration(
    project: ProjectConfiguration,
    repository_root: str | Path,
    *,
    level: str | None = None,
    log_format: str | None = None,
    log_file: Path | None = None,
    component_filter: tuple[str, ...] = (),
    no_log_file: bool = False,
    no_color: bool = False,
    verbosity: int = 0,
) -> LoggingConfiguration:
    """Apply CLI logging options after environment/project/default resolution."""

    configured = project.logging
    if level is not None:
        effective_level = LogLevel(level.casefold())
    elif verbosity >= 2:
        effective_level = LogLevel.TRACE
    elif verbosity == 1:
        effective_level = _raise_verbosity(LogLevel(configured.level))
    else:
        effective_level = LogLevel(configured.level)
    effective_format = LogFormat(
        configured.format if log_format is None else log_format.casefold()
    )
    root = Path(repository_root).expanduser().resolve(strict=True)
    file_value = Path(configured.file) if log_file is None else log_file
    file_enabled = configured.file_enabled or log_file is not None
    if no_log_file:
        file_enabled = False
    return LoggingConfiguration(
        level=effective_level,
        format=effective_format,
        file_enabled=file_enabled,
        file=file_value,
        rotation_bytes=configured.rotation_bytes,
        retained_files=configured.retained_files,
        components={
            key: LogLevel(value) for key, value in configured.components.items()
        },
        component_filter=frozenset(component_filter),
        no_color=no_color,
        repository_root=root,
    )


def configuration_resolution(project: ProjectConfiguration) -> dict[str, Any]:
    """Return detached, safe precedence metadata for diagnostics and APIs."""

    return {
        "sources": dict(project._value_sources),
        "candidates": {
            key: dict(value) for key, value in project._value_candidates.items()
        },
        "precedence": [
            "CLI",
            "environment",
            "config.local.toml",
            "config.toml",
            "built-in default",
        ],
    }


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


def _apply_logging_environment(
    payload: Mapping[str, object], environment: Mapping[str, str]
) -> dict[str, object]:
    result = dict(payload)
    raw_logging = result.get("logging")
    values: dict[str, object] = (
        dict(raw_logging) if isinstance(raw_logging, dict) else {}
    )
    names = {
        "CONTEXTFORGE_LOG_LEVEL": "level",
        "CONTEXTFORGE_LOG_FORMAT": "format",
        "CONTEXTFORGE_LOG_FILE": "file",
    }
    for environment_name, field_name in names.items():
        raw = environment.get(environment_name)
        if raw is not None:
            values[field_name] = raw
            if field_name == "file":
                values["file_enabled"] = True
    components = environment.get("CONTEXTFORGE_LOG_COMPONENTS")
    if components is not None:
        # Environment components are a focus allowlist, not per-component levels;
        # the CLI resolver receives this value through the private candidates.
        values.setdefault("components", {})
    result["logging"] = values
    return result


def _record_configuration_sources(
    project: ProjectConfiguration,
    *,
    shared_payload: Mapping[str, object],
    local_payload: Mapping[str, object],
    environment: Mapping[str, str],
    shared_name: str,
) -> None:
    shared_context = _nested_value(shared_payload, "models", "context_window")
    local_context = _nested_value(local_payload, "models", "context_window")
    environment_context: int | None = None
    raw_environment = environment.get("CONTEXTFORGE_MODEL_CONTEXT_WINDOW")
    if raw_environment is not None:
        try:
            environment_context = int(raw_environment)
        except ValueError:
            environment_context = None
    candidates: dict[str, object | None] = {
        "CLI": None,
        "environment": environment_context,
        "config.local.toml": local_context,
        "config.toml": shared_context,
        "provider metadata": None,
        "model metadata": None,
        "built-in default": DEFAULT_CONTEXT_WINDOW_TOKENS,
    }
    if environment_context is not None:
        source = "environment"
    elif local_context is not None:
        source = "config.local.toml"
    elif shared_context is not None:
        source = "config.toml" if shared_name == "config.toml" else shared_name
    else:
        source = "built-in default"
    project._value_sources["models.context_window"] = source
    project._value_candidates["models.context_window"] = candidates

    logging_fields = ("level", "format", "file")
    for field_name in logging_fields:
        environment_name = f"CONTEXTFORGE_LOG_{field_name.upper()}"
        environment_value = environment.get(environment_name)
        local_value = _nested_value(local_payload, "logging", field_name)
        shared_value = _nested_value(shared_payload, "logging", field_name)
        default_value = getattr(LoggingSettings(), field_name)
        if environment_value is not None:
            value_source = "environment"
        elif local_value is not None:
            value_source = "config.local.toml"
        elif shared_value is not None:
            value_source = "config.toml"
        else:
            value_source = "built-in default"
        key = f"logging.{field_name}"
        project._value_sources[key] = value_source
        project._value_candidates[key] = {
            "CLI": None,
            "environment": environment_value,
            "config.local.toml": local_value,
            "config.toml": shared_value,
            "built-in default": default_value,
        }
    raw_components = environment.get("CONTEXTFORGE_LOG_COMPONENTS")
    if raw_components is not None:
        project._value_candidates["logging.component_filter"] = {
            "environment": tuple(
                item.strip() for item in raw_components.split(",") if item.strip()
            )
        }


def _nested_value(
    payload: Mapping[str, object], section: str, field_name: str
) -> object | None:
    value = payload.get(section)
    return value.get(field_name) if isinstance(value, dict) else None


def _raise_verbosity(level: LogLevel) -> LogLevel:
    return {
        LogLevel.QUIET: LogLevel.ERROR,
        LogLevel.ERROR: LogLevel.WARNING,
        LogLevel.WARNING: LogLevel.INFO,
        LogLevel.INFO: LogLevel.DEBUG,
        LogLevel.DEBUG: LogLevel.TRACE,
        LogLevel.TRACE: LogLevel.TRACE,
    }[level]


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
    "LoggingSettings",
    "RetentionSettings",
    "configuration_resolution",
    "create_model_provider",
    "load_project_configuration",
    "resolve_provider_configuration",
    "resolve_logging_configuration",
]
