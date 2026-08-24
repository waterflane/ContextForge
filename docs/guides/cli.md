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

Discovery commands follow the same separation. `context suggest` defaults to
text and supports `--format text|markdown|json`; `benchmark discovery` uses the
same three result formats. Without `--output`, stdout contains only the selected
result while progress and logs use stderr. See
[Discovery output and benchmarks](discovery.md) for the canonical-result,
renderer, benchmark, warning, counter, and repeatability contracts.

## Local bridge

```bash
contextforge bridge --stdio --workspace /path/to/repository
```

`--stdio` is required in protocol v1. The bridge reads one UTF-8 JSON-RPC 2.0
request per stdin line and writes one response per stdout line. Stdout is
protocol-only; bounded diagnostics use stderr. The client must negotiate
protocol `1.0` with `hello` before repository requests. The workspace is fixed
for the process lifetime and all bridge capabilities are model-free and
read-only. See the [bridge v1 guide](bridge.md) for the method, cancellation,
snapshot, security, and shutdown contracts.

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
