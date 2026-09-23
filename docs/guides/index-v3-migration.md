# Migrating to Index v3.1 and Context Capsule v2

## Index rebuild

Index schemas 1–3 are not used by v3.1 retrieval. `contextforge index status PATH`
reports `rebuild_required` for them. `contextforge index update PATH` rejects them;
run a full build instead:

```bash
contextforge index build PATH --provider none
# or full callable enrichment
contextforge index build PATH --semantic-scope all
```

The build creates a new immutable schema 4 generation. It does not migrate old
semantics and does not delete old generations. Only explicit
`contextforge index clean PATH` removes generated index data.

The structural generation becomes active before model enrichment. If
enrichment fails, status may be partial but `map`, deterministic search, symbol
lookup, and compilation against structural evidence remain usable.

Resolver/analyzer identities changed with this revision (fallback 4, resolver
9, Python 6, polyglot 10, Semantic Card analyzer 8, prompt
`semantic-card-v3.5`). Rebuild/update does not reuse older CodeMaps or semantic
cache entries under those contracts. Polyglot analysis now includes Kotlin
`.kt` and `.kts` declarations, imports, calls, and references.

Relative TypeScript imports that intentionally use emitted `.js`, `.jsx`,
`.mjs`, or `.cjs` suffixes can resolve to the corresponding source `.ts`,
`.tsx`, `.mts`, or `.cts` through a closed substitution table. Exact source
paths still win, ambiguity remains unresolved, package imports are external,
and `tsconfig` aliases are not executed. Shared file policy now identifies
polyglot tests and derives source-test links from resolved imports, calls, and
references; those links do not affect centrality.

A no-op update reuses unchanged records and keeps the active generation ID.
Unchanged parse-error records are also reused as failed/partial evidence;
`--force-reanalyze` explicitly retries their extraction. For ordinary no-op
source identity, this force applies to semantic analysis/cache reuse only and
does not force CodeMap extraction. A corrupt active
generation is reported as `corrupt` with `rebuild_required` rather than making
`index status` crash. Recover it with `index build`; old immutable generations
remain until explicit `index clean`.

## CLI behavior

With an active v3 index:

- `context suggest --task ...` returns RetrievalResult/CandidateCards v3;
- task-based `context create --task ...` returns Context Capsule v2;
- manual `context create` without a task remains ContextPackage v1; and
- `context inspect`/`context review` accept both artifact generations.

`contextforge map PATH` remains the orientation-map default. Use `--kind
architecture`, `--kind conventions`, `--kind features`, or `--kind all` for
the enriched typed maps. JSON for the default stays the raw OrientationMap;
`--kind all --format json` returns `orientation` plus `repository_maps`.

Use `--legacy-discovery` and `--legacy-handoff` during migration if a consumer
still expects `FinalContextSelection` or `TaskHandoff`. Remove those flags after
the consumer accepts CandidateCards and Capsule v2.

## Budget migration

Capsule creation requires an explicit or configured model context window.
Account for prior conversation and expected response separately:

```bash
contextforge context create PATH --task "..." \
  --context-tokens 32768 \
  --history-tokens 6000 \
  --response-tokens 4096 \
  --safety-margin-tokens 1024
```

`--working-file` influences retrieval and reserves Working Set context.
`--working-lines PATH:START-END` requests exact source ranges. `--full-file`
allows FULL for an explicitly pinned file; otherwise automatic FULL applies
only to files of at most 200 lines and only when it fits.

A 30% automatic task-evidence allocation is a soft ceiling, not a promised
token saving. The compiler first preserves mandatory role and graph-endpoint
coverage, then distinct concepts/ranges, then upgrades. Automatic task evidence
has a soft ceiling of 30% of the available budget and
stops when its Evidence Plan is covered; it is not padded to that size.
Explicit Working Set material, requested ranges, pinned FULL files, and required
Git material may exceed the soft ceiling but never the hard budget. Planner
representation suggestions are advisory and cannot bypass those rules.

