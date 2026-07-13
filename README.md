# ContextForge

Create a deterministic local repository inventory as a foundation for future
context-management workflows.

ContextForge is currently an early-stage repository inventory tool and
architectural foundation. It is intended to become an independent
context-management layer between a software repository and external AI tools.

## Status

The completed, unreleased v0.2 milestone adds deterministic local repository
scanning to the v0.1.0 architectural foundation. ContextForge does not yet
perform semantic context retrieval, prompt generation, model calls, or
repository indexing.

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

## CLI

```bash
contextforge version
contextforge doctor
```

### Repository scanning

Scan a repository from the terminal. `PATH` defaults to the current working
directory, table output is the default, and the default maximum included file
size is 1,000,000 bytes. Table output contains the included file inventory and
summary; JSON uses a stable versioned envelope.

```bash
contextforge scan .
contextforge scan . --format json
contextforge scan . --output scan.json --format json
contextforge scan . --show-excluded
contextforge scan . --fail-on-error
```

Available scan options:

- `--format table|json` selects human-readable or stable structured output.
- `--output PATH` writes the selected output to a new file. Parent directories
  must already exist, and existing destinations are never overwritten.
- `--max-file-size INTEGER` sets the maximum included file size in bytes and
  must be greater than zero.
- `--show-excluded` lists excluded paths and reasons in table output. JSON
  output includes detailed `ignored_files` and `skipped_files` only when this
  option is enabled. Without it, those lists are empty except that unreadable
  entries remain in `skipped_files` for diagnostics.
- `--fail-on-error` exits with code 3 if the completed scan contains unreadable
  entries. Ignored, protected, binary, oversized, symlink, and unsupported
  entries do not trigger this exit code.

Exit code 0 means the scan completed successfully, 1 reports a scan or output
operation failure, 2 reports invalid command input, and 3 is reserved for a
completed `--fail-on-error` scan with unreadable entries. Output files are
rendered before writing and published atomically after the scan completes.

Ignored directories are recorded once and pruned without enumerating their
descendants. Ordinary rules use Git-style last-match semantics: a negative rule
can reopen traversal only by effectively re-including the directory itself.
A descendant negative rule cannot cross a parent directory that remains
ignored. Common caches, including `.uv-cache/`, are excluded by default, while
`uv.lock` remains scannable. Protected `.git`, `.hg`, and `.svn` roots are
always pruned and cannot be re-included.

The scanner reads ignore rules from the repository-root `.gitignore` and
`.contextforgeignore`; nested ignore files are currently ordinary scanned files,
not additional rule sources. Text detection accepts UTF-8 (including ASCII) and
treats an initial sample containing invalid UTF-8, NUL bytes, or many control
bytes as binary-like. Symbolic links and Windows directory junctions are
reported but never followed.

`discovered_count` counts file-like entries actually reached during traversal.
`ignored_count` counts reached files or directory roots excluded by ordinary
rules, `protected_count` counts reached protected VCS roots or entries, and
`skipped_count` counts explicit exclusion/failure records. Descendants of
pruned directories are not estimated or counted.

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
