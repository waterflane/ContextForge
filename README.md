# ContextForge

Build, manage, and optimize context for AI models and agents.

ContextForge is a tool for managing project context and creating optimized
prompts and context packages for AI models and agents. It is intended to become
an independent context-management layer between a software repository and
external AI tools.

## Status

ContextForge v0.1.0 is an architectural foundation. It does not yet perform
useful repository analysis, context retrieval, prompt generation, model calls,
or indexing.

## Long-term vision

ContextForge aims to help developers:

- analyze software repositories;
- identify information relevant to a development task;
- organize project knowledge;
- build compact, reviewable context packages;
- generate prompts adapted to different AI models and agents.

ContextForge is not an AI coding assistant. It should prepare and manage
context for other tools.

## Non-goals

The initial release deliberately excludes:

- repository indexing or retrieval;
- embeddings or vector databases;
- model-provider SDKs;
- knowledge graphs;
- prompt generation logic;
- IDE extensions;
- background services or persistent storage.

## Planned architecture

ContextForge is planned as a straightforward modular monolith:

- core domain and application logic;
- repository and language analysis adapters;
- storage adapters;
- model-provider adapters;
- CLI interface;
- local HTTP API;
- future IDE extensions.

The core package must remain independent from FastAPI, Typer, model providers,
storage implementations, and editor integrations.

## Development setup

Requires Python 3.12 or newer.

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

## Placeholder commands

```bash
contextforge version
contextforge doctor
```

The local API can be run during development with:

```bash
uvicorn contextforge.api.app:create_app --factory
```

Available endpoints:

- `GET /health`
- `GET /version`

## Validation

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Roadmap summary

- v0.1: project foundation;
- v0.2: repository scanning and file inventory;
- v0.3: context selection and export;
- v0.4: local model integration;
- later: IDE integration and advanced project memory.

See [ROADMAP.md](ROADMAP.md) for the initial roadmap.

## Contributing

Contributions are welcome once the project direction stabilizes. Please read
[CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
and [SECURITY.md](SECURITY.md) before opening issues or pull requests.
