# Structured logging and diagnostics

`contextforge.logging` is the single application diagnostic boundary. Domain
and application code emit stable events without Rich, Typer, terminal width,
ANSI, or interactive behavior. CLI handlers render stderr; future HTTP, SSE,
WebSocket, GUI, MCP, and test adapters consume `DiagnosticRecord.to_dict()` or
`recent_records()` rather than parsing console prose.

## Event contract

Schema version 1 contains timestamp, process-local thread-safe sequence,
level, component, stable event, human message, operation/type, generation,
phase, request/parent request, attempt limits, duration, status, safe data, and
a bounded causal error chain. All values are JSON serializable. Stable events
include operation lifecycle, configuration resolution, budget calculation/
reduction/rejection, provider stages/retries, response parsing/validation,
fallback selection, persistence, and generation publication.

Levels are quiet, error, warning, info, debug, and trace. Component thresholds
may be more verbose than the global threshold; repeatable CLI component focus
acts as an allowlist. Trace payloads remain safe and bounded.

## Sinks and safety

The console sink is pretty or JSON Lines on stderr. The optional rotating file
sink is UTF-8 JSON Lines with handler locking, size rotation, and bounded
retention. Sink failure emits one best-effort warning and never controls the
operation or active generation. The Rich progress adapter retains sole console
ownership and temporarily proxies stderr handlers while live.

Central recursive redaction recognizes sensitive field classes, not just
literal replacements. It removes authorization, bearer/API/password/cookie/
token/credential values, URL user-info, and sensitive query values. Complete
prompts, source, request bodies, response bodies, and raw model output are not
event payloads. Stack traces are bounded, redacted, and file-only.

## Run summaries

Significant application operations atomically persist compact summaries in
`.contextforge/runs`. Summaries include correlation, outcome/generation,
provider/model roles, context sources, reproducible budget breakdowns, request
and token totals, retries, failed/fallback phases, final code, safe causal chain,
and remediation. Source, prompts, credentials, and raw responses are excluded.
Summary persistence is diagnostic only and cannot roll back or corrupt work.
