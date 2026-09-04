# Retrieval and Qwen review, 2026-09-04

Historical review. The remaining findings below are addressed by the subsequent
[implementation and validation report](2026-09-04-remaining-defects.md); the
original observations are retained here unchanged.

Scope: local `waterflane/polyglot-symbol-index`, plus read-only checks against
`dsh-contextforge` and the user-provided loopback provider. No external repository
was edited and no Git refs were pushed.

## Fixed in this review

- Exact identifier scanning now uses the shared bounded, path-safe snapshot
  reader. Content changed since the snapshot cannot supply ranking evidence.
- Exact verified declarations remain line-range selections through model selection
  and deterministic fallback. Exact usages use bounded surrounding lines rather
  than entire caller files. Manual full-file pins remain full-file pins.
- Discovery requests allow the existing one-shot truncation retry to increase
  output from 512 to 1,024 tokens, subject to the provider context budget.
- Candidate-ID selection accepts a bounded optional informational summary while
  retaining closed validation for unknown action arguments.
- Polyglot extraction distinguishes public visibility from explicit exports,
  recognizes common constructor forms, and retains async method metadata.
  Modifiers are inspected only in the declaration header, not in nested bodies.
  Analyzer version 2 invalidates records with the old metadata semantics.

## Live checks

The endpoint `http://127.0.0.1:1919/v1` served `Qwen3.6-35B-A3B-NVFP4`.

- The server rejected constrained JSON Schema/JSON object decoding. ContextForge
  fell back to plain JSON with local schema validation, as designed.
- A compact gateway probe returned valid JSON with `reasoning_effort=off`, 18
  completion tokens, and no model-assisted repair.
- Real-source retrieval placed `src/progress.ts` first and selected lines 148-158
  (596 UTF-8 bytes) for `preparationProgressStage`.
- Qwen explained the failed/cancelled/INDEX_PHASES branches correctly from that
  excerpt: 509 input tokens, 131 output tokens, 9,390 ms, no repair.
- A model-guided Russian discovery completed with model provenance after two
  repairs, but initially included whole caller files (60,288 context bytes).
- After usage-range selection, the English discovery selected the same three
  paths in only 2,352 context bytes. Its action response still failed validation
  after two repairs; the result explicitly reported deterministic fallback.
  These runs are functional observations, not a controlled latency benchmark.

## Remaining findings

1. **P1: fresh action generation is not reliable on this provider.** Valid answers
   from a source excerpt do not establish reliable tool planning. Live runs
   produced invalid action arguments and exhausted repairs. Keep fallback
   provenance visible; evaluate a compact candidate-selection contract for
   evidence-complete requests and bound accumulated tool observations separately.
2. **P1: large-file semantic coverage remains incomplete.**
   `SemanticAnalysisOptions.max_chunks_per_file` exists, but analysis still uses
   one bounded excerpt rather than 64 symbol-aligned chunks with overlap. The
   index/codemap/semantic schema-v2 migration from the original plan is also not
   implemented. Analyzer-version invalidation is not that migration.
3. **P2: declaration-free supported languages miss inferred-region analysis.**
   `_semantic_route` selects rich analysis by language alone. Common forms absent
   from the current symbol mappings, such as JavaScript arrow-function bindings,
   can have no verified symbols yet never enter the generic region-producing
   schema. A follow-up should route based on verified declaration coverage and
   preserve the verified/inferred distinction.
4. **Provider configuration caveat:** `/models` advertised a 262,144-token context,
   while a live request was rejected with an effective prompt-plus-generation
   limit of 8,264. Later probes used `context_window=8192`. Do not equate the
   model's architectural maximum with the server's allocated KV-cache capacity.

## Validation

- Full pytest: 1,219 passed, 8 skipped; coverage 90.03%.
- Ruff: all checks passed.
- Mypy: no issues in 144 source/test files.
- Wheel and source distribution built successfully.
- `git diff --check`: clean.

One existing Starlette/httpx deprecation warning remains unrelated to these changes.
