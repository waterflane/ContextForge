# Repository architecture and feature maps

## Implemented boundary

ContextForge can now build repository-wide deterministic overviews and optional
model-interpreted architecture and feature maps. These records are index
artifacts. They are not task-specific context discovery, a context selection,
CLI orchestration, or MCP resources.

The public Python API exposes `build_repository_overview()`,
`build_repository_maps()`, `load_repository_overview()`,
`load_architecture_map()`, and `load_feature_map()`. Normal tests use the
deterministic fake provider and require no network access.

## Exact record meanings

`RepositoryOverview` is a deterministic projection of the pinned CodeMaps. It
contains the portable repository tree, package path groupings, language and
coverage counts, parser limitations, source/test associations, and a normalized
relationship graph. It contains no model-written architectural description.

`ArchitectureMap` is a model interpretation bound to the source snapshot,
CodeMap facts digest, file-interpretation digest, global analyzer identity,
prompt version, and analysis-options digest. The provider-facing DTO is smaller
than this internal model: validated concise signals become attributed internal
diagnostics while deterministic relationships and test links are enriched from
the verified overview. The model is not asked to reproduce source ranges.

`FeatureMap` derives bounded `FeatureArea` records from validated feature
signals and cited paths. IDs and test membership are added deterministically.
A generated ID is practical provenance, not a promise that a materially changed
feature keeps the same identity.

`RepositoryDiagnostic` labels deterministic limitations, model uncertainties,
or operational failures by provenance. A valid map response proves only schema
and evidence integrity; it does not prove repository completeness.

## Facts, best-effort structure, and interpretation

Every repository relationship declares one of three provenances:

- `verified`: imports, imported-by inverses, and lexical containment derived
  directly from deterministic CodeMaps;
- `best-effort-structural`: observed call names, static references,
  source/test associations, and configuration consumers whose basis is stored
  but whose runtime meaning is not proven; or
- `model-inferred`: feature membership, entry-point-to-handler mappings, and
  semantic relatedness. These carry model prose, evidence, and confidence.

Model output never mutates a CodeMap fact or promotes a guess to verified.
Source/test relationships do not claim runtime test coverage. Observed calls do
not form a sound dynamic call graph. An external import means only that its
target was outside the scanned snapshot.

## Bounded hierarchical synthesis

Global analysis never sends the full repository source in one request. It uses:

1. deterministic package/module shards of at most two files containing compact
   CodeMap projections
   and, when available, bounded file and symbol semantic summaries;
2. one or more strictly validated group-synthesis levels; and
3. separate strictly validated repository architecture and feature passes.

Every level is preflighted against the configured context window. Prior-summary
context is shortened deterministically before dispatch if necessary. The
model-facing schema requires version 1, a short scope ID, 160-character title,
600-character summary, 240-character confidence rationale, no more than eight
architecture/behavior/feature signals, six questions, and twelve compact
evidence records. Each request allows at most 512 output tokens. This avoids the
previous nested 50-100 item grammar and its optional source-range unions.

Source text is absent from global requests. Prior file analyses and hierarchy
outputs are framed as untrusted model context. Repository text and prior model
prose cannot change instructions, select another path, request tools, or expand
budgets. The complete hierarchy is preflighted against a hard model-call limit
before the first summary request, so repository size cannot create an unbounded
provider-call sequence. All response paths are checked against the pinned snapshot; all final
symbols, facts, ranges, confidence values, and evidence are validated before
publication.

## Persistence and incremental behavior

The immutable generation stores `overview.json`, `architecture.json`, and
`features.json`. Their canonical digests are folded into the generation's
interpretations digest. Readers pin one manifest and reject tampered, stale,
unknown-version, duplicate-key, or schema-invalid records.

Unchanged maps with identical facts, file interpretations, provider/model,
prompt, and analysis options are reused without provider calls. A changed or
deleted participating file causes the structural/semantic generation to omit
the previous global records, so repository maps must be synthesized again and
deleted paths cannot survive in membership. A global prompt-version change also
invalidates both semantic maps.

The two final map passes fail independently. In non-strict mode, a failed map or
hierarchy is replaced by a valid deterministic degraded map containing verified
overview relationships and an operational diagnostic; accepted per-file
semantics remain in the published generation. If
a complete previous map for the same facts exists and recovery is enabled, any
failed replacement leaves that previous generation active and returns it with
`recovered` status; it is never relabeled as output from the requested new
prompt. Strict mode publishes nothing after any global-map failure and restores
the prior active generation.

## Limitations

- Python has rich structural extraction; unsupported languages contribute only
  file identity and bounded semantic information when available.
- Dynamic imports, dispatch, decorators, framework registration, generated
  code, and runtime configuration can make architecture and relationships
  incomplete.
- Feature boundaries, module roles, entry-point handlers, data-flow prose, and
  external-boundary descriptions are model interpretations and may vary.
- Hierarchical summaries reduce prompt size but can lose detail. Coverage and
  unresolved questions remain visible rather than implying completeness.
- Task-specific discovery consumes these maps through the shared discovery
  service. CLI and MCP adapters expose that service rather than duplicating map
  or selection logic.
