# Generic bridge protocols

ContextForge Bridge is a persistent, workspace-bound service for trusted local
integrations. Protocols 1.0 and 1.1 are model-free and read-only. Protocol 2.0
adds opt-in, tracked index mutation through the same application workflow used
by the CLI. Protocol 2.1 adds generation-pinned read-only map, search, symbol,
and Context Capsule compilation plus an independent whole-job timeout. Start it
as a child process:

```bash
contextforge bridge --stdio --workspace /path/to/repository
```

It is not a remote API, sandbox, session manager, or agent orchestrator. For
discovery the consumer owns model selection, prompts, working-set state, and
candidate choice. A Bridge 2 index request may select a configured provider and
bounded execution policy, but cannot supply prompts. ContextForge retains
repository truth: scanning,
ignore/protection policy, current index provenance, path authorization, source
identity, verified reads, budgets, and canonical package construction.

The transport is JSON-RPC 2.0 over UTF-8 NDJSON. Each request and response is
one JSON object followed by LF. Stdout contains response frames only. Bounded
diagnostics go to stderr; a client must never parse stderr as protocol data.
Repository excerpts and packages on stdout, and operational details on stderr,
can be sensitive.

## Required handshake and versioning

Protocol versioning is independent of the ContextForge package version and the
context-package schema. The first application request must be:

```json
{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol_version":"1.1","client_name":"example"}}
```

The response reports `protocol_version`, `supported_protocol_versions`, exact
method/operation capabilities, limits, schemas, and policy. A client must
compare the selected version with the version it implements before continuing.
Missing negotiation returns `PROTOCOL_NEGOTIATION_REQUIRED`; an unsupported
version returns `INCOMPATIBLE_PROTOCOL_VERSION` and the supported list.

V1 requests and parameter objects are closed. Unknown methods, fields, and
operations fail rather than being ignored. Compatible optional evolution
requires explicit minor-version negotiation. Required fields, changed source or
budget semantics, weaker verification, or changed success/failure meaning
require a new major version. The normative frame schemas are
[`contextforge-bridge-v1.schema.json`](../schemas/contextforge-bridge-v1.schema.json)
and
[`contextforge-bridge-v2.schema.json`](../schemas/contextforge-bridge-v2.schema.json).
The 2.1 additions are defined by
[`contextforge-bridge-v2.1.schema.json`](../schemas/contextforge-bridge-v2.1.schema.json).

`hello.capabilities.schemas` reports independent persisted/wire formats rather
than inferring them from the bridge version. Bridge 2.1 advertises index,
manifest, record, and Semantic Card schema 3, retrieval schema 3, Context
Capsule schema 2, progress schema 3, and legacy ContextPackage schema 1.
Earlier negotiated versions retain their original capability shape.

## Repository flow

1. Call `snapshot` and retain its `snapshot_digest`.
2. Pass that value as `expected_snapshot_digest` to every repository-sensitive
   `discover`, `expand`, `read`, or `package` request.
3. Give the consumer or its selected model the deterministic candidate metadata.
4. Optionally call one of the closed `expand` operations to obtain more bounded
   evidence.
5. Send caller-selected candidate IDs to `read` or `package`. Including the
   candidate `path` and `source_sha256` adds explicit identity assertions.

Example discovery request:

```json
{"jsonrpc":"2.0","id":3,"method":"discover","params":{"expected_snapshot_digest":"<64 hex characters>","task":"Locate the configuration loader","mode":"hybrid"}}
```

`discover` is model-free: `model_provider_used` is always `false`. Ranking is
deterministic for the pinned repository/index state. The consumer may use any
model, no model, or human input to select candidates. ContextForge never accepts
an arbitrary path as a substitute for a prepared candidate.

The public `expand` operations in v1 are `symbol`, `text`, `callers`,
`importers`, and `related_tests`. Their `data` is bounded structured evidence.
No internal discovery action IDs, tool names, mutable executors, or model-side
session state are part of the bridge contract.

Version 1.1 adds verified expansion candidates. `text` and `symbol` expansion
results include a `candidates` array whose IDs are registered under the same
`preparation_id`; callers may pass those IDs and ranges directly to `read` or
`package`. Version 1.0 retains its original response shape. Version 1.1 status
also includes index coverage counts so a structural-only or semantic-disabled
index cannot be mistaken for a fully enriched one.

## Method contract

