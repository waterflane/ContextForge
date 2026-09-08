# ADR-003: Host-managed index jobs and stable provider identity

## Status

Accepted for ContextForge 0.5.1.

## Context

Local hosts need to build and update ContextForge indexes without duplicating
writer-lock, staging, publication, progress, timeout, and cancellation logic.
The CLI previously exposed only human progress and `--fail-on-error`, which
finishes all semantic work before declining publication. Repeated provider-wide
failures could therefore spend one retry sequence per file. Bridge 1 is
intentionally model-free and read-only, so it cannot own this lifecycle.

OpenAI-compatible endpoint URLs were also encoded into analyzer versions. A
temporary loopback port could invalidate every model-backed record even when
the provider, model, prompts, and response contracts were unchanged.

## Decision

The application index workflow accepts cooperative cancellation and independent
failure limits. `fail_fast` means one failed semantic unit; `max_failures`
defines another positive threshold. They are mutually exclusive. Reaching a
threshold stops issuing work, cancels in-flight provider waits, aborts the
publication transaction, and preserves the prior active generation.
`fail_on_error` without a limit retains its original finish-then-refuse behavior.

A job-scoped circuit breaker sits in the shared provider runtime. Typed
authentication, authorization, missing credential, quota, model, and
configuration failures open it immediately. Three consecutive exhausted
transient failures with the same safe error code and provider/model identity
also open it; success resets the sequence. The key never includes response
text, endpoints, or secrets.

CLI index commands expose `--progress jsonl` as a pure flushed stream of full
`ProgressEvent` schema 3 objects. Human summary output is suppressed and
diagnostics remain on stderr.

Bridge protocol 2.0 adds a closed `index` method for `build` and `update`. It
requires `expected_snapshot_digest`, owns the complete application workflow,
and sends correlated `$/progress` notifications. Cancellation, caller timeout,
EOF, and shutdown feed the same cooperative cancellation event. Bridge 1.0 and
1.1 remain read-only and model-free. Bridge capabilities explicitly publish
current and readable index, manifest, record, progress, and context-package
schema versions.

The application validates the expected digest against its own build snapshot
and checks cancellation inside the publication transaction immediately before
manifest activation. Bridge timeout sets cooperative cancellation before it
returns the deadline error, and the bridge tracks background cleanup so a
structural worker retains its writer lock until it has stopped. Progress
notifications use one writer task and a bounded queue. Cumulative snapshots from
synchronous producer bursts are coalesced; sustained backpressure cancels rather
than accumulating unbounded tasks.

Analyzer identity includes analyzer, prompt, response-schema, provider, and
model identity, but excludes transport endpoint. Legacy analyzer versions with
a terminal `+base.<sha256>` suffix compare as the neutral identity. The next
update republishes semantic and repository-map records with the neutral
identity without new model calls.

Known Bridge index failures use safe typed JSON-RPC categories and bounded
structured data. Raw provider bodies, credentialed URLs, secrets, absolute
paths, tracebacks, and exception representations are excluded. `-32603` is
reserved for unexpected defects.

## Consequences

- Hosts can track and cancel one atomic index job without managing internal
  storage state.
- A failed provider cannot trigger unbounded repository-wide repeated calls.
- CLI subprocess integrations receive stable machine progress without parsing
  Rich output.
- Endpoint changes no longer create false staleness, while provider/model or
  analysis-contract changes still invalidate records.
- Bridge 2 has narrowly scoped index and provider authority; it still cannot
  write source, mutate Git, or execute arbitrary commands.
- Persisted index, manifest, and record schemas remain version 2; progress
  remains version 3 in ContextForge 0.5.1.
