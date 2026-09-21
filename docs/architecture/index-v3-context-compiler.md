# Index v3 retrieval and Context Capsule compiler

The current repository-context pipeline is:

```text
Source Identity
→ deterministic CodeMaps
→ relationship graph
→ sparse grounded Semantic Cards
→ repository maps
→ BM25/graph retrieval
→ MAP/SUMMARY/SLICE/FULL compiler
→ Context Capsule v2
```

## Two-stage publication

One index job holds the writer lock through both transactions. The first
transaction atomically publishes CodeMaps, the relationship graph, structural
retrieval postings, and orientation map. The second transaction publishes an
enriched generation with Semantic Cards and derived repository maps. Failure,
operation timeout, or cancellation during enrichment leaves the structural
generation active and usable.

`timeout_ms` limits how long a Bridge client waits for its response;
`request_timeout` limits one provider attempt; `operation_timeout` limits the
whole index job. A Bridge client timeout does not cancel the tracked job or
release its writer lock. Explicit cancellation and shutdown do cancel it.

Index, manifest, and record schemas are version 3. A v2 index is readable only
for status/migration diagnostics, reports `rebuild_required`, is excluded from
retrieval, and is rejected by `index update`. Run `index build` to create v3;
semantic records are not migrated. Immutable generations are removed only by
explicit `index clean`.

Fallback analyzer version 4, resolver version 9, Python analyzer version 6,
and polyglot analyzer version 10 extract imports, calls, and non-call references
for Python plus JavaScript, TypeScript, Java, Kotlin, C#, Go, Rust, C, C++, PHP,
and Ruby. Exact relative paths and
unambiguous snapshot symbols are verified; package/convention resolution is
best-effort and ambiguity stays unresolved. Config consumers match SHA-256
digests of discovered key names in permitted root/module scope. Config values
are never stored.

Semantic Card analyzer version 7 (`semantic-card-v3.4`) uses one full request
when it fits or up to four declaration-aware UTF-8 chunks with eight lines of
overlap. Evidence is constrained to the active chunk and may address verified
import, call, reference, or config facts. Every primary and scheduler-owned
repair consumes the shared request and full-request token ceilings; provider
internal repair is disabled for card requests. Ranking prose must also have a
lexical or identifier anchor in cited evidence.

Graph nodes, edges, metrics, and file projections are stored in digest-bound
shards of at most 4 MiB. Retrieval documents use the same bounded shard format.
The public graph loader still reconstructs the complete `RelationshipGraph`,
while the warm query path reads only the compact file projection and retrieval
documents. Grounded synopsis, concepts, and evidence are copied into the
digest-bound retrieval generation, so a query does not reopen every CodeMap or
Semantic Card.

## Retrieval and evidence planning

Retrieval first partitions exact matches in this order: exact path, qualified
symbol, symbol, and source identifier. Approximate candidates use BM25 with
`k1=1.2`, `b=0.75`, and weights path `4`, symbols `3`, source identifiers `2`,
grounded semantics `2`. It then applies graph proximity (`+0.20` one hop,
`+0.08` two hops), normalized centrality (up to `+0.10`), current diff
(`+0.25`), and Working Set (`+1.0`). Exact groups always precede approximate
scores.

Current-diff and Working Set membership are preserved as CandidateCard
provenance. Centrality is only a bounded ordering signal: a candidate is
eligible for automatic materialization only when it also has an exact group,
BM25 or grounded semantic/evidence match, graph proximity, current-diff, or
Working Set signal. This prevents unrelated metadata, lock, or generated files
from entering a capsule merely because they are structurally central.
Only `verified` and `best-effort-structural` edges contribute graph proximity.
`model-inferred` neighbors remain visible with provenance in CandidateCards but
cannot be the sole relevance signal.

A `CandidateCard` includes source identity, synopsis, matched concepts and
symbols, evidence ranges, graph neighbors, provenance, freshness, and only the
MAP/SUMMARY/SLICE/FULL representations that can actually be materialized.
Default discovery performs no provider call. `ContextPlanningMode` is `off`,
`auto`, or `required`; the default project configuration is `auto`.

The configured provider may use at most three rounds and four closed actions
per round: `search(query)`, `symbol(identifier)`, `graph(candidate_id)`, and
`map(module_id)`, followed by `finalize`. Each action can only expand the
verified pool; the model cannot create paths, symbols, ranges, evidence IDs, or
source claims. The pool is capped at 64 candidates, each request at 8192 input
and 768 output tokens, and the session at 24,576 input and 2304 output tokens.
Unsupported `json_schema`, malformed JSON, and repair consume the same three
HTTP-call ceiling. Capability caching avoids retrying a structured mode already
known to be unsupported.

The prompt exposes source-spanning representative evidence first, then compact
previews of remaining evidence, so one large declaration cannot hide later
facts. A final plan may select at most 8 files and 8 evidence ranges per file.
Every ID, source identity, relevance signal, representation, and range is
validated locally. `auto` replaces any materially invalid or incompletely
materializable plan as a whole with deterministic complementary selection;
`required` raises a typed planning error. Diagnostics record actual rounds,
HTTP calls, token counts, and fallback reason. Legacy `rerank=true/false`
remains a compatibility alias.

