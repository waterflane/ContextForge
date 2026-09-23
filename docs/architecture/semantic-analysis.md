# Sparse grounded Semantic Cards v3

Semantic enrichment in Index v3.1 is evidence-bound. It
does not replace CodeMaps, source identity, or graph facts. The structural
generation is published before enrichment starts and remains usable if the
provider fails, the operation times out, or the caller cancels enrichment.
The current cache identity is analyzer 8 with prompt `semantic-card-v3.5`.
An incomplete file remains partial and is retried on the next update.

## Card contract

`SemanticCard` schema 3 contains:

- top-level analyzer, method, cache-hit, and repair provenance;
- portable path, source SHA-256, facts SHA-256, and one of the `code`,
  `documentation`, `config`, or `test` profiles;
- a required grounded synopsis and at least one grounded concept;
- optional responsibilities, at most 12 verified key symbols, side effects,
  profile-specific facts, and grounded inferred relationships;
- a canonical evidence table whose IDs address current source ranges and/or
  verified fact and symbol IDs; and
- `complete`, `partial`, or `deterministic` quality plus bounded safe
  diagnostics.

Every string used by retrieval ranking is a `GroundedClaim` with at least one
known evidence ID. Validation discards unknown IDs, stale hashes, out-of-source
ranges, and symbol bindings that do not refer to a verified CodeMap symbol.
Optional claims validate independently: one invalid optional item is removed
without losing valid siblings and changes quality to `partial`. Invalid
required fields permit at most one repair request; after that the builder uses
a deterministic fallback where safe or records the file failure.

The normative artifact schema is
[`semantic-card-v3.schema.json`](../schemas/semantic-card-v3.schema.json).


Every callable with a stable CodeMap ID receives a concise description and
supported English search expressions. Direct call edges may also receive
expressions. Requests include the full file, all callable IDs and ranges,
outgoing call provenance, and direct external callee code. Model output cannot
create IDs or structural edges. Approved expressions feed BM25; exact symbols
and verified graph edges remain independent.
## Profiles and deterministic routing

The four profiles request different bounded facts:

- `code`: purpose, concepts, responsibilities, key symbols, and side effects;
- `documentation`: sections, guarantees, APIs, commands, constraints, and
  references;
- `config`: purpose, sections, important keys, and configured subsystems; and
- `test`: tested subsystem, scenarios, fixtures, and covered symbols.

Empty, generated/control, lock, barrel, and simple metadata files receive
deterministic cards without a provider call. Barrel classification is
behavioral: an initializer may contain imports, re-exports, and `__all__`, but
no executable calls or callable implementations. An executable `__init__.py`
or `index.js`/`index.ts` remains eligible for model analysis. Key symbols
in the card are limited to verified public, exported, central, entrypoint, or
side-effect-heavy declarations; the callable lexicon covers every callable.
The configured scope and budgets bound model requests.

## Scheduler limits

The default `all` scope analyzes every eligible file and callable. The
`priority` scheduler remains available for bounded work. Defaults are 64
model files for priority, 100,000 requests, and 100,000,000 estimated input
tokens. If changed files alone exceed a ceiling, the same
deterministic structural score selects within the tier.
CLI/config and the Python API may select `priority`, `all`, or `none` and
lower those ceilings.

The scheduler plans the whole bounded priority set before dispatch. Per-attempt
provider timeout is independent of the operation timeout. A provider response
may never expand the selected file set, evidence table, source ranges, or
request budget.

Code and test requests carry the full source file. Callable targets may be
batched, but each batch repeats the full file and call table. If direct external
callee code exceeds the effective context window, the request retries with
the full file and call table in `file_only` mode. If even that cannot fit,
the file fails explicitly without source truncation. Documentation and config
profiles retain bounded chunking. Evidence IDs cover verified import, call,
reference, and config facts.

Root evidence, key symbols, and resolved facts are bounded independently: a
card has at most 32 evidence records, at most 12 key symbols, and a 128 KiB
serialized limit. Partial cards store their actual source coverage; their
synopsis is not represented or ranked as a whole-file description.

Token accounting covers the entire `ModelRequest`, including its response
schema and protocol wrapper. Every primary and scheduler-owned repair consumes
one shared request slot; without a slot no repair is sent. Provider-level JSON
repair is disabled for Semantic Cards, leaving exactly one repair authority.
Ranking claims require an exact identifier anchor or two meaningful lexical
anchors in cited source. Speculative `likely`/`probably`/`may` prose is retained
only as interpretation and does not enter ranking. Requests enumerate the exact
`allowed_evidence_ids`; transport
container IDs are explicitly non-evidence so compatible providers cannot
silently substitute them. Safe diagnostics include a typed dropped-item count
for benchmark accounting without retaining rejected prose.

## Content-addressed cache and rename reuse

Validated model payloads use the direct cache path
`cache/semantic/<prefix>/<key>.json`. The key includes source SHA, parser and
analyzer versions, semantic schema, prompt version, profile, and provider/model.
It deliberately excludes the repository path. On a cache hit, path-specific
evidence and symbol IDs are rebound to the current CodeMap and fully validated.
This permits a byte-identical renamed file to reuse semantic content without a
model call while preventing stale path evidence from entering the new card.
After an unrelated structural update, an unchanged published card is also
reused when both its source and CodeMap record digests still match. Its inferred
targets are rebound against the current closed candidate set; stale targets are
dropped independently. `--force-reanalyze` remains the explicit way to retry a
previous deterministic fallback or otherwise bypass both reuse paths.

## Grounded inferred relationships

An eligible card receives at most 24 deterministically selected relationship
candidates. The provider can return only their content-addressed candidate IDs
and current-file evidence IDs. Validation independently removes unknown,
stale, self, and duplicate targets; surviving claims keep the card while any
rejection marks it `partial`. On source or target rename, candidate paths and
symbol IDs are rebound from current source SHA and structural identity. These
relationships are published only in the enriched graph with
`model-inferred` provenance and never affect centrality or structural
dependency metrics.

## Privacy and failure behavior

Provider prompts contain only bounded current-file source and verified local
facts. Raw prompts/responses are not stored by default. Remote transport still
requires the configured external-data policy. Safe diagnostics contain no
source, provider body, credential, or absolute path.

An enrichment failure never rolls back the already published structural
generation. Successful enrichment publishes a second immutable generation in a
separate transaction. Old generations remain until explicit `index clean`.
