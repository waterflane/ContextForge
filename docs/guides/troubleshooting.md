# Troubleshooting

## Model request exceeds configured context window

This message can be a local ContextForge preflight rejection. It does not mean
LM Studio rejected the request. Capture debug diagnostics and inspect sources:

```bash
contextforge context suggest . --task "..." --log-level debug
contextforge diagnostics config .
contextforge diagnostics last . --format json
```

Four values are distinct:

1. the LM Studio loaded-model context configured outside ContextForge;
2. any provider-reported or model-metadata context (OpenAI-compatible APIs do
   not standardize this);
3. ContextForge's configured `context_window`, resolved by precedence; and
4. the effective request budget: context window minus output, protocol, schema,
   and safety reserves.

For example, LM Studio/model metadata may report 98,304 while
`.contextforge/config.toml` explicitly sets 16,384. ContextForge uses 16,384,
logs `effective_context_window_source=config.toml`, and if a 22,140-token
estimate does not fit emits `budget.rejected` with
`request_dispatched=false`. Increase ContextForge `context_window` to the
verified loaded-model capacity, reduce candidates, enable hierarchical
synthesis, or select another provider. Do not merely increase LM Studio while
leaving the lower ContextForge override in place.

The budget record decomposes system, user, source, selected index, schema,
requested output, protocol overhead, and safety margin so the total can be
reproduced numerically without exposing the prompt.

## Bridge exits, hangs, or returns no parseable response

Confirm the installed command and protocol help first:

```bash
contextforge --version
contextforge bridge --help
```

Start with `--stdio` and send exactly one UTF-8 JSON object plus LF per request.
The first application request must be `hello` with
`{"protocol_version":"1.0"}`. `PROTOCOL_NEGOTIATION_REQUIRED` means a
repository request arrived before a successful hello.
`INCOMPATIBLE_PROTOCOL_VERSION` means the client must stop and use one of the
reported `supported_protocol_versions`; do not guess compatibility from the
Python package version.

Read responses only from stdout and correlate by JSON-RPC `id`; concurrent
responses may be out of order. Read stderr separately for bounded diagnostics.
Do not combine stderr into stdout, add terminal prompts to stdin, pretty-print a
request across lines, or buffer a request without its final LF. The maximum v1
frame size is reported by `hello` and oversized frames fail with
`MESSAGE_TOO_LARGE`.

## Bridge reports SOURCE_IDENTITY_CHANGED

The repository no longer matches the digest returned by `snapshot`, or a
selected candidate's optional `path`/`source_sha256` assertion changed. Discard
the preparation and any excerpts derived from it, call `snapshot` again, then
repeat discovery and selection. Do not substitute the new digest into an old
request: candidates and ranges belong to the old repository truth.

For reproducible runs, pause formatters, generators, checkout operations, and
other processes that rewrite the workspace. The bridge detects identity drift
but is not a sandbox against a hostile same-account writer.

## Cancellation did not stop immediately

`$/cancelRequest` is cooperative and targets the JSON-RPC request `id`. The
operation may finish before cancellation is observed. A cancelled operation
returns `REQUEST_CANCELLED` and never a partial successful read or package.
After shutdown begins, new work fails with `SHUTTING_DOWN`.

## Bridge cannot find a preparation

`UNKNOWN_PREPARATION` means the ID belongs to another bridge process/snapshot or
was evicted from the bounded in-memory cache. Call `snapshot` and `discover`
again in the same process. Preparation state is intentionally not persisted.
