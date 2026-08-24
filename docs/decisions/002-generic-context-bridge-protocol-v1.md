# ADR-002: Generic ContextForge bridge protocol v1

## Status

Accepted and implemented for the ContextForge 0.5.0 foundation.

## Context

Local agents, IDE integrations, MCP adapters, and external harnesses need the
same repository discovery capabilities without importing ContextForge's mutable
`DiscoverySession` or forcing ContextForge to choose or call their model.
Duplicating repository scanning, index freshness checks, path authorization, or
source reads in each integration would create competing definitions of
repository truth and weaken the existing security boundary.

The bridge is a persistent workspace-bound process started with
`contextforge bridge --stdio --workspace PATH`. It uses JSON-RPC 2.0 over
UTF-8 NDJSON and retains only bounded preparation state in memory.

## Decision

ContextForge defines a generic bridge protocol v1 around four operations:

- `prepare_discovery_candidates` produces deterministic, bounded candidates;
- `expand_discovery` performs one closed read-only evidence operation;
- `read_verified_context` verifies a caller-owned candidate selection;
- `package_verified_context` re-verifies and builds the canonical context
  package.

The Python application API uses frozen, closed Pydantic DTOs. It never returns
`DiscoverySession`, `DiscoveryKnowledge`, `DiscoveryToolExecutor`, filesystem
handles, or mutable budget trackers. The protocol envelope is generic and
transport-neutral. DeepSeek Harness can consume it, but no message, operation,
or Python type is named for or coupled to that harness.

## Ownership of repository truth

ContextForge owns repository truth for every operation. The caller identifies a
repository root or passes an already verified in-process `ProjectSnapshot`.
ContextForge scans according to its existing ignore/protection policy, produces
portable case-sensitive paths and source identities, and authorizes all reads
against that inventory. Index records and model-generated semantics are hints;
they never override current source.

The integration owns task orchestration and candidate choice. It may use any
model, no model, or user input, but can select only identifiers issued by the
preparation it supplies back to ContextForge.

## Snapshot and index pinning

Candidate preparation includes a `source_snapshot_digest`, optional immutable
`index_generation_id`, and deterministic `preparation_id`. Later operations
must present that preparation. ContextForge re-scans or checks the supplied
snapshot and rejects the operation if repository identities, the pinned index
generation, mode semantics, request constraints, candidates, or warnings no
longer match.

`fresh` ignores persistent semantic and repository-wide maps. `indexed`
requires current structural records. `hybrid` uses current indexed records and
fills structural gaps from current source. Stale records are excluded, stale
global maps are not used, and stale paths are disclosed.

## Source verification and budgets

Candidate metadata carries portable paths, byte sizes, and SHA-256 source
identities. Expansion reads reuse the closed discovery tool schemas and stable
reader. Final reads are all-or-nothing, re-authorize exact snapshot membership,
reject links and traversal, verify size and hash, decode strict UTF-8, and apply
file/source/result/context budgets. Package construction re-reads source and
checks the produced block identities against the verified DTO.

Budget usage is explicit and immutable at the public boundary. A caller carries
forward the latest usage between otherwise stateless expansion messages. A
caller cannot lower already charged preparation usage.

## Cancellation

Protocol v1 assigns an optional `cancellation_id` to a request and defines a
separate cancellation message. Cancellation is cooperative and all-or-nothing:
no cancelled read or package operation returns a partial successful artifact.
Transport adapters will map cancellation IDs to application cancellation
signals. They must not interpret cancellation as permission to mutate source or
the index.

## Transport isolation

Protocol v1 exposes `hello`, `status`, `snapshot`, `discover`, `expand`, `read`,
`package`, `$/cancelRequest`, and `shutdown`. Standard input is bounded UTF-8
NDJSON and standard output contains only serialized JSON-RPC response frames.
Diagnostics are bounded and written only to standard error. Independent
requests may run concurrently, while all response writes are serialized.

Repository-sensitive methods require `expected_snapshot_digest` after
`snapshot`. Drift is returned as typed `SOURCE_IDENTITY_CHANGED`, never as a
partial result. Cancellation IDs are JSON-RPC request IDs and cancellation is
carried into the application operation through its cooperative event.

The adapter never exposes shell execution, source writes, Git mutation,
arbitrary subprocesses, path-policy bypasses, or direct index mutation. Package
results remain in memory and are returned in the response.

## Model ownership

Model selection remains outside the Python bridge. ContextForge cannot know an
integration's latency, cost, privacy, routing, or capability policy. Candidate
preparation and evidence expansion are deterministic and require no
`ModelProvider`. Existing model-assisted `discover_repository` remains a
compatible in-process workflow and continues to own its provider lifecycle.

## Compatibility and versioning

Protocol version `1.0` is independent of the Python package version and of the
discovery/context package schema versions. V1 messages are closed: unknown
fields and unknown operations are rejected. Within v1, new optional fields may
be added only when old readers can safely ignore them through a negotiated
minor-version capability; otherwise the protocol major version changes.
Required-field changes, changed path or budget semantics, weaker verification,
or changed success/failure meaning require a new major version.

Serialized DTOs use deterministic UTF-8 JSON with sorted keys, no non-finite
numbers, and a final LF when persisted. Request and response IDs are correlation
metadata, not repository or source identities. Error responses are structured,
safe, and never share a result payload.

## Consequences

- Harnesses and IDEs can orchestrate discovery without a Python-side model.
- Existing CLI 0.4.x commands and model-assisted discovery remain unchanged.
- Repository truth, immutable index generations, verification, and path policy
  remain centralized.
- Stateful transport concerns and richer working-set/session behavior remain
  explicitly deferred.
