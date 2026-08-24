# Generic bridge v1

ContextForge bridge v1 is a persistent, workspace-bound, model-free service for
trusted local integrations. Start it as a child process:

```bash
contextforge bridge --stdio --workspace /path/to/repository
```

It is not a remote API, sandbox, session manager, model gateway, or agent
orchestrator. The consumer owns model selection, prompts, retries, working-set
state, and candidate choice. ContextForge retains repository truth: scanning,
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
{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol_version":"1.0","client_name":"example"}}
```

The response reports `protocol_version`, `supported_protocol_versions`, exact
method/operation capabilities, limits, and read-only policy. A client must
compare the selected version with the version it implements before continuing.
Missing negotiation returns `PROTOCOL_NEGOTIATION_REQUIRED`; an unsupported
version returns `INCOMPATIBLE_PROTOCOL_VERSION` and the supported list.

V1 requests and parameter objects are closed. Unknown methods, fields, and
operations fail rather than being ignored. Compatible optional evolution
requires explicit minor-version negotiation. Required fields, changed source or
budget semantics, weaker verification, or changed success/failure meaning
require a new major version. The normative frame schema is
[`contextforge-bridge-v1.schema.json`](../schemas/contextforge-bridge-v1.schema.json).

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

## Method contract

| Method | Required parameters | Result purpose |
| --- | --- | --- |
| `hello` | `protocol_version` | Negotiated version, package version, capabilities, workspace identity, and policy |
| `status` | none | Current readiness, source drift, and read-only index status |
| `snapshot` | none | New authoritative digest and bounded inventory summary |
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

JSON-RPC standard errors retain their numeric meaning. ContextForge also puts a
stable uppercase typed code in `error.data.code`. Integration-relevant v1 codes
include `PROTOCOL_NEGOTIATION_REQUIRED`, `INCOMPATIBLE_PROTOCOL_VERSION`,
`SNAPSHOT_REQUIRED`, `SOURCE_IDENTITY_CHANGED`, `UNKNOWN_PREPARATION`,
`UNKNOWN_CANDIDATE`, `REQUEST_CANCELLED`, `REQUEST_TIMEOUT`,
`DUPLICATE_REQUEST_ID`, `SHUTTING_DOWN`, `MESSAGE_TOO_LARGE`,
`RESOURCE_LIMIT_EXCEEDED`, `INVALID_SOURCE_RANGE`,
`APPLICATION_REQUEST_REJECTED`, and `INTERNAL_ERROR`. Errors never share a
`result` payload, and internal exceptions or local paths are not returned.

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
and grants no mutation capability. A cancellation notification has no response;
include its own JSON-RPC `id` only when a `{"cancelled": true|false}` response is
needed.

Send `shutdown` after outstanding work is resolved, wait for its response, then
close stdin and wait for the child process. Clean stdin EOF also stops the
bridge. Abrupt process termination is safe with respect to repository and index
state because bridge operations are read-only.

Shutdown and clean EOF use the same bounded drain. The bridge first stops
accepting work, signals every active request's cooperative cancellation event,
and waits at most 5 seconds. Any request task still pending then receives direct
asyncio cancellation and gets at most another 0.1 seconds for cleanup. After
that 5.1-second maximum drain budget, the bridge detaches any remaining task and
does not wait for it again. These internal v1 limits are fixed rather than CLI
configurable. A timed-out request cannot return a partial success, and the
shutdown response remains a normal serialized JSON-RPC frame.

## Security and read-only boundary

Run the bridge only as a child process of a trusted local consumer. It inherits
the user's filesystem read authority and intentionally returns repository data.
It provides no authentication, authorization, encryption, tenant isolation, or
protection from another hostile process running as the same user. Do not expose
stdio through a network or untrusted broker without an external security layer.

Bridge v1 cannot write repository source, mutate Git, invoke a shell or arbitrary
subprocess, call a model/provider, access external data, mutate the index, or
publish artifacts to disk. `package` returns the canonical package in memory.
The workspace is fixed at process start and requests cannot replace it.

See the runnable [generic bridge client](../../examples/generic_bridge_client.py)
and [troubleshooting guide](troubleshooting.md).
