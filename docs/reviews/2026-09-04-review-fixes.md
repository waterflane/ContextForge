# Eight review fixes, 2026-09-04

Scope: the existing `waterflane/polyglot-symbol-index` branch. The previous
`2cc569d` tip was restored after a local reset, preserving the five existing
commits. No push, PR, publication, or edits to `dsh-contextforge` were performed.

## Changes

- Polyglot analyzer 4 validates method ownership, includes callable fields,
  parenthesized functions, ambient declarations and prototypes, and fixes Rust
  generic impl/C# namespace ownership. Per-file extractor failures produce
  retryable diagnostics with no unreliable symbols. Source/transaction failures
  still abort. `parsed` describes syntax, not exhaustive language coverage.
- Semantic analyzer/prompt 5 plans against source, metadata, schema, context
  reserve and required-symbol response costs. Required IDs are never discarded.
  Declaration-aware splitting supports 101 functions in an 8192-token window;
  impossible budgets fail before dispatch. Checkpoints include final ranges,
  required IDs and planner version. Default output ceiling is now 1024 across
  API, project initialization and configuration; explicit limits remain binding.
- Discovery prioritizes verified declarations over text counts throughout
  ranking and thematic expansion. Optional symbol selections resolve and merge
  source ranges, including selections from earlier steps. Python scope analysis
  and JS/TS syntax references expose same-file dependencies and shadowing.
  Incomplete evidence retains tools, review opportunities and explicit unknowns.
- Missing exact names permit approximate selections with mandatory warnings and
  confidence capped at 0.35. Fallback/preselection use at most three unpinned
  alternatives. Unicode and qualified names retain case-sensitive exact matching.
  Optional path inventories can shrink to fit the model window.
- Relationship tools expose supported/partial/unsupported/unknown coverage,
  scope and limitations. TypeScript calls are not indexed. Empty results and zero
  unresolved counts describe only observed static facts; old analyzers do not
  acquire complete coverage retroactively.

## External checks

Read-only extraction against `dsh-contextforge` completed for all 53 JS/TS files,
including the five previously failing test files. Four additional `.mjs` files
are reported unsupported by the existing language classifier.

Live provider: `Qwen3.6-35B-A3B-NVFP4` at `127.0.0.1:1919/v1`, explicitly limited
to 8192 tokens, reasoning off. EN/RU explanation questions and EN/RU phase-list
questions all selected `src/progress.ts` with model provenance and zero discovery
JSON repairs. Each context included lines 148–158 and 276–282 (1105 UTF-8 bytes),
covering `preparationProgressStage` and `INDEX_PHASES`. Branch results and the full
literal phase list were checked against source. Qwen sometimes answered Russian
questions in English; the checks validate content, not response language.

The absent `preparationProgressStageV2` query completed with
`exact-identifier-not-found`, low relevance and unresolved-dependency warnings,
and confidence at most 0.35. It did not claim an exact implementation was found.
Semantic analysis of a temporary copy of `progress.ts` completed with full
coverage in eight requests, no failed paths or diagnostics. The provider's
unsupported structured-output mode used its existing JSON fallback.

Reproduce the live checks without writing an index into the source repository:

```powershell
$env:PYTHONPATH = "$PWD/src"
.venv/Scripts/python.exe scripts/verify_qwen_retrieval.py C:/Programming/Projects/dsh-contextforge --repeats 1 --semantic
```

## Validation

The acceptance run retains the repository's 90% branch-aware coverage gate.
Regressions cover owner validation, isolated extraction/retry, required IDs and
unattainable budgets, source coverage and resume, evidence priority, selected
symbol IDs/ranges, dependency scopes, missing/Unicode names and caller coverage.
The full suite includes CLI and Bridge protocol compatibility tests.

- Full frozen pytest: **1304 passed, 8 skipped**, zero failures/errors, **90.28%**
  branch-aware coverage (required 90%), 152.70 seconds. One existing
  Starlette/httpx deprecation warning.
- Ruff check and format check passed. Mypy passed for all 152 configured
  source/test files. CLI smoke returned `ContextForge 0.5.0`.
- `git diff --check` passed. Tests and live semantic indexing used temporary
  directories; the external source repository was read only.
