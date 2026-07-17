# ContextForge

Create deterministic, portable, reviewable context packages from local
repositories.

ContextForge is an early-stage local repository-intelligence and context
packaging tool. It builds deterministic CodeMaps, maintains an immutable local
index, supports bounded indexed/fresh/hybrid context discovery, verifies final
source selections, and produces reviewable handoffs and compiled prompts.

## Status

The unreleased v0.4 milestone implements repository intelligence, model-provider
adapters, architecture/feature maps, automatic context discovery, Git-aware
handoffs, prompt compilation, thin CLI commands, and a local read-only stdio MCP
foundation. Structural indexing works without a model. Model-assisted behavior
uses a configured local Ollama provider by default; the deterministic fake is
for offline tests and demonstrations.

## Non-goals

The current implementation deliberately excludes:

- embeddings or vector databases;
- autonomous source edits or patch application;
- shell or arbitrary process tools;
- coding-agent or multi-agent orchestration;
- Git mutation and worktree management;
- full multi-root workspaces;
- IDE extensions;
- graphical workspace UI;
- remote MCP transport or MCP sampling.

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

Automatic discovery is opt-in, so the existing manual command remains
unchanged when `--discovery` is absent:

```bash
contextforge context suggest . --task "Fix index invalidation" --discovery hybrid
contextforge context suggest . --task "Audit parsing" --discovery fresh --format json
contextforge context create . --task "Fix index invalidation" --discovery hybrid --format json --output handoff.json
contextforge context create . --task "Review staged behavior" --discovery indexed --git-diff staged --prompt-output prompt.md
contextforge context review handoff.json
```

`context suggest` writes a review only. It never creates or modifies source
files. Table output shows selected paths/ranges, reasons, confidence, warnings,
estimated bytes, and mode. JSON stdout is a directly parseable
`FinalContextSelection`. Exact `--include` paths are pinned and exact
`--exclude` paths have precedence.

Automatic `context create` emits a portable `TaskHandoff` as JSON or the
compiled prompt as Markdown. `--prompt-output` can publish the prompt as a
separate atomic artifact. `context review` validates and displays a JSON
handoff without the original repository.

### Repository index

```bash
contextforge index build .
contextforge index update .
contextforge index status .
contextforge index status . --format json
contextforge index clean .
contextforge index clean . --force
```

`index build` and `index update` support `--provider`, `--model`, `--base-url`, `--config`,
`--concurrency`, `--fail-on-error`, `--force-reanalyze`, `--max-files`, and
`--local-only`. Use `--provider none` for deterministic structural-only
indexing. Updates reuse unchanged valid facts and interpretations, while new,
changed, deleted, analyzer-stale, prompt-stale, and model-stale records are
processed explicitly. A failed strict build restores the prior active pointer.

`index status` is read-only and reports schema, repository and generation
identity, indexed/stale/failed/deleted files, provider/model and prompt
provenance, global-map availability, and lock state. It never prints credential
values. `index clean` removes only generated index truth; it preserves
`.contextforge/config.toml`, saved contexts, and run data.

### Provider configuration

Project configuration is read from `.contextforge/config.toml` or an explicit
`--config PATH`. Command-line provider/model/concurrency choices override the
file for one operation. The default is local Ollama:

```toml
config_version = 1

[models]
provider = "ollama"
endpoint = "http://127.0.0.1:11434/api/chat"
model = "qwen2.5-coder"
timeout_seconds = 120.0
max_response_bytes = 1000000
concurrency_limit = 2
retry_limit = 2
local_only = true
external_data_policy = "deny"
store_raw_prompts = false
store_raw_responses = false
```

Configuration is closed and secret-free. An optional `credential_env` stores
only an environment-variable name; its value is resolved at request time and
is never persisted in the index or handoff.

LM Studio is available through the `lmstudio` alias for the canonical
`openai-compatible` provider. Select the model by copying its exact ID from
`GET /v1/models`; ContextForge has no default LM Studio model:

```bash
contextforge index build . --provider lmstudio --model <MODEL_ID>
contextforge index update . --provider openai-compatible --model <MODEL_ID>
```

```toml
[models]
provider = "openai-compatible"
model = "<MODEL_ID>"
base_url = "http://localhost:1234/v1"
concurrency_limit = 2
# credential_env = "LM_STUDIO_API_KEY"
```

The adapter uses non-streaming `POST /v1/chat/completions` with strict JSON
Schema output and checks the exact configured ID through `GET /v1/models`.
CLI `--model`, `--base-url`, and `--concurrency` values override this section.

### Read-only MCP

Configure an MCP client to launch one stdio server pinned to one repository:

```json
{
  "mcpServers": {
    "contextforge": {
      "command": "contextforge",
      "args": ["mcp", "serve", "/absolute/path/to/repository", "--provider", "none"]
    }
  }
}
```

The server exposes repository overview/tree/search/summary/relationship tools,
verified file/range reads, bounded read-only Git diff, context suggestion,
in-memory package building, and portable package inspection. It has no source
write, shell/process, Git mutation, index mutation, sampling, prompt, remote
transport, or agent-orchestration capability. Stdio contains protocol JSON
only; operational diagnostics go to stderr.

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

Exit code 0 means success, 1 means an operational/provider/read/write/protocol
failure, 2 means invalid usage/configuration/mode/schema/selector/ref/budget,
3 remains reserved for `scan --fail-on-error`, and 130 means cancellation.
Partial non-strict semantic coverage returns 0 with an explicit `partial`
summary; `--fail-on-error` turns it into an operational failure.

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