The normative wire schema is
[`retrieval-result-v3.schema.json`](../schemas/retrieval-result-v3.schema.json).

## Context compiler

The compiler receives a task, pinned generation, Working Set, retrieval
evidence, optional Git diff, and a `ContextBudget`. Available tokens equal the
model context window minus caller-supplied history, response limit, and safety
margin. Initial shares are 20% orientation, 15% Working Set, 55% task evidence,
and 10% diff/metadata; unused space flows to task evidence, then Working Set,
then map.

For a fully automatic capsule the compiler treats 30% of available tokens as a
soft ceiling, not a fill target. It stops as soon as the validated plan is
covered and no candidate adds a symbol, concept, source range, graph flow, or
task role. Explicit Working Set files, requested ranges, pinned FULL files,
and required Git material may take the capsule beyond that target, while the
hard available-token budget remains absolute. Unused allocation still flows to
evidence, then Working Set, then the repository map.
For automatic compilation the 20/15/55/10 shares are calculated inside the
soft payload after the capsule envelope. Relevant complementary MAPs are seeded
before any representation upgrade. Upgrade utility is recomputed against the
selected set so repeated concepts, ranges, and graph neighbors lose value; a
single cheap FULL cannot replace several complementary MAPs.

Representations are:

- `MAP`: verified signatures and structure;
- `SUMMARY`: grounded Semantic Card content only;
- `SLICE`: evidence with five context lines, expanded to intersecting
  declarations and merged when gaps are at most three lines; and
- `FULL`: only an explicitly pinned full file, or a file of at most 200 lines
  when the upgrade fits.

Upgrades are greedy by marginal utility per token, considering relevance,
evidence strength, facet coverage, graph utility, and duplicate
ranges/concepts. Source SHA is rechecked before materialization. No source range
or section is cut mid-way to satisfy a budget.

Validated planned items are materialized in model order and bypass the
deterministic duplicate-role filter. An unavailable representation is downgraded
only through `FULL → SLICE → MAP`; automatic FULL is never advertised for a
file over 200 lines. If any planned item is stale, missing, over the soft
ceiling after downgrade, or cannot be materialized, the complete model plan is
discarded and deterministic complementary selection is used. A plan cannot
remain `sufficient` after losing an item, and a non-empty retrieval result
cannot silently compile to empty task material.

The stable prompt root is `<contextforge schema_version="2">` with separate
snapshot, verified repository map, Working Set, task context, and Git sections.
Model selection rationale is labeled interpretation and never merged into
verified source. A stable verified-usage section says that source facts are
evidence, repository maps establish structure rather than source contents,
only materialized SLICE/FULL lines may be quoted, summaries are evidence-linked
interpretations rather than guarantees, and unknown behavior must be reported
as unknown. See
[`context-capsule-v2.schema.json`](../schemas/context-capsule-v2.schema.json).

## Storage and benchmark concurrency

Package, Capsule, and prompt outputs written inside the repository are recorded
by digest. Registry read-modify-write is serialized by a separate bounded
`.contextforge/generated-artifacts.lock`; owner identity is checked on release
and stale locks are recovered without deleting a replacement lock. Scanner
suppression remains digest-bound, so a user edit makes the file ordinary source.

Benchmark manifest schema 1 has an additive `pipeline` field. Its default is
`legacy_discovery`; `index_v3_capsule` measures cold build/retrieve/compile,
warm retrieve/compile, and isolated incremental update/retrieve/compile for
fresh/indexed/hybrid modes. Results account for materialized ranges/tokens,
grounded and dropped card claims, and provider-reported transport/HTTP calls.
Optional `answer_assertions` plus real `oracle_ranges` run a paired downstream
answer regression with the same provider settings. The ordinary-client
baseline contains the complete required and Working Set files and is used for
token-efficiency comparison. The manually selected oracle ranges are used only
as the answer-quality reference. ContextForge and oracle answers share the same
task, response schema, temperature, and provider. Each answer returns assertion
IDs and citations; citations must fit inside an actual materialized Capsule or
oracle source range.

Three blinded groundedness-judge calls compare the ContextForge answer only
with its materialized source and use majority agreement. The report separates
offline indexing, evidence planning, final-answer, and judge tokens, latency,
logical generations, and HTTP calls. It records both conservative estimated
input and provider-reported input instead of hiding provider truncation or
repair traffic.

## Public surface

Python exports `load_relationship_graph()`, `load_orientation_map()`,
`retrieve_context_candidates()`, and `compile_context_capsule()` plus the
public card/candidate/capsule/budget/estimator types. Bridge 2.1, MCP, and the
development HTTP API expose read-only `map`, `search`, `symbol`, and `compile`
operations. Bridge 2.2 adds explicit `planning_mode` and advertises the Evidence
Plan schema; Bridge 2.1 keeps the unchanged `rerank` wire field. None can write
source, invoke a shell, or mutate Git.
`contextforge map --kind orientation|architecture|conventions|features|all`
exposes the same pinned artifacts. Normative schemas are
[`orientation-map-v3.schema.json`](../schemas/orientation-map-v3.schema.json)
and [`repository-map-v3.schema.json`](../schemas/repository-map-v3.schema.json).
