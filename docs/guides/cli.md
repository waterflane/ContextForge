# CLI logging and diagnostics reference

The `contextforge` and `ctxf` entry points share one Typer application and one
logging policy. Command results use stdout. Progress, logs, and unavoidable
fatal diagnostics use stderr. `--version` and `version` remain clean.

## Global logging options

- `--log-level quiet|error|warning|info|debug|trace`: effective threshold;
- `--log-format auto|pretty|json`: concise text or one JSON object per line;
- `--log-file PATH`: enable rotating UTF-8 JSON file logging;
- `--log-component COMPONENT`: repeat to focus on components such as
  `provider`, `budget`, `retrieval`, `semantic`, `synthesis`, `schema`,
  `storage`, `progress`, `configuration`, or `mcp`;
- `--no-log-file`: override and disable configured file logging;
- `--no-color`: disable log/progress color;
- `-v`: raise configured verbosity one level; and
- `-vv`: enable trace. An explicit `--log-level` takes precedence.

Installed entry points accept the global options before the command or after
leaf arguments. Programmatic Typer callers should place them before the
subcommand.

```bash
contextforge context suggest . --task "Fix indexing" --log-level debug
contextforge context suggest . --task "Fix indexing" --log-format json
ctxf --log-component budget -vv context suggest . --task "Fix indexing"
```

JSON logs are JSON Lines on stderr; they do not modify `--format json` stdout.
Pretty redirected output has no ANSI cursor controls.

Context retrieval follows the same separation. `context suggest` defaults to
Index v3 CandidateCards and supports `--format text|markdown|json`;
`benchmark discovery` uses the same three result formats. Without `--output`,
stdout contains only the selected result while progress and logs use stderr. See
[Discovery output and benchmarks](discovery.md) for the canonical-result,
renderer, benchmark, warning, counter, and repeatability contracts.

## Index v3 and Context Capsule commands

```bash
contextforge index build . --provider none --semantic-scope none
contextforge index update . --semantic-scope priority \
  --semantic-max-requests 96 --semantic-max-input-tokens 256000 \
  --semantic-max-chunks-per-file 4
contextforge map . --format json
contextforge context suggest . --task "Trace startup" --working-file src/app.py
contextforge context create . --task "Trace startup" \
  --working-lines src/app.py:1-80 --context-tokens 32768 \
  --history-tokens 4000 --response-tokens 4096 --no-rerank \
  --format json --output capsule.json --prompt-output prompt.xml
```

Semantic scope is `priority`, `all`, or `none`; request, estimated-input-token,
model-file, and chunk ceilings are hard limits. `--working-file` (also
`--include` for suggestion) gives retrieval a Working Set boost.
`--working-lines` also requests exact compiler material.
`--full-file` is the only way to force FULL for files over 200 lines.
`--rerank` enables the bounded provider reranker; `--no-rerank` guarantees zero
query-time provider calls.

Successful build/update summaries read the v3 manifest artifacts and report
`orientation`, `architecture`, `conventions`, and `features` as `current`.
In-repository package, Capsule, and prompt outputs are registered by digest so
an unchanged generated artifact does not enter the next scan. If the user edits
it, it is indexed normally; outputs outside the repository are not registered.

Manual `context create` without `--task` still emits ContextPackage v1.
`--legacy-discovery` and `--legacy-handoff` retain the deprecated task-based
flows. `context inspect` and `context review` accept both legacy JSON artifacts
and Context Capsule v2.

## Local bridge

```bash
contextforge bridge --stdio --workspace /path/to/repository
```

`--stdio` is required. The bridge reads one UTF-8 JSON-RPC 2.0
request per stdin line and writes one response per stdout line. Stdout is
protocol-only; bounded diagnostics use stderr. The client must negotiate
`1.0`, `1.1`, `2.0`, or `2.1` with `hello` before repository requests. The
workspace is fixed for the process lifetime. Bridge 2 may mutate only its index;
Bridge 2.1 map/search/symbol/compile operations are read-only. See the
[bridge guide](bridge.md) for method, timeout, cancellation, snapshot, security,
and shutdown contracts.

## Read-only diagnostics

```bash
contextforge diagnostics last .
contextforge diagnostics last . --format json
contextforge diagnostics show . <operation-id>
contextforge diagnostics config .
contextforge diagnostics provider .
```

`last` and `show` read compact safe summaries from `.contextforge/runs`.
`config` explains precedence and every context-window candidate. `provider`
shows sanitized identity, timeouts, retry policy, context window, and whether a
credential reference exists; it performs no network probe.