| Method | Required parameters | Result purpose |
| --- | --- | --- |
| `hello` | `protocol_version` | Negotiated version, package version, capabilities, workspace identity, and policy |
| `status` | none | Current readiness, source drift, and read-only index status |
| `snapshot` | none | New authoritative digest and bounded inventory summary |
| `index` (v2) | action and expected snapshot digest | Atomic tracked build/update job using the application workflow |
| `map` (v2.1) | expected snapshot digest | Full pinned orientation map |
| `search` (v2.1) | digest and task | CandidateCards from deterministic retrieval; optional bounded rerank |
| `symbol` (v2.1) | digest and query | Verified exact/qualified symbol matches |
| `compile` (v2.1) | digest, task, and token budget | Retrieval plus Context Capsule v2 compilation |
| `discover` | `expected_snapshot_digest`, `task` | Deterministic candidates and preparation identity; never a model call |
| `expand` | digest, preparation ID, operation | One bounded read-only evidence result and cumulative usage |
| `read` | digest, preparation ID, non-empty items | All-or-nothing verified source excerpts and selection identity |
| `package` | digest, preparation ID, non-empty items | Re-verified canonical `ContextPackage` schema 1 in memory |
| `$/cancelRequest` | target request `id` | Cooperative cancellation notification or optional acknowledgement |
| `shutdown` | none | Stop accepting work and terminate after the response |

Every params object is closed and may also carry `timeout_ms` from 1 through
900,000, except cancellation, which accepts only its target `id`. Discover
defaults to `hybrid`; optional pinned/excluded paths must be unique, sorted,
portable relative paths. Read/package items must be unique and sorted by
candidate ID; line ranges are one-based, inclusive, sorted, and disjoint. See
the normative schema for every budget and response field.

## Bridge 2 tracked index jobs

Negotiate `2.0`, call `snapshot`, then pass the exact digest to `index`:

```json
{"jsonrpc":"2.0","id":"build-7","method":"index","params":{"action":"update","expected_snapshot_digest":"<64 hex characters>","provider":"openai-compatible","model":"exact/model-id","base_url":"http://127.0.0.1:1234/v1","concurrency":2,"max_failures":3}}
```

`action` is `build` or `update`. Optional fields mirror the bounded CLI provider,
model, endpoint, concurrency, per-attempt timeout, context-window, JSON repair,
output-token, semantic scope/request/input/chunk ceilings, failure, force,
file-limit, and stale-lock recovery policies. Bridge 2.1 additionally accepts
`operation_timeout` for the whole job. `fail_fast` and `max_failures` are
mutually exclusive.

The bridge owns scanning, lock acquisition, staging, generation validation, and
atomic publication. The application verifies the expected snapshot against the
exact scan and rescans before each publication. The structural generation is
published first; enrichment may publish a second generation. `timeout_ms`
limits only the client's wait for a response and never cancels an index job.
The job remains tracked by `operation_id`, continues cleanup, and retains the
writer lock. `request_timeout` limits one provider attempt and
`operation_timeout` limits the whole job. Explicit cancellation, clean EOF, and
shutdown still signal the job cancellation token.

While the request runs, Bridge 2 emits notifications before its final response:

```json
{"jsonrpc":"2.0","method":"$/progress","params":{"request_id":"build-7","event":{"schema_version":3,"operation_id":"bridge-index-...","sequence":4,"status":"running"}}}
```

The real `event` is the full closed `ProgressEvent` schema 3 object. Correlate
notifications with `params.request_id`; sequence is monotonic within the
operation but may contain gaps when cumulative snapshots from a synchronous
producer burst are coalesced. A successful result contains `generation_id`,
`snapshot_digest`, `index_schema`, statistics, and `partial`.

Clients should continuously consume Bridge stdout while an index request is
active. Progress delivery uses one writer task and a bounded queue. Overflow
coalesces or replaces intermediate cumulative events and reports coalesced and
dropped counts. Terminal state has priority and is always delivered. A slow or
abandoned progress consumer cannot cancel the index job.

## Bridge 2.1 read-only operations

After negotiating `2.1`, call `snapshot` and pass its digest to every new
operation. `map` returns the pinned orientation record. `search` returns
RetrievalResult v3; reranking is off by default. `symbol` searches verified
CodeMap declarations. `compile` accepts Working Set files/ranges, explicit full
files, optional diff text, and context/history/response/safety budgets, then
returns a Context Capsule v2 and stable prompt.

These methods do not grant source-write, shell, subprocess, Git mutation, or
index mutation authority. A source or generation identity change fails the
request instead of silently recompiling against mixed snapshots.

JSON-RPC standard errors retain their numeric meaning. ContextForge also puts a
stable uppercase typed code in `error.data.code`. Integration-relevant v1 codes
include `PROTOCOL_NEGOTIATION_REQUIRED`, `INCOMPATIBLE_PROTOCOL_VERSION`,
`SNAPSHOT_REQUIRED`, `SOURCE_IDENTITY_CHANGED`, `UNKNOWN_PREPARATION`,
`UNKNOWN_CANDIDATE`, `REQUEST_CANCELLED`, `REQUEST_TIMEOUT`,
`DUPLICATE_REQUEST_ID`, `SHUTTING_DOWN`, `MESSAGE_TOO_LARGE`,
`RESOURCE_LIMIT_EXCEEDED`, `INVALID_SOURCE_RANGE`,
`APPLICATION_REQUEST_REJECTED`, and `INTERNAL_ERROR`. Errors never share a
`result` payload, and internal exceptions or local paths are not returned.

