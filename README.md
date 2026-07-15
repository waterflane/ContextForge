# ContextForge

Create deterministic, portable, reviewable context packages from local
repositories.

ContextForge is currently an early-stage local context-packaging tool and
architectural foundation. It is intended to remain an independent
context-management layer between a software repository and external AI tools.

## Status

The unreleased v0.3 milestone adds manual context selection, verified source
reading, portable Markdown and JSON packages, and offline JSON inspection to
the repository-scanning foundation. ContextForge does not perform automatic
relevance selection, prompt optimization, model calls, embeddings, or
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

### Context packages

Create a package from a repository. With no include option, all selectable
snapshot files are included; exclusions may be used alone. Exact selectors,
directory selectors, and GitWildMatch patterns remain explicit so their
meaning is portable and deterministic.

```bash
contextforge context create . --include src/contextforge/config.py
contextforge context create . --directory src/contextforge/context --exclude "**/__init__.py"
contextforge context create . --glob "*.py" --include-lines src/contextforge/config.py:1-40
contextforge context create . --task "Review configuration" --format markdown
contextforge context create . --format json --output context.json
contextforge context inspect context.json
```

Creation options:

- `--task TEXT` sets the package task; the default is `Context package`.
- `--include PATH` (also `--file`) includes one exact portable relative path.
- `--directory PATH` recursively includes snapshot files below a directory.
- `--glob PATTERN` includes snapshot files using GitWildMatch syntax.
- `--exclude PATTERN` removes matches after all includes are unioned.
- `--include-lines PATH:START-END` (also `--lines`) includes one-based,
  inclusive source ranges and may be repeated.
- `--include-tree` / `--no-include-tree` controls portable tree metadata.
- `--format markdown|json` selects output; Markdown is the default.
- `--output PATH` publishes the fully rendered package atomically. Existing
  destinations require `--force`, and parent directories must already exist.
- `--max-files INTEGER` limits selected files; `--max-context-bytes INTEGER`
  limits included canonical UTF-8 content bytes. These are byte safeguards,
  not model-specific token budgets.

Without `--output`, package content is written to stdout. JSON stdout contains
no status text and remains directly parseable. Package files contain portable
relative paths, never the scanned repository's absolute path.

`context inspect` accepts JSON packages only. It performs bounded, strict
schema and semantic validation without accessing the original repository, then
prints the task, schema version, counts, statistics, selected paths, and line
ranges. Malformed or unsupported packages are rejected without a traceback.

Context command exit code 0 means success, 1 means an operational, read,
decode, render, output, or package-validation failure, and 2 means invalid
command input, repository root, selector, range, task, or limit. Code 3 remains
reserved for `scan --fail-on-error`.

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
- v0.3: context selection and export (implemented, unreleased);
- v0.4: local model integration;
- later: IDE integration and advanced project memory.

See [ROADMAP.md](ROADMAP.md) for the initial roadmap.

## Contributing

Contributions are welcome once the project direction stabilizes. Please read
[CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
and [SECURITY.md](SECURITY.md) before opening issues or pull requests.
