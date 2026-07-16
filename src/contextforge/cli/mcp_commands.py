"""Thin Typer entry point for the local read-only stdio MCP server."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Never

import typer

from contextforge.mcp import ReadOnlyMCPFoundation, serve_stdio
from contextforge.models import ModelProvider, ModelProviderError
from contextforge.project_config import (
    ProjectConfigError,
    create_model_provider,
    load_project_configuration,
    resolve_provider_configuration,
)
from contextforge.repositories.ignore import IgnoreRulesError

mcp_app = typer.Typer(
    help="Serve bounded read-only repository intelligence over MCP.",
    no_args_is_help=True,
)


@mcp_app.command("serve")
def serve_mcp(
    path: Annotated[
        Path,
        typer.Argument(help="Repository root pinned for this stdio session."),
    ] = Path("."),
    provider_name: Annotated[
        str | None,
        typer.Option(
            "--provider", help="Provider ID for suggest_context; 'none' disables it."
        ),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model ID override.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Explicit project configuration TOML."),
    ] = None,
) -> None:
    """Run the local newline-delimited JSON-RPC stdio MCP server."""

    provider: ModelProvider | None = None
    try:
        project = load_project_configuration(path, config_path=config)
        provider_configuration = resolve_provider_configuration(
            project, provider=provider_name, model=model
        )
        if provider_configuration is not None:
            provider = create_model_provider(provider_configuration)
        foundation = ReadOnlyMCPFoundation(path, provider=provider)
        serve_stdio(foundation)
    except (FileNotFoundError, NotADirectoryError, ProjectConfigError) as exc:
        _exit_with_error(str(exc), code=2)
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except (IgnoreRulesError, ModelProviderError, OSError, ValueError) as exc:
        _exit_with_error(str(exc), code=1)
    finally:
        if provider is not None:
            with suppress(ModelProviderError):
                asyncio.run(provider.close())


def _exit_with_error(message: str, *, code: int) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


__all__ = ["mcp_app"]
