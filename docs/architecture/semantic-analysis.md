# Sparse grounded Semantic Cards v3

Semantic enrichment in Index v3 is optional, sparse, and evidence-bound. It
does not replace CodeMaps, source identity, or graph facts. The structural
generation is published before enrichment starts and remains usable if the
provider fails, the operation times out, or the caller cancels enrichment.

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
or `index.js`/`index.ts` remains eligible for model analysis. Model analysis is
sparse; it does not describe every declaration. Key symbols are limited to
verified public, exported, central, entrypoint, or side-effect-heavy
declarations.

## Scheduler limits

The default `priority` scope orders tiers as changed/added files, entrypoints,
public APIs, the highest 10% centrality tier (at least one file), important
docs/config, related tests, then a stable structural score. Defaults are 64
model files, 96 requests, 256,000 estimated input tokens, and at most four
chunks per large file. If changed files alone exceed a ceiling, that same
deterministic structural score selects within the tier. CLI/config and the
Python API may select `priority`, `all`, or `none` and lower those ceilings.

The scheduler plans the whole bounded priority set before dispatch. Per-attempt
provider timeout is independent of the operation timeout. A provider response
may never expand the selected file set, evidence table, source ranges, or
request budget.

A file uses one request when its complete bounded prompt fits. Otherwise the
UTF-8/declaration-aware planner emits at most four chunks with eight source
lines of overlap. Evidence IDs are scoped to the current chunk and also cover
verified import, call, reference, and config facts. An uncovered tail or
invalid chunk makes the surviving card partial.

Token accounting covers the entire `ModelRequest`, including its response
schema and protocol wrapper. Every primary and scheduler-owned repair consumes
one shared request slot; without a slot no repair is sent. Provider-level JSON
repair is disabled for Semantic Cards, leaving exactly one repair authority.
Ranking claims require both valid evidence and a lexical/identifier anchor in
that evidence. Requests enumerate the exact `allowed_evidence_ids`; transport
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