Model-assisted planning may use up to three bounded `search`, `symbol`, `graph`,
`map`, or multilingual `expand_query` rounds before finalization.
`expand_query` can submit at most eight short expressions from supplied
repository vocabulary; expressions are search interpretation, not source facts.
`--planning-rounds` may lower that ceiling.
The candidate pool and every returned ID remain ContextForge-controlled. In
`auto`, one invalid or unmaterializable planned item causes the entire plan to
fall back to deterministic complementary selection. In `required`, the same
condition is a typed error. Bridge 2.1 continues to map its boolean `rerank`
field to this behavior; Bridge 2.2 exposes `planning_mode` directly.

The plan's `sufficiency` is declared before compilation. Read the additive
`CompilationSufficiency.effective_status` and evidence-coverage diagnostics
after materialization: a missing planned item, range, or mandatory role, and an
empty task context, must yield `insufficient`. A one-short-file exact retrieval
may use the additive Capsule v2 `compact_profile` only when it is cheaper than
the ordinary envelope and retains verification rules.

Package, Capsule, and prompt files created inside the repository are recorded
in `.contextforge/generated-artifacts.json`. An unchanged registered artifact
is omitted from later scans and updates; editing it changes the digest and makes
it ordinary source again. No ignore-file migration is required.
Concurrent registry writers are serialized by the internal bounded
`.contextforge/generated-artifacts.lock`; stale ownership is recovered safely.

## Integration migration

Bridge clients may keep negotiating 1.0, 1.1, or 2.0 unchanged. Negotiate 2.1
to use `map`, `search`, `symbol`, and `compile`, semantic scheduler options, and
`operation_timeout`. Continue correlating a timed-out index job by its
`operation_id`; client `timeout_ms` no longer cancels that job.

MCP clients should refresh `tools/list` and accept the four new read-only tools.
Development HTTP clients may use `/v1/map`, `/v1/search`, `/v1/symbol`, and
`/v1/compile`. No new interface grants source-write, shell, or Git-mutation
authority.

Bridge 2.2 remains additive: it exposes planning controls plus coverage and
effective-sufficiency diagnostics in compatible result fields. A DSH-specific
integration is intentionally out of scope for this migration; track it as a
separate future Bridge 2.2 migration task rather than changing DSH behavior
here.

Python callers can migrate incrementally:

```python
from contextforge import (
    ContextBudget,
    compile_context_capsule,
    load_orientation_map,
    retrieve_context_candidates,
)


async def build_capsule(root, task):
    retrieval = await retrieve_context_candidates(root, task)
    return compile_context_capsule(
        root,
        task,
        retrieval,
        budget=ContextBudget(
            context_window_tokens=32768,
            history_tokens=6000,
            response_tokens=4096,
            safety_margin_tokens=1024,
        ),
    )
```

Legacy `ContextPackage`, `TaskHandoff`, and `compile_prompt` APIs remain
available for the deprecation period.

Benchmark manifests remain schema 1. The additive task field `pipeline`
defaults to `legacy_discovery`; set it to `index_v3_capsule` to measure cold
build, warm retrieval, and isolated incremental update through Capsule v2.
For paired answer evaluation, complete required/working files form the ordinary
token baseline; manual oracle ranges are only the quality reference. Three
blinded groundedness votes validate ContextForge answers against materialized
ranges, and phase-specific model tokens, HTTP calls, and latency stay separate.
Candidate recall is retrieval-pool coverage; materialized recall is delivery to
the answer model. Citation containment only validates citation location;
assertion evidence support, lexical/identifier support, and blinded semantic
grounding are separate measures. Report token savings in the headline only when
the quality gate passes (file recall >=0.90, range recall >=0.85, citation
validity 1.0, and quality not below oracle); otherwise retain
`quality_gate_failed` and exclude savings.
