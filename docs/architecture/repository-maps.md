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
prompt version, and analysis-options digest. Its typed entries are `ModuleRole`,
`EntryPoint`, `DataFlow`, and `ExternalBoundary`. It also carries explicit test
relationships from the deterministic overview, model-inferred relationships,
diagnostics, evidence, confidence, and coverage.

`FeatureMap` is a behavior-based model grouping. Every `FeatureArea` has a
generated stable ID derived from the model's stable key and its canonical file
and symbol membership, a title and description, participating files and
symbols, related tests, evidence, confidence, and unresolved questions. It can
therefore group poorly named files when their bounded structural and semantic
summaries describe related behavior. A generated ID is practical provenance,
not a promise that a materially changed feature keeps the same identity.

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

1. deterministic package/module shards containing bounded CodeMap projections
   and, when available, bounded file and symbol semantic summaries;
2. one or more strictly validated group-synthesis levels; and
3. separate strictly validated repository architecture and feature passes.

Source text is absent from global requests. Prior file analyses and hierarchy
outputs are framed as untrusted model context. Repository text and prior model
prose cannot change instructions, select another path, request tools, or expand
budgets. All response paths are checked against the pinned snapshot; all final
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

The two final map passes fail independently. With no prior map, a valid map may
be published while the malformed/failed sibling remains explicitly absent. If
a complete previous map for the same facts exists and recovery is enabled, any
failed replacement leaves that previous generation active and returns it with
`recovered` status; it is never relabeled as output from the requested new
prompt. Strict mode publishes nothing after any global-map failure.

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
- No task-specific context discovery, context recommendation, CLI command, or
  MCP surface is implemented by this component.
