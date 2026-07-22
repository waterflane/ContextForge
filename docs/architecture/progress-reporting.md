# Application progress reporting

## Boundary

`contextforge.progress` is the shared, presentation-independent progress
contract for application workflows. It is public from both `contextforge` and
`contextforge.core`. The contract has no dependency on Typer, Rich, terminals,
HTTP, GUI frameworks, or MCP transports. An interface adapter decides how to
render, transmit, aggregate, or discard events.

The application layer emits weighted progress from synchronous index inspection
and from asynchronous index build/update, indexed/fresh/hybrid discovery,
automatic handoff materialization, and prompt compilation. Reporting remains
phase-based, with bounded per-file updates where the work total is known.

Progress is not logging. A progress event describes current operation state;
the versioned structured records in `contextforge.logging` preserve diagnostic
facts and decisions. Reporter creation/terminal state may produce one lifecycle
log, but spinner refreshes and repeated live-panel paints never become log
records. Application and domain code remain independent of Rich and Typer.

## Event schema

`ProgressEvent` is a frozen Pydantic model with unknown fields forbidden. New
events use schema version `3`; schema-version-1 and version-2 payloads remain
accepted and receive compatibility defaults. `model_dump(mode="json")` or
`model_dump_json()` produces JSON-compatible data.

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

Schema version 2 adds display-complete, presentation-neutral fields:

- `overall_percent` mirrors the retained `percentage` field;
- `phase_label`, `phase_percent`, and `phase_weight` describe the current
  weighted phase;
- `completed_units`, `total_units`, and `unit_type` describe phase work;
- `current_item`, `last_completed_item`, and `last_failed_item` expose bounded
  portable paths without source content;
- `active_items` and `active_item_count` represent concurrent work;
- `reused_units`, `skipped_units`, and `failed_units` keep non-request outcomes
  explicit;
- `elapsed_seconds` is measured from the operation reporter; and
- `activity` is `idle`, `active`, or `waiting`.

Schema version 3 makes semantic accounting and provider lifecycle explicit:

- `planned_units` and `processed_units` define the semantic phase metric;
- `succeeded_units`, `fallback_units`, `failed_units`, `skipped_units`, and
  `reused_units` split terminal outcomes without inference;
- `active_units` mirrors the concurrent active count;
- `current_attempt` and `max_attempts` expose bounded retries;
- `lifecycle_state` distinguishes planning, provider wait, validation,
  in-memory acceptance, durable staging, retry, failure, and publication;
- `safe_error_code` and `safe_error_message` provide bounded diagnostics;
- `request_elapsed_seconds` resets for each attempt; and
- `operation_elapsed_seconds` measures the whole operation;
- `analyzer_kind`, `estimated_input_tokens`, `output_token_budget`, and
  `input_truncated` expose safe request planning metrics without prompt or source
  material; and
- `configured_context_window`, `schema_overhead_tokens`,
  `safety_margin_tokens`, and `estimated_total_tokens` expose the complete safe
  preflight accounting.

Map requests use the same event stream and renderer. Lifecycle messages can
identify context reduction, schema rejection, JSON-object fallback, validation,
or deterministic map fallback. Error codes remain bounded and never include raw
provider output.

These fields are sufficient for an HTTP API, SSE/WebSocket stream, or GUI to
reproduce terminal state without parsing `message` or any CLI output. Provider
and model IDs may appear in metadata; endpoints, credentials, prompts, request
bodies, responses, and source contents never do.

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

`ProgressReporter.scaled_observer()` maps an existing child `ProgressEvent`
stream into an explicit weighted range of its parent operation. This is a
composition facility on the same contract, not a second progress abstraction.
Child completion closes its assigned phase; only the parent workflow emits the
parent terminal event.

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

CLI rendering is outside the application contract.
`contextforge.cli.progress.CLIProgressRenderer` is the
single Typer adapter and the top-level CLI command is its sole owner. Nested
work emits events only. A stream-scoped, synchronized ownership guard makes
repeated initialization, start, refresh, and close idempotent; a nested adapter
delegates to the owner and cannot create or stop another `Live` instance.

The adapter uses Typer's actual Rich stderr console rather than testing stdout.
In `auto` mode an interactive stderr receives one transient Rich `Live` panel
with spinner, bars, items, elapsed time, and provider/model.
The panel refreshes its spinner and elapsed clock while a provider request is
quiet without emitting fake progress events or changing percentages. Redirected
stderr receives coalesced, non-ANSI records only for meaningful phase,
percentage, item, counter, or terminal changes. `never` suppresses rendering;
`always` never forces terminal controls onto an unsafe redirected stream.

