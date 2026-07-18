"""Read-only CLI access to safe structured operation diagnostics."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Never

import typer

from contextforge.diagnostics import (
    DiagnosticStoreError,
    load_last_summary,
    load_summary,
)
from contextforge.logging import sanitize_url
from contextforge.project_config import (
    ProjectConfigError,
    configuration_resolution,
    load_project_configuration,
    resolve_provider_configuration,
)


class DiagnosticFormat(StrEnum):
    """Supported diagnostic command output representations."""

    table = "table"
    json = "json"


diagnostics_app = typer.Typer(
    help=(
        "Inspect safe configuration, provider, budget, retry, and failure diagnostics."
    ),
    no_args_is_help=True,
)


@diagnostics_app.command("last")
def diagnostics_last(
    path: Annotated[Path, typer.Argument(help="Repository root to inspect.")] = Path(
        "."
    ),
    output_format: Annotated[
        DiagnosticFormat,
        typer.Option("--format", help="Output representation.", case_sensitive=False),
    ] = DiagnosticFormat.table,
    failed: Annotated[
        bool,
        typer.Option("--failed", help="Return the latest failed operation only."),
    ] = False,
) -> None:
    """Show the latest completed, failed, or cancelled operation."""

    try:
        value = load_last_summary(path, failed_only=failed)
    except DiagnosticStoreError as exc:
        _exit(str(exc))
    _write(value, output_format)


@diagnostics_app.command("show")
def diagnostics_show(
    path: Annotated[Path, typer.Argument(help="Repository root to inspect.")],
    operation_id: Annotated[str, typer.Argument(help="Operation ID from diagnostics.")],
    output_format: Annotated[
        DiagnosticFormat,
        typer.Option("--format", help="Output representation.", case_sensitive=False),
    ] = DiagnosticFormat.table,
) -> None:
    """Show one safe operation summary by its public operation ID."""

    try:
        value = load_summary(path, operation_id)
    except DiagnosticStoreError as exc:
        _exit(str(exc))
    _write(value, output_format)


@diagnostics_app.command("config")
def diagnostics_config(
    path: Annotated[Path, typer.Argument(help="Repository root to inspect.")] = Path(
        "."
    ),
    output_format: Annotated[
        DiagnosticFormat,
        typer.Option("--format", help="Output representation.", case_sensitive=False),
    ] = DiagnosticFormat.table,
) -> None:
    """Explain effective configuration precedence without credential values."""

    try:
        project = load_project_configuration(path)
        resolution = configuration_resolution(project)
    except (OSError, ProjectConfigError) as exc:
        _exit(str(exc))
    context = resolution["candidates"]["models.context_window"]
    value = {
        "schema_version": 1,
        "precedence": resolution["precedence"],
        "context_window": {
            "cli_context_window": context.get("CLI"),
            "environment_context_window": context.get("environment"),
            "local_config_context_window": context.get("config.local.toml"),
            "shared_config_context_window": context.get("config.toml"),
            "provider_reported_context_window": context.get("provider metadata"),
            "model_metadata_context_window": context.get("model metadata"),
            "default_context_window": context.get("built-in default"),
            "effective_context_window": project.models.context_window,
            "effective_context_window_source": resolution["sources"][
                "models.context_window"
            ],
            "explicit": resolution["sources"]["models.context_window"]
            != "built-in default",
        },
        "logging": {
            "level": project.logging.level,
            "format": project.logging.format,
            "file_enabled": project.logging.file_enabled,
            "file": project.logging.file,
            "rotation_bytes": project.logging.rotation_bytes,
            "retained_files": project.logging.retained_files,
            "components": project.logging.components,
            "sources": {
                key: source
                for key, source in resolution["sources"].items()
                if key.startswith("logging.")
            },
        },
        "credential_values_exposed": False,
    }
    _write(value, output_format)


@diagnostics_app.command("provider")
def diagnostics_provider(
    path: Annotated[Path, typer.Argument(help="Repository root to inspect.")] = Path(
        "."
    ),
    output_format: Annotated[
        DiagnosticFormat,
        typer.Option("--format", help="Output representation.", case_sensitive=False),
    ] = DiagnosticFormat.table,
) -> None:
    """Show provider identity and policy without making a network request."""

    try:
        project = load_project_configuration(path)
        provider = resolve_provider_configuration(project)
    except (OSError, ProjectConfigError) as exc:
        _exit(str(exc))
    if provider is None:
        value: dict[str, Any] = {
            "schema_version": 1,
            "enabled": False,
            "probe_performed": False,
        }
    else:
        value = {
            "schema_version": 1,
            "enabled": True,
            "provider": provider.provider_id,
            "model": provider.model_id,
            "endpoint": sanitize_url(provider.endpoint),
            "local_only": provider.local_only,
            "effective_context_window": provider.context_window,
            "effective_context_window_source": provider.context_window_source,
            "provider_reported_context_window": (
                provider.provider_reported_context_window
            ),
            "model_metadata_context_window": provider.model_metadata_context_window,
            "connect_timeout_seconds": provider.connect_timeout_seconds,
            "read_timeout_seconds": provider.read_timeout_seconds,
            "operation_timeout_seconds": provider.operation_timeout_seconds,
            "retry_limit": provider.retry_limit,
            "structured_output_expected": True,
            "probe_performed": False,
            "credential_reference_configured": provider.credential_env is not None,
            "credential_value_exposed": False,
        }
    _write(value, output_format)


def _write(value: dict[str, Any], output_format: DiagnosticFormat) -> None:
    if output_format is DiagnosticFormat.json:
        typer.echo(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return
    typer.echo(_render(value), nl=False)


def _render(value: dict[str, Any]) -> str:
    lines = ["ContextForge diagnostics"]
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(f"{key.replace('_', ' ').title()}:")
            for nested_key, nested in item.items():
                lines.append(f"  {nested_key}: {_scalar(nested)}")
        elif isinstance(item, list):
            lines.append(f"{key}: {_scalar(item)}")
        else:
            lines.append(f"{key}: {_scalar(item)}")
    return "\n".join(lines) + "\n"


def _scalar(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return str(value)


def _exit(message: str) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


__all__ = ["DiagnosticFormat", "diagnostics_app"]
