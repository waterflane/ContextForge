# Index v3.1 retrieval and Context Capsule compiler

`QueryIntent` records original explicit code anchors, evidence roles and topic
facets. Candidate `match_origin` separates explicit anchors from lexical discovery
while `exact_group` retains the observed match category. Only explicit anchors
receive guaranteed exact-group priority. Ordinary words matching symbols cannot
certify the task topic; role nouns remain topical unless instruction syntax
consumes them. Requirements and retrieval results carry the same immutable intent.

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

Index storage and manifest schemas are version 4. Index schemas 1–3 are readable
only for status/migration diagnostics, report `rebuild_required`, and are excluded
from retrieval. Run `index build` to create Index v3.1;
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

Semantic Card analyzer version 9 (`semantic-card-v3.6`) uses one full request
when it fits or up to four declaration-aware UTF-8 chunks with eight lines of
overlap. Evidence is constrained to the active chunk and may address verified
import, call, reference, or config facts. Every primary and scheduler-owned
repair consumes the shared request and full-request token ceilings; provider
internal repair is disabled for card requests. Ranking prose must also have a
lexical or identifier anchor in cited evidence.
Every indexed code/test file has source-bound search tags. Grounded model tags
have a dedicated BM25 field; if they are unavailable, structural tags preserve
coverage while the model analysis remains partial. Function descriptions and
direct-call expressions retain stable symbol/edge identities. Successfully
verified function groups survive a later failure and an update retries missing
IDs. Every function request carries the full file and outgoing call table;
Requested semantic coverage is reported independently of provider failures.
File/request/token limits, missing function/call IDs, validation failures, and
context overflow keep requested full enrichment partial. Updates also resume
missing call expressions without replacing verified function descriptions.
`file_only` omits external callee code if the request exceeds the actual window.

Graph nodes, edges, metrics, and file projections are stored in digest-bound
shards of at most 4 MiB. Retrieval documents use the same bounded shard format.
Internal file, graph, and retrieval records use bounded compression; manifests
still bind their decoded contents by digest and remain readable JSON.
The public graph loader still reconstructs the complete `RelationshipGraph`,
while the warm query path reads only the compact file projection and retrieval
documents. Grounded synopsis, concepts, and evidence are copied into the
digest-bound retrieval generation, so a query does not reopen every CodeMap or
Semantic Card. An enriched generation stores semantic fields as an overlay of
structural retrieval documents rather than duplicating structural postings. The
overlay is bound to the structural manifest digest; a corrupt overlay produces
`rebuild_required`, not a mixed index.

## Retrieval and evidence planning

An immutable EvidenceRequirements record carries task roles, source-bound
anchors, required evidence IDs, and the verified basis of graph endpoints
through retrieval, planning, and compilation. Selected representations cannot
change those requirements. Caller/callee bindings require directed verified
calls (or entrypoint-handler flows); imports alone do not prove calls. CodeMaps
supply configuration/public API/entrypoint facts, rather than arbitrary symbol
matches or words in filenames. General Russian task syntax uses the same closed
roles; domain vocabulary comes only from repository postings or grounded query
expansion. Retrieval build version 9 adds structural role facts to derived
documents; unchanged CodeMaps remain reusable.

Candidate diagnostics retain weighted BM25 field contributions and closed
selection reasons. Reviewed benchmark reports separately trace source evidence
through retrieval, planning, and materialization and classify pool, selection,
range, budget, and stale-source losses. These reviewed addresses never enter
answer requests. Planner request estimates and provider-reported input/output
usage are separate; unavailable reported usage is null rather than an estimate.

