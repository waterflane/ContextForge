# ContextForge

Create deterministic, portable, reviewable context packages from local
repositories.

ContextForge is an early-stage local repository-intelligence and context
packaging tool. It builds deterministic CodeMaps, maintains an immutable local
index, supports bounded indexed/fresh/hybrid context discovery, verifies final
source selections, and produces reviewable handoffs and compiled prompts.

## Status

Version 0.4.1 is a maintenance and usability release for the completed v0.4
repository-intelligence milestone. It adds structured, percentage-based
progress for long-running workflows, correct nested `.gitignore` handling,
consistent version commands, the `ctxf` console alias, and structured
diagnostics. Structural indexing works without a model. Model-assisted behavior
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

For an editable development command that remains available without activating
the repository virtual environment, install it with `pipx` using an absolute
repository path:

```bash
pipx install --editable /absolute/path/to/ContextForge
```

Console commands are generated during installation. After changing
`[project.scripts]` in `pyproject.toml`, reinstall the package so additions or
renames are reflected in the available commands:

```bash
python -m pip install -e ".[dev]"
# Or refresh the standalone editable installation:
pipx install --force --editable /absolute/path/to/ContextForge
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
contextforge --version
python -m contextforge --version
contextforge doctor
```

An installation also provides the short `ctxf` command. It invokes the same
CLI application, so every command and option is interchangeable:

```bash
ctxf --version
ctxf index status
ctxf context suggest --help
ctxf mcp serve --help
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

Long-running index and automatic context commands report weighted progress to
stderr. `--progress auto` (the default) uses one compact Rich live panel when
stderr is interactive and discrete records when stderr is redirected.
`--progress never` suppresses terminal progress; `--progress always` requests
the live display only when the actual stderr console can render it safely.
Captured or redirected stdout does not disable an interactive stderr panel.

During model analysis the live panel shows overall and phase bars,
processed/planned, succeeded/failed/fallback/skipped/reused counters, current,
last successful and last failed files, a safe failure reason, attempt count,
request and total elapsed time, active concurrency, and provider/model. A
semantic file transition is also fully available to programmatic observers:

```text
Indexing repository: 53% — Semantic analysis 9/26 processed · 35%; completed=src/app.py current=src/service.py failures=1 processed=9/26 succeeded=8
```

```bash
contextforge context suggest . --task "Audit parsing" --format json > selection.json
python -m json.tool selection.json
```

Interactive and redirected runs use the same structured phase events.
Redirected stderr contains readable lines and no ANSI control sequences or
spinner ticks. Structured JSON and Markdown remain the only stdout content.
Progress does not change exit codes; Ctrl+C still exits with 130 and restores
the live display.

### Logging and diagnostics

ContextForge 0.4.1 has one structured diagnostic pipeline, separate from
command results and from progress. Logs always use stderr; JSON, Markdown, and
MCP protocol results remain isolated on stdout. Normal use needs no logging
flags. The built-in console level is `warning`.

```bash
contextforge context suggest . --task "Audit parsing" --log-level debug
contextforge context suggest . --task "Audit parsing" --log-format json
contextforge diagnostics last .
contextforge diagnostics config .
contextforge diagnostics provider .
```

Global options are `--log-level quiet|error|warning|info|debug|trace`,
`--log-format auto|pretty|json`, `--log-file PATH`, repeatable
`--log-component COMPONENT`, `--no-log-file`, `--no-color`, `-v`, and `-vv`.
One `-v` raises the configured level once; `-vv` selects trace. An explicit
`--log-level` wins. Both `contextforge` and `ctxf` use the identical policy.

Debug budget records decompose system, user, source, index, schema, output,
protocol-overhead, and safety-margin tokens. They include the effective
ContextForge context window and its source, whether dispatch occurred, and
safe retry/fallback/error state, but never complete prompts, source contents,
raw model responses, authorization data, or credentials. See the
[CLI reference](docs/guides/cli.md),
[configuration guide](docs/guides/configuration.md), and
[diagnostics architecture](docs/architecture/diagnostics.md).

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
`--concurrency`, `--request-timeout`, `--max-output-tokens`, `--fail-on-error`,
`--force-reanalyze`, `--max-files`, and `--local-only`. Use `--provider none` for deterministic structural-only
indexing. Updates reuse unchanged valid facts and interpretations, while new,
changed, deleted, analyzer-stale, prompt-stale, and model-stale records are
processed explicitly. A failed strict build restores the prior active pointer.
For model-enabled builds, scan/planning occupy approximately 0–8%, structural
extraction 8–18%, per-file semantic model work 18–82%, repository-map model work
82–94%, deterministic finalization 94–97%, and validation/publication 97–100%.
Structural-only builds redistribute the model ranges. The semantic phase bar is
`processed_units / planned_units`, where failures and reuse are terminal work,
not successes. Overall weighting gives deterministic metadata/reuse one unit and
model files eight base units plus one unit per 32 KiB (source units capped at
16). `.gitignore`, `.gitattributes`, `.editorconfig`, environment examples,
lock files, `.gitkeep`, and empty files avoid provider calls; environment
examples store variable names only. Meaningful non-Python text still uses
generic model semantics through the separate `generic-text-semantic` identity.
No `.contextforge` file enters structural or semantic routing. The sole successful 100% event
is emitted only after generation validation and atomic active-pointer
publication succeed.

`index status` is read-only and reports schema, repository and generation
identity, indexed/stale/failed/deleted files, provider/model and prompt
provenance, global-map availability, and lock state. It never prints credential
values. `index clean` removes only generated index truth; it preserves
`.contextforge/config.toml`, saved contexts, and run data.

### Provider configuration

Project configuration is read from `.contextforge/config.toml`, with optional
machine-local overrides in `.contextforge/config.local.toml`, or from an
explicit `--config PATH`. CLI and supported environment settings take
precedence. The default is local Ollama:

```toml
config_version = 1

