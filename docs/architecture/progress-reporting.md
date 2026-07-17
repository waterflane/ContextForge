# Application progress reporting

## Boundary

`contextforge.progress` is the shared, presentation-independent progress
contract for application workflows. It is public from both `contextforge` and
`contextforge.core`. The contract has no dependency on Typer, Rich, terminals,
HTTP, GUI frameworks, or MCP transports. An interface adapter decides how to
render, transmit, aggregate, or discard events.

The application layer currently emits coarse progress from synchronous index
inspection and from asynchronous index build, context discovery, and automatic
handoff workflows. Reporting is deliberately phase-based. Lower-level loops do
not emit an event for every file, symbol, token, or model response.

## Event schema

`ProgressEvent` is a frozen Pydantic model with unknown fields forbidden. Its
schema version is `1`, and `model_dump(mode="json")` or `model_dump_json()`
produces JSON-compatible data.

Each event contains:

- `operation_id`: the identity of one invocation;
- `operation_type`: a stable machine-readable workflow kind;
- `phase_id` and `message`: a machine-readable phase and a human-readable,
  presentation-neutral description;
- `completed` and optional `total`: work units when they are meaningful;
- `percentage`: a value from `0` through `100`;
- `status`: `running`, `completed`, `failed`, or `cancelled`;
- optional `parent_operation_id`: hierarchy for nested work;
- `metadata`: JSON-only adapter or workflow context;
- `sequence`: a deterministic zero-based event order within the operation; and
- `indeterminate`: whether the current underlying workload has an unknown
  extent.

Percentages may represent weighted phase units when the underlying workload is
unknown. In that case an adapter can still draw overall progress. For a truly
indeterminate phase, `total` may be `null` and `indeterminate` may be true.

Validation rejects out-of-range or non-finite percentages, non-JSON metadata,
work completed beyond a known total, self-parenting operations, and malformed
or oversized identifiers and messages. Only a successful `completed` event may
report `100` percent.

## Reporter and observer

`ProgressObserver` is a synchronous callback type. This keeps synchronous
workflows synchronous and lets asynchronous workflows invoke the same contract
without changing their result boundary. An async transport such as MCP or an
HTTP stream should use a cheap callback that enqueues the immutable event for
transport-specific delivery rather than doing blocking I/O in the callback.

`NoOpProgressObserver` and the shared `NO_OP_PROGRESS_OBSERVER` discard events.
Application workflow parameters default to no observer, so existing callers
remain source-compatible and retain their existing results.

`ProgressReporter` owns one operation stream. It:

- assigns deterministic sequence numbers;
- rejects decreasing percentages;
- permits repeated percentages for phase detail;
- emits successful completion at exactly `100` percent;
- preserves the last percentage for failed or cancelled terminal events;
- rejects events after the first terminal event;
- merges operation and phase metadata; and
- isolates every exception raised by an optional observer.

Observer delivery happens after an event has been validated and recorded by
the reporter. Observer exceptions, including cancellation-like base
exceptions, are counted by `observer_error_count` and never replace the
workflow result or exception. For mutating index builds, failure notification
is outside the lock and rollback implementation, so observer behavior cannot
interfere with repository recovery or active-index publication.

## Application usage

Application workflows accept optional `progress`, `operation_id`, and
`parent_operation_id` keyword arguments. Omitting them preserves the original
call form. A caller that needs correlation should supply its own operation ID;
otherwise the application creates a unique ID and exposes it in every emitted
event.

Successful operations always end with one `completed` event at `100` percent.
Exceptions emit a `failed` terminal event and are re-raised unchanged. Task,
provider, and discovery cancellation emit a `cancelled` terminal event and are
also re-raised unchanged. Terminal failure metadata includes only the exception
type, not exception text that could contain source content or secrets.

CLI rendering is outside this contract and is intentionally not implemented by
the progress foundation.
