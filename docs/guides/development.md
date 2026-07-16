# Development Guide

## Requirements

- Python 3.12 or newer.
- A POSIX shell, PowerShell, or another shell capable of running Python tools.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Common commands

```bash
contextforge version
contextforge doctor
contextforge index build . --provider fake
contextforge index status . --format json
contextforge context suggest . --task "Review this repository" --provider fake
ruff check .
ruff format --check .
mypy
pytest
git diff --check
```

The deterministic `fake` provider is intended for normal tests and offline
smoke checks. It returns schema-valid fixture interpretations and a stable
first-file discovery selection; it is not a relevance-quality model. Tests use
`tmp_path` and must not require a live provider, network, or developer-checkout
scan.

## Repository-intelligence validation

The complete gate is:

```bash
ruff format --check .
ruff check .
mypy
pytest
git diff --check
```

Manual offline checks should cover structural/fake `index build`, no-op and
changed-file `index update`, table/JSON status, clean confirmation/config
preservation, all discovery modes, automatic and manual context creation,
portable handoff review, and MCP initialize/list/call/resource exchange.

## Project provider configuration

`.contextforge/config.toml` is user-authored after initialization. CLI
`--provider`, `--model`, `--config`, and `--concurrency` override it for one
operation. Store only an optional `credential_env` variable name, never a
credential value. `--provider none` disables model phases for a structural-only
index. `--local-only` requires the approved provider adapter to remain local.

## MCP development configuration

The server uses newline-delimited JSON-RPC over stdio:

```powershell
contextforge mcp serve C:\path\to\repository --provider none
```

Example client entry:

```json
{
  "mcpServers": {
    "contextforge-dev": {
      "command": "python",
      "args": [
        "-m",
        "contextforge",
        "mcp",
        "serve",
        "C:/path/to/repository",
        "--provider",
        "none"
      ]
    }
  }
}
```

MCP tests call the protocol adapter directly and exercise a stdio loop with
in-memory streams. They assert exact tool schemas, structured errors, path and
byte rejection, resource reads, and the absence of write/sampling capability.

## Local API

```bash
uvicorn contextforge.api.app:create_app --factory
```

Then check:

- `GET /health`
- `GET /version`