Bridge 2 index failures additionally use dedicated numeric/typed categories:

| JSON-RPC | Typed code | Meaning |
| --- | --- | --- |
| `-32001` | `SOURCE_IDENTITY_CHANGED` | Snapshot drift before or during the job |
| `-32009` | `PROVIDER_FAILURE` / `PROVIDER_CONFIGURATION_ERROR` | Safe provider or configuration failure |
| `-32010` | `FAILURE_LIMIT_REACHED` | `fail_fast`/`max_failures` threshold reached |
| `-32011` | `PROVIDER_CIRCUIT_OPEN` | Provider-wide circuit opened |
| `-32012` | `INDEX_STORAGE_ERROR` | Safe index storage failure |
| `-32013` | `INDEX_LOCKED` | Active or unrecoverable writer lock |

Their `error.data` includes `code`, `error_code`, `phase`, safe `reason`,
`retryable`, and `operation_id`. Provider bodies, credentialed URLs, secrets,
absolute paths, tracebacks, and exception representations are never returned.
`-32603 INTERNAL_ERROR` is reserved for unexpected defects.

## Verified source and identity changes

Index records, semantic summaries, rankings, and consumer/model output are
hints. Current repository source is authoritative. Every source excerpt is
re-authorized against the snapshot and re-read through the stable reader.
ContextForge verifies regular-file identity, portable exact-case path, size,
SHA-256, strict UTF-8 decoding, line ranges, and effective byte/file budgets.
Reads and packages are all-or-nothing; partial successful source is never
returned.

`expected_snapshot_digest` is an optimistic concurrency and identity guard. If
the repository differs from the snapshot captured by `snapshot`, the operation
fails with JSON-RPC code `-32001` and typed code
`SOURCE_IDENTITY_CHANGED`. Discard prepared candidates and excerpts, call
`snapshot` again, and restart discovery. Do not retry the stale request by
silently replacing its digest.

## Cancellation and shutdown

Independent requests may run concurrently and responses can arrive out of
order. Correlate them by JSON-RPC `id`. Response writes are serialized.
Cancellation is cooperative and uses the target request ID:

```json
{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":7}}
```

The target completes normally if it won the race, or fails with typed
`REQUEST_CANCELLED`. Cancellation does not return partial read/package output
or publish a partial index generation. A cancellation notification has no response;
include its own JSON-RPC `id` only when a `{"cancelled": true|false}` response is
needed.

Send `shutdown` after outstanding work is resolved, wait for its response, then
close stdin and wait for the child process. Clean stdin EOF also stops the
bridge. For Bridge 1 this remains read-only. Bridge 2 cancellation cannot
invalidate an already published structural generation; abrupt termination may
leave recoverable staging or lock metadata, while the last active generation
remains authoritative.

Shutdown and clean EOF use the same bounded drain. The bridge first stops
accepting work, signals every active request's cooperative cancellation event,
and waits at most 5 seconds. Any request task still pending then receives direct
asyncio cancellation and gets at most another 0.1 seconds for cleanup. After
that 5.1-second maximum drain budget, the bridge detaches any remaining task and
does not wait for it again. These internal bridge limits are fixed rather than CLI
configurable. Caller timeout returns its JSON-RPC error immediately; the tracked
index job can continue normally until its operation timeout, explicit
cancellation, or shutdown. The shutdown response remains a normal serialized
JSON-RPC frame.

## Security and mutation boundary

Bridge 1.1 coverage includes `semantic_partial_files`,
`semantic_chunks_planned`, and `semantic_chunks_completed`. Partial files do
not count toward `semantic_complete_files`. Bridge 1.0 keeps its existing
response shape. Chunk cache payloads are internal and are not returned in
semantic tool summaries or packaged source.

Run the bridge only as a child process of a trusted local consumer. It inherits
the user's filesystem read authority and intentionally returns repository data.
It provides no authentication, authorization, encryption, tenant isolation, or
protection from another hostile process running as the same user. Do not expose
stdio through a network or untrusted broker without an external security layer.

Bridge v1 cannot write repository source, mutate Git, invoke a shell or arbitrary
subprocess, call a model/provider, access external data, mutate the index, or
publish artifacts to disk. Bridge 2 relaxes only model/provider access under the
configured policy and verified atomic writes beneath `.contextforge/index`.
Bridge 2.1 adds only read-only repository-context operations. No version writes
source or Git state. `package` returns the canonical
package in memory. The workspace is fixed at process start and requests cannot
replace it.

See the runnable [generic bridge client](../../examples/generic_bridge_client.py)
and [troubleshooting guide](troubleshooting.md).
