# Remaining retrieval and indexing defects, 2026-09-04

Scope: local `waterflane/polyglot-symbol-index` only. No new branch, push,
publication, merge into `dev`, or changes to `dsh-contextforge` / Harness.

## Implemented

- Fresh discovery uses candidate-ID selection for conservative, evidence-complete
  definition questions. Ambiguous identifiers, relationship questions and missing
  evidence retain investigative tools. Source ranges, SHA and read authority
  remain server-owned. Pins, exclusions, stale-source checks, budget enforcement,
  explicit fallback provenance and weak-relevance warnings remain in effect.
- Accumulated observations are dropped oldest-first as whole records until the
  request fits. Candidate evidence and current selection remain separate.
- Explicit provider context refusals are typed and never consume JSON repair.
  An unambiguous smaller limit lowers only the provider instance's working window;
  callers rebuild once. Architectural metadata cannot enlarge an explicit window.
  Existing reasoning-off, capability fallback and truncation handling remain.
- Polyglot extraction covers named JS/TS callable bindings without duplicate
  variables, common variables/constants, Go receiver and Rust impl ownership,
  C++ methods and user-defined return types. Anonymous types/expressions do not
  acquire invented verified names. Independent nodes survive parser errors.
- Deterministic source regions cover declarations and gaps, with 64 KiB UTF-8
  chunks, up to eight overlap lines, byte-column splits for oversized lines,
  and at most 64 chunks. Context preflight can reduce chunk size further.
- Each chunk selects rich or inferred-region analysis by actual declarations.
  Successful chunks are cached with source/range/schema/analyzer/options identity,
  including inside published partial records for cross-run resume. Aggregation
  needs no synthesis request and retains alternate interpretations and provenance.
  Invalid inferred evidence is rejected; conflicting overlapping regions retain
  the first valid result with a warning. Concurrent source edits reject publication.
- Index, manifest/record, CodeMap and persisted semantic schemas are v2. Legacy
  v1 has a separate inspection loader, is stale and is never reused in v2 builds.
  Publication validates a coherent immutable generation and switches the active
  pointer only at transaction success. Failed migration leaves v1 available.
  Provider response DTO versions are independent of persisted schema versions.
- Bridge 1.1 reports partial files and planned/completed semantic chunks; Bridge
  1.0 retains its response shape. Internal chunk checkpoints do not enter discovery
  tool summaries. Existing expand/read/package and capsule boundary tests remain.

## Live Qwen validation

Endpoint: `http://127.0.0.1:1919/v1`; model:
`Qwen3.6-35B-A3B-NVFP4`; configured context 8,192; reasoning `off`.

The strengthened read-only probe made five sequential English and five Russian
fresh-discovery requests about `preparationProgressStage`, followed by answers
from the verified source excerpt. All ten passed:

- Candidate 1: `src/progress.ts`; selected lines 148–158, 596 UTF-8 bytes.
- Selection provenance: model; zero discovery repair attempts; no fallback.
- Answer assertions: all five index failure kinds, other failures, `cancelled`,
  `INDEX_PHASES` membership and the default `context` branch were correct.

The provider rejects constrained structured decoding; its supported plain-JSON
fallback with local validation was used. An earlier free-explanation series also
retrieved correctly 10/10, but one terse explanation omitted `cancelled`. The
probe was strengthened to require explicit branch results, rather than treating
successful retrieval alone as a correct answer. Some Russian-query explanations
were in English; this run does not establish language-following quality.

Reproduce (read-only against the supplied repository):

```powershell
$env:PYTHONPATH = "$PWD/src"
.venv/Scripts/python.exe scripts/verify_qwen_retrieval.py C:/Programming/Projects/dsh-contextforge
```

## Validation

- Full frozen pytest: **1,261 passed, 8 skipped**, branch-aware coverage **90.15%**
  (required: 90%), 137.14 seconds. One existing Starlette/httpx deprecation warning.
- Ruff: all checks passed. Mypy: no issues in 149 source/test/probe files.
- Wheel/sdist build, archive validation and installed-wheel CLI/Bridge smoke checks
  passed. Both Bridge 1.0 and 1.1 completed hello/status/shutdown over stdio;
  coverage fields appeared only in 1.1.
- `git diff --check`: clean. All work remains on the requested local branch.

## Boundaries and external follow-up

- The structural reader has an explicit 16 MiB source cap. Semantic work stops
  after 64 chunks and reports incomplete coverage; lowering the caller's limits
  also remains explicit. Unsupported languages use inferred regions, not fabricated
  verified symbols. No model leaves deterministic source regions and disabled
  semantic status.
- Compact exact-definition success is not proof that Qwen reliably plans every
  open-ended tool investigation. Investigative failure remains explicitly labelled;
  unsupported structured-decoding capability is still provider-dependent.
- `dsh-contextforge`: populate nonempty summaries, remove alphabetical fallback,
  truncate capsules at complete record/source boundaries, and adopt Bridge 1.1
  expansion candidates and coverage reporting.
- Harness/UI: keep permissions and policy metadata out of user answers. Neither
  external follow-up repository was changed here.