Retrieval first partitions exact matches in this order: exact path, qualified
symbol, symbol, and source identifier. Approximate candidates use BM25 with
`k1=1.2`, `b=0.75`, and weights path `4`, symbols `3`, source identifiers `2`,
model file tags `3`, grounded semantics `2`. It then applies graph proximity (`+0.20` one hop,
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

The configured provider may use at most three rounds and five closed actions
per round: `search(query)`, `symbol(identifier)`, `graph(candidate_id)`,
`map(module_id)`, and `expand_query(expressions)`, followed by `finalize`.
`expand_query` accepts at most eight short expressions from compact repository
vocabulary (modules, verified identifiers, grounded concepts, and task roles).
Expressions are interpretation, not source facts: they are used only as
exact/BM25/structural search input and their returned candidate IDs are
recorded in bounded diagnostics. This supports multilingual task phrasing
without a built-in domain dictionary or model-created path.

Each action can only expand the
verified pool; the model cannot create paths, symbols, ranges, evidence IDs, or
source claims. The pool is capped at 64 candidates, each request at 8192 input
and 768 output tokens, and the session at 24,576 input and 2304 output tokens.
Unsupported `json_schema`, malformed JSON, and repair consume the same three
HTTP-call ceiling. A bounded, credential-free process-local capability cache
avoids retrying a structured mode already known to be unsupported. An action
must add a role, identifier, evidence range, or graph endpoint; no-gain actions
cannot expand the session.

The prompt exposes source-spanning representative evidence first, then compact
previews of remaining evidence, so one large declaration cannot hide later
facts. A final plan may select at most 8 files and 8 evidence ranges per file.
Every ID, source identity, relevance signal, representation, and range is
validated locally. Task roles are closed general evidence requirements:
`entrypoint`, `implementation`, `caller`, `callee`, `configuration`, `test`,
`documentation`, `public_api`, `data_model`, and `unknown`. They are inferred
only from task syntax, exact identifiers, graph kinds, and file policy.
`CoverageLedger` contains canonical IDs for covered/missing roles, bindings,
symbols, concepts, ranges, and graph endpoints—never model prose. `auto`
replaces any materially invalid or incompletely materializable plan as a whole
with deterministic complementary selection; `required` raises a typed planning
error. Diagnostics record actual rounds, HTTP calls, token counts, query
expansions, action deltas, and fallback reason. Legacy `rerank=true/false`
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
soft payload after the capsule envelope. The first pass assigns the least-cost
MAP or SLICE that covers each mandatory role and graph endpoint; later passes
cover distinct concepts/ranges, then upgrade material. Upgrade utility is
recomputed against the ledger so repeated concepts, ranges, and graph neighbors
lose value, and an upgrade cannot displace final coverage for a role. A single
cheap FULL cannot replace several complementary MAPs.

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
discarded and deterministic complementary selection is used. After every
materialization, `CompilationSufficiency` compares planned and actual items,
evidence IDs, ranges, and role coverage. Its `effective_status` cannot be
`sufficient` if material is absent, a mandatory role is lost, or task context
is empty. This is distinct from the planner's declared status and carries only
bounded reason codes; it does not silently mutate the retrieval result.

For exactly one short exact file, an automatic compact profile can use a
minimal snapshot envelope and concise verified usage section instead of normal
orientation prose. It is chosen only when cheaper than the ordinary capsule and
when every verification rule is preserved. The schema stays v2;
`compact_profile` and evidence diagnostics are additive fields.

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
with cited material ranges and use majority agreement. Citation containment
means a citation fits materialized evidence; it does not establish that an
assertion follows. The benchmark separately records assertion-ID recall,
assertion evidence support, lexical/identifier support, and semantic grounding.
The report separates
offline indexing, evidence planning, final-answer, and judge tokens, latency,
logical generations, and HTTP calls. It records both conservative estimated
input and provider-reported input instead of hiding provider truncation or
repair traffic.

Candidate required-file recall is retrieval-pool coverage; materialized
required-file recall is the fraction actually delivered in the capsule. Token
savings count in a headline aggregate only when required-file recall is at
least 0.90, range recall at least 0.85, citation validity is 1.0, and answer
quality is not below the oracle. Failed quality gates are reported explicitly
and never turn missing evidence into a saving.

The pinned real-repository manifest uses schema 2: every required file has a
reviewed range, and every assertion binds to source ranges and CodeMap evidence
IDs. The built-in runner clones each pinned revision independently, records
structural and semantic build cost separately from warm retrieval, final answers,
and blinded judging, and requires three independent clones multiplied by three
model repetitions in each clone for a passing report. `repetition` remains the
clone alias for older reports; `clone_repetition` and `model_repetition` record
the separate coordinates. `--repetitions` selects clones and
`--model-repetitions` selects model repeats. Indexing occurs once per clone;
planner, paired answers and judges repeat independently. The runner atomically
checkpoints completed phases/tasks to `--output`, including failed request cost
and estimated/reported tokens, before final aggregation. It
also records function-description coverage, `file_only` use, no-op identity,
active index size, and fresh-process reload stability. Callback-based
observations remain useful for unit tests but cannot certify a live pass.
No-op duration is diagnostic, not a quality gate; a ContextForge final answer
over 90 seconds fails its task quality gate.

## Public surface

Complementary discovery uses the complete directed verified graph projection,
including call/import, source-test and config-consumer edges, before the pool is
bounded to 64 candidates. Traversal stops after two hops. Displayed planner
neighbors remain bounded independently. Exact groups retain their ordering;
unclosed evidence requirements precede relevance in complementary selection.
Centrality and inferred edges cannot admit a file without a lexical/exact seed
or a verified connection. Query instruction words do not form coverage facets.
Entrypoint evidence belongs to the source of a captured handler/callback flow.
An executable module plus an import cannot establish that flow. Source-test
coverage selects a counterpart with a verified incoming call or reference by
relevance. Import-only and naming links remain discovery hints. Other related
tests stay available as optional context instead of becoming universal obligations.
Role bindings must belong to a task anchor or an explicit source obligation;
an unrelated file of the same category cannot close a requested role. Each
explicit implementation anchor needs its own test binding when tests are
requested. Directed call endpoints and connected entrypoint facts retain their
source obligations through selection and compilation.

Source-bound resolved anchors retain the query identifier, structural symbol ID,
qualified name, resolution method and declaration/implementation evidence IDs.
A unique component suffix may resolve `Widget.run` to `widget.Widget.run`;
full names take precedence and multiple matches remain ambiguous. Symbol-level
call and test obligations use directed persisted CodeMap facts, including calls
within a file. Each selected method requires its own implementation range;
the enclosing class remains optional. Planner binding validation and ledger
construction use the same frozen requirement predicate and source identity.

Real-repository observations retain compilation sufficiency and materialization
coverage independently of planner status in both deterministic and planned modes.
The evaluator reports consistent sufficient, false sufficient, insufficient and
unverified counts. Reviewed assertion support is used only by the evaluator.
Missing answer/judge evaluation leaves semantic calibration unverified. An honest
insufficient result does not automatically fail answer quality; contradictory
sufficient claims do. Legacy `plan_sufficient` remains readable, while acceptance
requires the new compilation audit and verified semantic evaluation.

Warm retrieval caches parsed immutable CodeMaps and graph projections by
generation and digest. Every query still reads and validates their records and
shards before reuse. Neighbor lookup groups edges by source once; implementation
and symbol lookup structures are reused within the query. These runtime caches
do not change persisted derived records or trigger structural reanalysis.

Frozen source obligations retain declaration IDs and add implementation/call-site
addresses from digest-checked persisted CodeMaps. The compiler selects source
coverage before optional context and evaluates the final ledger from actual
SLICE/FULL ranges and source hashes. MAP/SUMMARY have no source evidence IDs.
A deterministic result can be sufficient when every frozen obligation and
requested role is present and the task has an exact identifier or a source-bound
grounded semantic anchor. Lexical matches and structural role coverage alone do
not resolve the task topic: `task_anchor_unresolved` keeps such compilations
insufficient even when the selected heuristic obligations fit. Grounded semantics
remain interpretation and do not create structural facts. Exact anchors must match
the supplied identifier against a verified symbol or path; a code-shaped word
elsewhere in the question does not validate incidental lexical matches. Missing required source material reports
`required_source_evidence_missing`; a planner declaration cannot override it.

Python exports `load_relationship_graph()`, `load_orientation_map()`,
`retrieve_context_candidates()`, and `compile_context_capsule()` plus the
public card/candidate/capsule/budget/estimator types. Bridge 2.1, MCP, and the
development HTTP API expose read-only `map`, `search`, `symbol`, and `compile`
operations. Bridge 2.2 adds explicit `planning_mode` and advertises the Evidence
Plan schema; Bridge 2.1 keeps the unchanged `rerank` wire field. Additive
Bridge 2.2 results expose coverage diagnostics and compilation sufficiency.
None can write
source, invoke a shell, or mutate Git.
`contextforge map --kind orientation|architecture|conventions|features|all`
exposes the same pinned artifacts. Normative schemas are
[`orientation-map-v3.schema.json`](../schemas/orientation-map-v3.schema.json)
and [`repository-map-v3.schema.json`](../schemas/repository-map-v3.schema.json).
# Complete assertion support auditing

Reviewed support is evaluator-only. Compilation and answer audits share the
material-presence predicate: every support of an assertion needs its complete
source range and known material IDs. One present support cannot substitute for
another missing support. A sufficient compilation with missing reviewed support
is false sufficient even when no semantic judge has run. Answer evaluation adds
citation checks and groundedness; missing answer/judge evaluation remains
semantically unverified. All model payloads, including blinded judges, receive
only public assertion IDs and descriptions, never reviewed support addresses.

Broad discovery covers topical facets with up to four lexical seeds and verified
two-hop expansion before the 64-candidate pool is truncated. Explicit anchors
retain exact-group priority; complementary selections cover frozen obligations,
then uncovered IDF-weighted facets. Diagnostics preserve field scores, match
origin, facet weights, and expansion reasons. Model tags contribute semantic
ranking only when their claims retain source-bound evidence. Deterministic
fallback prose cannot establish semantic task grounding. Several independent
grounded topic anchors retain separate source obligations.

Derived retrieval records carry source evidence units for implementations,
observed calls/references/callbacks, and decorators. Units retain the owner
symbol, source hash, range, stable evidence ID, and verification basis. Observed
callback syntax does not establish an internal call target. Candidate units use
the smallest owning implementation for an address; unrelated class members are
optional. Distinct evidence IDs sharing one range survive discovery. Derived
analyzer version changes rebuild these records from saved CodeMaps, without
extracting unchanged sources again.

Python analyzer version 7 additionally records lambda callback arguments as
observed syntax. Upgrading an index with older Python facts requires Python
reanalysis; other language facts remain reusable. A subsequent unchanged build
is a no-op and preserves the generation.

Frozen source requirements contain both evidence IDs and complete behavioral
ranges. Grounded claims retain all supports and the smallest owning
implementations; linked test usage retains its implementation and decorators.
Other file matches remain optional. Materialization recovers selected canonical
IDs from verified CodeMaps when their complete ranges are physically covered.
Unknown IDs receive no credit. Upgrades preserve the same frozen obligations;
MAP/SUMMARY cannot close behavioral source requirements.

Planner search strings are recorded separately from the original QueryIntent.
Search can discover evidence from an empty pool, but rewritten identifiers never
become explicit user anchors. New candidates are resolved against the original
anchors and immutable CodeMaps. Discovery extends frozen requirements and
retains prior obligations, roles, and ambiguity. A broad task without grounded
topical support remains insufficient, regardless of the planner's declaration.

Each retrieval query uses one generation-bound view. Source digests are checked
on the first read of an artifact in a query; repeated reads reuse those validated
facts. Every new query validates the immutable artifacts again. Source symbol
and range lookups use a 256-entry LRU keyed by repository, generation, and digest;
verified graph routing uses an eight-entry LRU with the same provenance boundary.
Coverage checks index IDs and range bounds rather than scanning every candidate
range for every requirement. These caches neither disable integrity checks nor
change deterministic retrieval output.


Bounded live benchmarks share a dispatch budget across index, planner, paired
answers, and judges. The guard checks actual-call allowance, estimated input
allowance, phase ceilings, and elapsed time before dispatch. Bounded runs require
zero transport retries and JSON repairs. Exhausted allowance remains a partial
run with explicit stop reasons; it never establishes semantic acceptance.
Compiler duration is reported separately. Optional query stage measurements are
returned through a caller-owned dictionary, leaving deterministic retrieval
results independent of clock readings. Existing reports read these fields with
empty or absent defaults.

Phase token estimates describe dispatched attempts. A circuit or preflight
rejection records zero attempts and zero dispatched input tokens. Each concurrent
request records its own attempts instead of a delta from a shared counter.
Pre-dispatch prompt-size estimates remain separate from observed phase usage.


The Codex adapter passes explicit reasoning settings through CLI configuration
rather than reporting an unapplied setting. `provider_default` leaves the CLI
choice unchanged; the common `off` setting requests CLI `none` without silently
falling back. A live preflight must establish that the selected model supports
the requested effort. Unsupported settings block that protocol until an
explicit supported setting is selected. Login preflight failures count zero
model dispatches.