Direct stderr and existing stderr logging handlers are routed through the same
live console while it is active, then restored on the single stop path. This
prints diagnostics above the panel instead of leaving a duplicate frame. All
terminal outcomes and the final defensive close converge on exactly-once cursor
restoration.

The centralized structured stderr handler participates in this ownership
mechanism. Pretty/JSON lines are printed above a live panel through Rich's file
proxy, then handlers are restored. Redirected stderr receives discrete plain
lines with no cursor movement. Logs do not render a second panel and progress
metadata never carries prompts, source, responses, or credentials.

The details layout reserves 16 characters for complete labels such as
`Processed`, `Last failure`, and `Request elapsed`; values fold or wrap. Below
58 columns, labels and values use separate lines. The panel width is capped at
the current console width and remeasured on refresh. ASCII borders and spinner
are used when Unicode is unavailable.

## Weighted workflow phases

Model-enabled index builds use the following cost-oriented default ranges:

- initialization and scan: 0–5%;
- incremental comparison and planning: 5–8%;
- structural extraction: 8–18%;
- semantic per-file model analysis: 18–82%;
- model-backed global repository maps: 82–94%;
- deterministic relationships/finalization: 94–97%; and
- validation and atomic publication: 97–100%.

Structural-only builds use 0–10% scan, 10–15% planning, 15–75% structural work,
75–90% deterministic finalization, and 90–100% validation/publication. If an
incremental model phase contains no provider work, it completes at its start and
its unused range is assigned to the remaining model-map phase rather than
showing a misleading semantic jump.

Within semantic analysis, the denominator is the complete routed plan, including
model analysis, deterministic summaries, reused records, and explicit skipped
or preflight outcomes. The phase bar uses the documented metric
`processed_units / planned_units`; “processed” means an item reached one terminal
state and does not imply success. Retries remain inside one work item. A failed
item therefore produces `processed=1`, `succeeded=0`, `failed=1`, and cannot be
rendered as `0/N` with a non-zero phase percentage.

Overall semantic percentage uses deterministic cost weights. Metadata, reuse,
and skip routes cost one unit. Model routes cost eight base units plus
`ceil(size_bytes / 32768)`, with source units capped at 16 per file. The model
range therefore dominates expensive work while cheap routes finish quickly.

An `analyzing` event is emitted immediately before a file's provider work and
sets `current_item`, active paths, and `activity=waiting`. Completion/failure is
emitted from that task as soon as it finishes, before later batches, and clears
or replaces the old current item. Event-loop-local set updates keep concurrent
completion monotonic. Publication remains below 100 until the active generation
is reloaded and checked after its atomic pointer switch.

Example semantic event (abridged):

```json
{
  "schema_version": 3,
  "operation_type": "repository.index.build",
  "overall_percent": 53.0,
  "phase_id": "semantic_analysis",
  "phase_label": "Semantic analysis",
  "phase_percent": 31.0,
  "phase_weight": 64.0,
  "completed_units": 9.0,
  "total_units": 26.0,
  "unit_type": "items",
  "planned_units": 26,
  "processed_units": 9,
  "succeeded_units": 8,
  "fallback_units": 0,
  "current_item": "src/service.py",
  "last_completed_item": "src/app.py",
  "active_item_count": 2,
  "reused_units": 4,
  "skipped_units": 0,
  "failed_units": 1,
  "last_failed_item": ".env.example",
  "safe_error_code": "structured_output_validation_failed",
  "safe_error_message": "structured response validation failed",
  "current_attempt": 1,
  "max_attempts": 3,
  "lifecycle_state": "waiting_for_provider",
  "request_elapsed_seconds": 12.4,
  "analyzer_kind": "generic-text-semantic",
  "estimated_input_tokens": 824,
  "output_token_budget": 192,
  "input_truncated": false,
  "operation_elapsed_seconds": 94.1,
  "activity": "waiting"
}
```

Discovery composes mode-aware knowledge loading, bounded model/tool analysis,
final selection verification, handoff review, source re-scan, package and
CodeMap materialization, optional Git context, and prompt compilation. The
indexed, fresh, and hybrid modes share the event contract while retaining their
mode in metadata and phase names.

Example application usage:

```python
events: list[ProgressEvent] = []
report = await build_repository_index(
    repository,
    provider=provider,
    provider_configuration=configuration,
    progress=events.append,
    operation_id="api-index-42",
)
```

Observers are invoked only after reporter state is validated. Their failures are
isolated, including during validation, rollback, and publication, so they cannot
change index identity, semantic records, or transactional outcomes.

This contract and its CLI adapter are shipped in 0.4.1. HTTP streaming and GUI
renderers remain future adapter work; their suitability is a design property of
the structured event schema, not a claim that those interfaces are implemented.
