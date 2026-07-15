# ContextForge

Create deterministic, portable, reviewable context packages from local
repositories.

ContextForge is an early-stage local context-packaging tool. It scans a local
repository, applies explicit selectors, verifies selected source files, and
renders deterministic Markdown or JSON packages for review or use by other
tools.

## Status

The unreleased v0.3 milestone adds manual context selection, verified source
reading, portable Markdown and JSON packages, and offline JSON inspection to
the repository-scanning foundation. ContextForge does not perform automatic
relevance selection, prompt optimization, model calls, embeddings, or
repository indexing.

## Non-goals

The current implementation deliberately excludes:

- repository indexing or retrieval;
- embeddings or vector databases;
- model-provider SDKs;
- knowledge graphs;
- prompt generation logic;
- IDE extensions;
- background services or persistent storage.

## Architecture

ContextForge is a straightforward modular monolith:

- core domain and application logic;
- repository and language analysis adapters;
- storage adapters;
- model-provider adapters;
- CLI interface;
- local HTTP API;
- placeholder boundaries for future integrations.

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
contextforge context create . --include pyproject.toml
contextforge context create . --directory src/contextforge/context --exclude "**/__init__.py"
contextforge context create . --glob "src/contextforge/context/**/*.py"
contextforge context create . --include pyproject.toml --include-lines pyproject.toml:1-20
contextforge context create . --task "Review configuration" --format markdown
contextforge context create . --format json --output context.json
contextforge context inspect context.json
```

PowerShell accepts the normal separated option/value form. ContextForge
disables Click's Windows glob expansion so selector patterns reach the
GitWildMatch selector unchanged:

```powershell
contextforge context create . `
  --task 'Fix context serialization' `
  --directory 'src/contextforge/context' `
  --include 'pyproject.toml' `
  --exclude '**/__init__.py' `
  --format markdown `
  --output 'context.md' `
  --force

contextforge context create . `
  --task 'Review context modules' `
  --glob 'src/contextforge/context/**/*.py' `
  --exclude '**/__init__.py' `
  --format json
```

Creation options:

All selector options are repeatable. Include selectors are unioned before all
exclusions are applied.

- `--task TEXT` sets the package task; the default is `Context package`.
- `--include PATH` (also `--file`) includes one exact portable relative file;
  it does not infer directories or patterns.
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
no status text, is emitted as UTF-8, and remains directly parseable. Package
files contain portable relative paths, never the scanned repository's absolute
path.

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

See [ROADMAP.md](ROADMAP.md) for milestone status and future work.

## Contributing

Contributions are welcome once the project direction stabilizes. Please read
[CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
and [SECURITY.md](SECURITY.md) before opening issues or pull requests.
