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
- **Planned for v0.4.0**
- **Deferred to a later milestone**
- **Intentionally not planned**

## Feature matrix

| Feature | Classification | ContextForge contract and v0.4 boundary |
| --- | --- | --- |
| Repository maps | **Planned for v0.4.0** | Verified graph queries plus evidence-backed architecture and feature interpretations. Maps disclose coverage and never replace source. |
| CodeMaps | **Planned for v0.4.0** | Deterministic Python symbols, signatures, ranges, imports, exports, detectable calls/references, and test links; honest fallback elsewhere. |
| File selection | **Already implemented** | v0.3 supports exact, directory, glob, exclusion, and line-range selection over `ProjectSnapshot`. v0.4 adds reviewable model proposals without weakening manual selection. |
| Context budgeting | **Planned for v0.4.0** | v0.3 has file/byte limits. v0.4 adds discovery, tool, wall-time, provider-call, read-byte, and conservative token budgets. |
| Git diff context | **Planned for v0.4.0** | Optional bounded working/staged/base diff from a fixed read-only Git adapter, separate from `ContextPackage` v1. |
| Reviewable handoff packages | **Already implemented** | v0.3 Markdown/JSON `ContextPackage` is portable/reviewable. v0.4 adds a wrapper for reasons, evidence, confidence, unknowns, and diff metadata. |
| Model-guided context building | **Planned for v0.4.0** | Indexed, fresh, and default hybrid discovery use bounded read-only tools, completeness checks, and reviewer materialization. |
| CLI integration | **Planned for v0.4.0** | Existing commands remain; new intelligence, discovery, prompt, and MCP groups are thin adapters. |
| MCP integration | **Planned for v0.4.0** | Local stdio exposes read-only overview, maps, search, relationships, verified reads, and bounded diff. No sampling/write/remote transport. |
| Multi-root workspaces | **Deferred to a later milestone** | v0.4 pins one root per index, session, handoff, and MCP server. Root namespaces need separate design. |
| External coding-agent integration | **Planned for v0.4.0** | Agents consume compiled prompts, JSON handoffs, and read-only MCP. ContextForge does not launch, steer, or grant writes. |
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

v0.4 targets feature-direction parity in repository orientation, token-aware
curation, reviewable handoff, Git awareness, and read-only external-agent
access. It stops short of RepoPrompt's documented native workspace and
multi-agent orchestration surfaces. The release should be described as
“repository-intelligence and context-builder foundations,” not “complete
RepoPrompt parity.”

## Revisit criteria

- Multi-root: real projects requiring cross-root symbol IDs and authorization.
- Agent orchestration: a stable single-agent discovery contract and explicit
  authority model for external writes.
- Workspace UI: repeated reviews that CLI/JSON/Markdown cannot safely serve.
- Broader CodeMaps: demonstrated language demand plus deterministic fixtures
  and acceptable dependency/security cost.
