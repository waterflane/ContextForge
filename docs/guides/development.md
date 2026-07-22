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

The contributor virtual environment above provides the CLI while it is
activated. To keep an editable development command available independently of
that environment and the current working directory, use an absolute path with
`pipx`:

```bash
pipx install --editable /absolute/path/to/ContextForge
```

Entry-point launchers are generated from `[project.scripts]` at installation
time. Merely editing `pyproject.toml` does not create, rename, or remove an
installed command. Reinstall after every entry-point change:

```bash
python -m pip install -e ".[dev]"
# For the standalone pipx installation:
pipx install --force --editable /absolute/path/to/ContextForge
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
contextforge --version
python -m contextforge --version
ctxf --version
contextforge doctor
contextforge index build . --provider fake
contextforge index status . --format json
contextforge context suggest . --task "Review this repository" --provider fake
contextforge --log-level debug context suggest . --task "Review this repository" --provider fake
contextforge diagnostics config . --format json
ruff check .
ruff format --check .
mypy
pytest
git diff --check
```

For a release candidate, also run the full branch-coverage suite, exercise both
installed console entry points and module execution, and verify the wheel and
source distribution:

```bash
pytest --cov=contextforge --cov-branch --cov-report=term-missing
contextforge version
contextforge --version
ctxf version
ctxf --version
python -m contextforge --version
python -m build
```

Release smoke checks use temporary repositories for non-Git operation, nested
`.gitignore` behavior, table/JSON progress separation, and cancellation. They
must not write generated index/runs/staging data into the source checkout.

Both editable and regular package installations create `contextforge` and its
short alias, `ctxf`. They invoke the same CLI application; `ctxf` does not have
a separate command tree or behavior.

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
Logging validation additionally covers stderr/stdout separation, one-object-
per-line JSON, rotation retention, Rich live ownership, redirected non-ANSI
output, secret redaction, local budget rejection without provider dispatch,
and context-window values of 98,304 through resolution and budgeting.

Tests must not assert on human log prose when a stable `event`, `error.code`, or
structured `data` field exists. Use the in-process `recent_records()` API or
JSON Lines. New application code emits diagnostic facts through
`contextforge.logging.emit`; it must not add another progress abstraction or
depend on Rich/Typer.

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
