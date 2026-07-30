# Model-guided repository discovery

## Implemented boundary

`contextforge.discovery` provides bounded, read-only task-context discovery in
`indexed`, `fresh`, and `hybrid` modes. Hybrid is the default. The component
returns a `DiscoveryRunRecord` containing a reviewable `FinalContextSelection`;
it does not itself construct or change `ContextPackage` schema 1, execute
repository code, or modify source. Thin CLI and read-only MCP adapters call this
same service. The separate [Context handoff](context-handoffs.md) boundary
validates a selection, re-scans and re-verifies current source, and delegates
package construction to the existing builder.

Every session is pinned to a caller-supplied `ProjectSnapshot`. Initial index,
symbol, text, or semantic matches are hints only. The trusted model request
contains the complete allowed portable path inventory, and tree, text, and
verified-read tools remain capable of reaching every permitted snapshot file
until a caller-selected hard budget is exhausted.

## Mode semantics

- `indexed` requires a readable active generation and at least one current
  structural record. Hash-mismatched records and stale repository-wide maps are
  unavailable and disclosed. Selected source is verified again before success.
- `fresh` does not load persistent file semantics, architecture maps, or feature
  maps. It derives current in-memory CodeMaps within the read budget and never
  publishes semantic conclusions.
- `hybrid` uses current index records as an initial map, fills current
  structural gaps in memory, permits investigation outside index candidates,
  and degrades explicitly to fresh structural discovery when no valid index is
  available.

The index is never source truth. A source identity mismatch during investigation
or final verification aborts without returning a partial successful selection.

## Public API

The main entry point is asynchronous:

```python
record = await discover_repository(snapshot, provider, request)
```

`DiscoverySession` exposes the same lifecycle for callers that need in-progress
state. Core closed models include `DiscoveryRequest`, `DiscoveryMode`,
`DiscoveryAction`, `DiscoveryObservation`, `DiscoveryState`, `DiscoveryBudget`,
`DiscoveryCandidate`, `SelectionReason`, `FinalContextSelection`,
`CompletenessWarning`, and `DiscoveryRunRecord`. Typed failures carry a run
record whose `final_selection` is always absent.

## Tool and security boundary

`DISCOVERY_TOOL_SCHEMAS` publishes closed input schemas for repository overview,
tree, index/symbol/text search, file/symbol summaries, imports, importers,
references, callers, related tests, verified full/ranged reads, injected bounded
Git diff data, ephemeral context mutation, budget inspection, and finalization.

All paths pass strict portable validation and exact case-sensitive snapshot
membership checks. Absolute, drive-relative, UNC, backslash, traversal, NUL,
ignored, protected, binary, oversized, linked, and junction paths cannot be
opened. Reads reuse the existing stable verified reader. Discovery contains no
shell, subprocess, network, arbitrary filesystem, source-write, index-write,
secret, CLI, or MCP operation. Optional Git data must come from a separately
trusted `GitDiffProvider`; discovery never invokes Git or a process itself.

Repository source, index prose, diffs, and prior observations are framed as
untrusted model context. Only ContextForge orchestration supplies system
instructions. Every action is schema-validated before dispatch.

Fresh action requests state the required non-empty `actions` array and include a
minimal valid one-action example. The schema remains closed and strict. A
session-level structured-action circuit breaker fingerprints each validation
failure as `structured-validation-v1:` plus the SHA-256 of canonical JSON
containing only schema path, issue type, and relevant constraint. Three
equivalent failures without an intervening tool result that makes meaningful
progress stop model-assisted repair and select the existing deterministic
fallback. Distinct fingerprints have independent counts; meaningful tool
progress clears the active counts while lifetime diagnostic totals remain
tracked. A valid response received before the third equivalent failure is
accepted normally.

## Budgets and completeness

Hard limits cover steps, model calls, files read, source bytes, tool-result
bytes, final context files and bytes, repeated non-progress actions, cancellation,
and total elapsed time. Result and source byte accounting is authoritative.

The first finalization attempt runs an advisory missing-context review over
direct imports/importers, statically resolved callers, related tests,
configuration consumers, mapped public entry points, relevant diff paths, and
documentation signals. Warnings request one final model pass and remain in the
result. Parse gaps, unresolved calls, dynamic dispatch, semantic uncertainty,
and stale coverage lower confidence and explicitly recommend broader review;
they never become a false completeness guarantee or trigger automatic inclusion
of an entire dependency graph.
