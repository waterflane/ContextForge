# ContextForge RepoPrompt Feature-Parity Direction

Status: companion to
[ContextForge v0.4.0 — Repository Intelligence](repository-intelligence.md).

## Purpose and comparison boundary

This matrix is a planning aid, not a compatibility or equivalence claim.
RepoPrompt currently documents a context-builder flow of exploration,
curation, and handoff; CodeMaps, token budgets, Git diffs, MCP integrations,
CLI-agent integrations, a native workspace UI, and agent orchestration are
also documented on its [overview](https://repoprompt.com/docs) and
[prompt anatomy](https://repoprompt.com/repo-prompt-anatomy) pages.

ContextForge has a different product boundary: deterministic, portable,
reviewable repository context with source verification. v0.4 adds intelligence
and read-only integrations, but it does not become a coding-agent control plane
or native IDE.

Classification values are exactly:

- **Already implemented**
- **Implemented in v0.4.0**
- **Deferred to a later milestone**
- **Intentionally not planned**

## Feature matrix

| Feature | Classification | ContextForge contract and v0.4 boundary |
| --- | --- | --- |
| Repository maps | **Implemented in v0.4.0** | Verified graph queries plus evidence-backed architecture and feature interpretations. Maps disclose coverage and never replace source. |
| CodeMaps | **Implemented in v0.4.0** | Deterministic Python symbols, signatures, ranges, imports, exports, detectable calls/references, and test links; honest fallback elsewhere. |
| File selection | **Already implemented** | Manual exact, directory, glob, exclusion, and line-range selection remains intact; v0.4 adds reviewable model proposals without weakening it. |
| Context budgeting | **Implemented in v0.4.0** | File/byte, discovery-step, provider-call, source-read, result, wall-time, and prompt-category byte budgets are enforced. Exact tokenizer accounting remains deferred. |
| Git diff context | **Implemented in v0.4.0** | Optional bounded working/staged/base diff from a fixed read-only Git adapter, separate from `ContextPackage` v1. |
| Reviewable handoff packages | **Implemented in v0.4.0** | Portable `TaskHandoff` adds reasons, evidence, confidence, pinned state, warnings, budget, Git context, and index/model provenance around unchanged package schema 1. |
| Model-guided context building | **Implemented in v0.4.0** | Indexed, fresh, and default hybrid discovery use bounded read-only tools, completeness checks, reviewer materialization, and prompt compilation. |
| CLI integration | **Implemented in v0.4.0** | `index`, `context suggest/create/review`, and `mcp serve` are thin adapters; prior scan/tree/manual context/version/doctor commands remain. |
| MCP integration | **Implemented in v0.4.0** | Local stdio exposes bounded read-only overview, tree, search, summaries, relationships, verified reads, diff, suggestion, in-memory package building, and portable inspection. No sampling/write/remote transport. |
| Multi-root workspaces | **Deferred to a later milestone** | v0.4 pins one root per index, session, handoff, and MCP server. Root namespaces need separate design. |
| External coding-agent integration | **Implemented in v0.4.0** | External clients can consume compiled prompts, JSON handoffs, and read-only MCP. ContextForge does not launch, steer, orchestrate, or grant writes. |
| Agent orchestration | **Deferred to a later milestone** | One discovery model using bounded tools is not multi-agent lifecycle management. |
| Workspace UI | **Deferred to a later milestone** | Review is CLI/JSON/Markdown. A native/web IDE requires separate interaction, security, and multi-root design. |

## Deliberately narrower choices

- Autonomous source editing is **Intentionally not planned** for ContextForge's
  core. External tools may edit after consuming a handoff under their own
  authority.
- Git worktree management is **Intentionally not planned** for v0.4 and has no
  approved later milestone.
- Embeddings as the only retrieval mechanism are **Intentionally not planned**.
  Future embeddings may supplement, never replace, complete allowed-tree/text
  access and verified facts.
- A proprietary model runtime is **Intentionally not planned**. Providers stay
  replaceable and structural indexing remains model-free.
- Complete dynamic-language call-graph accuracy is **Intentionally not
  planned** because static extraction cannot honestly guarantee it.

## v0.4 parity statement

v0.4 implements feature-direction parity in repository orientation, bounded
curation, reviewable handoff, Git awareness, and read-only external-client
access. It stops short of exact tokenizer budgeting, full multi-root
workspaces, coding-agent orchestration, worktree management, autonomous edits,
and a graphical workspace UI. The release is a repository-intelligence and
context-builder foundation, not complete RepoPrompt parity.

## Revisit criteria

- Multi-root: real projects requiring cross-root symbol IDs and authorization.
- Coding-agent orchestration: explicitly deferred; read-only discovery is not
  authorization for external writes.
- Workspace UI: repeated reviews that CLI/JSON/Markdown cannot safely serve.
- Broader CodeMaps: demonstrated language demand plus deterministic fixtures
  and acceptable dependency/security cost.