[models]
provider = "ollama"
endpoint = "http://127.0.0.1:11434/api/chat"
model = "qwen2.5-coder"
timeout_seconds = 360.0
connect_timeout_seconds = 10.0
read_timeout_seconds = 300.0
operation_timeout_seconds = 360.0
context_window = 4096
context_safety_margin = 256
max_response_bytes = 1000000
concurrency_limit = 2
retry_limit = 2
semantic_max_output_tokens = 512
local_only = true
external_data_policy = "deny"
store_raw_prompts = false
store_raw_responses = false

[models.structured_response]
max_repair_attempts = 5

[logging]
level = "warning"
format = "auto"
file_enabled = false
file = ".contextforge/logs/contextforge.log"
rotation_bytes = 10000000
retained_files = 5

[logging.components]
# provider = "debug"
# budget = "trace"
# synthesis = "debug"
```

Configuration is closed and secret-free. An optional `credential_env` stores
only an environment-variable name; its value is resolved at request time and
is never persisted in the index or handoff.

Logging precedence is CLI, `CONTEXTFORGE_LOG_*` environment variables,
`.contextforge/config.local.toml`, `.contextforge/config.toml`, then defaults.
Machine-specific log paths normally belong in `config.local.toml`. File logs
are opt-in rotating UTF-8 JSON Lines; the defaults are 10,000,000 bytes and
five retained files.

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
context_window = 4096
# credential_env = "LM_STUDIO_API_KEY"
```

The adapter uses non-streaming `POST /v1/chat/completions` with strict JSON
Schema output and checks the exact configured ID through `GET /v1/models`.
CLI `--model`, `--base-url`, `--concurrency`, `--request-timeout`,
`--context-window`, `--max-output-tokens`, and `--json-repair-attempts` values
override this section. `CONTEXTFORGE_JSON_REPAIR_ATTEMPTS` is the corresponding
environment override; the safe range is 0–10 and the default is five repairs.
Defaults are 10 seconds for connection, 300 seconds for response read, and 360
seconds for the complete operation. Transient failures retain bounded retries;
context overflow and rejected schemas are never resent unchanged.

Remote endpoints are fail-closed unless configuration explicitly sets both
`local_only = false` and `external_data_policy = "allow_repository"`.
`allow_selected` is reserved for a future path-level transmission policy and
does not authorize a remote endpoint in this release. External repository-wide
use can transmit any selectable snapshot content, so review ignore rules and
provider retention before enabling it.

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

The scanner reads `.gitignore` rules throughout the repository, applying each
file relative to its containing directory with inherited Git-style precedence.
The repository-root `.contextforgeignore` remains the highest-precedence
ordinary rule source. Ignore control files remain ordinary scanned files unless
an active rule excludes them. Text detection accepts UTF-8 (including ASCII)
and treats an initial sample containing invalid UTF-8, NUL bytes, or many
control bytes as binary-like. Symbolic links and Windows directory junctions
are reported but never followed.

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
