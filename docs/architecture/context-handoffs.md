# Context handoffs and prompt compilation

## Implemented boundary

`contextforge.handoff` connects an accepted model-guided discovery selection to
the existing verified `ContextPackage` builder and then compiles a portable
prompt. Thin `context create` and `context review` commands expose this flow,
and the read-only MCP adapter can build or inspect a package in memory. Neither
surface invokes a coding agent or modifies repository source.

The flow is:

```text
original task
  -> indexed, fresh, or hybrid discovery
  -> validated ContextSelectionReview
  -> new repository scan and current source identity checks
  -> verified ContextPackage schema 1
  -> TaskHandoff schema 1
  -> deterministic CompiledPrompt schema 1
```

Discovery summaries, semantic analysis, architecture maps, and feature maps may
guide selection. They never replace required source. Every source file or range
in the final package is reopened through the identity-checked context reader
after the review checkpoint. A repository digest, path hash, line-bound, decode,
link, size, or read failure aborts the operation without returning a partial
package or handoff.

`discover_context_handoff()` exposes the complete in-process flow and returns a
`DiscoveryHandoffResult` pairing the discovery audit record with the verified
handoff. The lower-level `prepare_context_review()` and
`create_task_handoff()` functions remain available when a caller needs a human
approval or selector-override pause between discovery and materialization.

## Schema compatibility

`ContextPackage` remains the closed provider-neutral schema version 1. Its JSON
shape is unchanged. Review, Git, refinement, handoff, and compiled-prompt data
use separate closed schema-version-1 artifacts:

- `ContextSelectionReview` is the approval checkpoint;
- `GitDiffContext` is optional read-only repository state;
- `TaskRefinement` is optional labelled model output with provenance;
- `TaskHandoff` combines the approved review with a verified package; and
- `CompiledPrompt` contains `PromptPackage` text and deterministic metadata.

There is therefore no implicit `ContextPackage` migration. A future shape
change to any closed artifact must increment that artifact's independent schema
version and use explicit reader dispatch.

## Review checkpoint

`prepare_context_review()` verifies the discovery snapshot identity, selected
paths, source hashes, and ranges. Each `ReviewSelectionItem` records:

- portable path and current source hash;
- full source, source ranges, CodeMap-only, or omitted representation;
- selection reason and confidence;
- estimated raw and included bytes;
- pinned versus automatic provenance; and
- primary, supporting, test, or structural category.

The review also retains the original task verbatim, optional generated
refinement, acceptance criteria, discovery mode/run/index identity, completeness
warnings, and separate budget estimates.

Reviewers can supply `SelectionOverride`, which wraps the existing
`ContextSelection` exact-path, directory, glob, exclusion, and line-range
mechanisms. Overrides can merge with discovery or replace it. The result passes
the same snapshot, range, source, and budget validation before rendering.

## Materialization and identity

`create_task_handoff()` always performs a new scan, even when its caller has an
older `ProjectSnapshot`. The scan digest must equal the approved review digest,
and every reviewed path must retain its SHA-256 identity. The function converts
only approved source representations to `ContextSelection` and delegates all
reads to `build_context_package()`.

Selected CodeMaps are freshly extracted against the same current snapshot.
Snapshot-bound architecture and feature interpretations are filtered to
relevant selected paths and clearly marked generated. Stale interpretations are
omitted with warnings. The handoff records a SHA-256 identity over canonical
portable `ContextPackage` JSON; it contains no absolute repository root.

## Task refinement

`refine_task()` is an optional closed-schema model pass. It receives the
original task and portable package metadata, but no repository source,
filesystem handle, Git command, credentials, or execution tools. It may return:

- an optional clarified task;
- proposed acceptance criteria;
- open questions;
- likely affected areas; and
- an explicit list of preserved user constraints.

The artifact always records `generated=true`, provider, model, refinement prompt
version, and source package identity. The original task remains a separate
authoritative field and prompt section. Malformed or open-shaped model output
raises a typed error and produces no refinement. A refinement targeting another
package identity cannot be materialized.

## Budgeting

`HandoffBudgetLimits` and `HandoffBudgetUsage` account separately for:

- verified source content;
- CodeMaps;
- architecture and feature notes;
- Git diff text;
- compiler instructions and framing; and
- total compiled prompt bytes.

Budget planning applies this priority order:

1. preserve manually pinned required files;
2. keep full primary implementation files;
3. keep valid selected ranges for supporting files;
4. retain lower-priority structural context as CodeMaps; and
5. report every omitted test or other material explicitly.

Pinned source may exceed a caller's preferred source budget, but never the
existing hard 10 MiB package ceiling. Missing tests, CodeMaps, architecture
notes, or diffs are warnings, not silent omissions. Byte limits are
authoritative. The compiler records `token_count=null` with
`not-calculated-no-tokenizer`; it does not claim an exact model token count.

## Git-aware context

`contextforge.git.collect_git_diff()` accepts only the closed `GitDiffRequest`
shape: `working`, `staged`, or `base`, an optional validated base revision,
portable snapshot paths, bounded context lines, bytes, and timeout. It builds a
fixed argument vector with pager, external diff, text conversion, terminal
prompting, and shell execution disabled. Models cannot supply command strings.

The result records diff mode, resolved base/head when available, SHA-256,
bounded UTF-8 text, changed-file summary, touched/deleted portable paths,
truncation, and generic diagnostics. Repositories without Git or without an
installed Git executable remain supported through an explicit unavailable
artifact and completeness warning.

## Compiled prompt structure

`compile_prompt()` is pure: it performs no model call, repository read, Git
operation, write, or execution. For identical handoffs it emits identical UTF-8
Markdown and metadata. Sections appear in this order:

1. Original task.
2. Refined task, only when model-generated.
3. Acceptance criteria.
4. Repository overview.
5. Relevant project tree.
6. Architecture and feature notes.
7. Selected CodeMaps.
8. Selected source files and line ranges.
9. Related tests.
10. Relevant Git diff.
11. Known constraints.
12. Completeness warnings.
13. Expected response or implementation format.

The original task and all source/diff payloads use collision-safe fences longer
than every backtick run in their content. Source blocks include portable path,
range, byte length, and SHA-256 metadata and are explicitly described as
untrusted data. Compiler metadata records prompt version, handoff identity,
prompt digest, byte count, and category accounting. The artifact is a reviewable
prompt only; it makes no claim of direct agent execution.
