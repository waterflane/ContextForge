# Repository intelligence storage

## Declaration extraction and coverage

Polyglot analyzer version 4 supports named object methods, enum/module methods,
JS/TS callable class fields and parenthesized initializers, ambient TS functions,
C/C++ prototypes, generic Rust impl owners, and C# file-scoped namespaces.
Contained method IDs must refer to actual callable children of their owner.
Anonymous returned objects retain lexical ancestry without turning their
enclosing function into a type or object owner.

Parser/validation failures are isolated per file as a `parse_error` CodeMap
with an `extractor_error` diagnostic and no claimed declarations. The manifest
record is complete as a stored diagnostic, not as successful extraction; such
records are always retried. Source-read/freshness and publication failures still
abort. Old analyzer records are not reused on rebuild; old generations remain
readable and publication remains atomic.

`parsed` means syntactically parsed, not exhaustive coverage of a language.
Recognized declarations without a supported name produce a partial diagnostic.
Call/import capability is separate: current Python static extraction is
supported, current polyglot extraction is unsupported for those relationships,
and unknown/old analyzers have unknown coverage. Mixed repositories report
partial coverage; zero observed calls never proves absence of dynamic calls.

## Implemented boundary

ContextForge has deterministic local storage for structural facts and separate
incremental semantic interpretations. It provides strict CodeMap and manifest
models,
Python AST extraction, unsupported-language fallback records, conservative
relationship resolution, incremental invalidation, atomic staged records,
immutable generations, an atomic active pointer, bounded single-writer locking,
recovery, cleanup, and scanner protection.

The semantic-card builder may call the approved provider adapter for bounded
file analysis. Grounded cards remain interpretations; source, CodeMaps, and
structural graph edges remain authoritative. Repository maps are deterministic
aggregations and task retrieval/Context Capsule compilation use pinned public
APIs. See [Index v3 retrieval and Context Capsule compiler](index-v3-context-compiler.md).

## Ownership and layout

The layout follows the approved immutable-generation design:

```text
.contextforge/
  config.toml                         # user-authored input after initialization
  index/                              # generated index truth
    manifest.json                     # atomic ActiveIndexPointer, when active
    lock.json                         # transient writer metadata
    staging/<run-id>/                 # resumable, not visible to readers
    generations/<generation-id>/
      manifest.json                   # complete IndexManifest
      files/*.facts.json              # source identity + deterministic CodeMaps
      files/*.interpretation.json     # sparse grounded Semantic Cards
      relationships.jsonl             # canonical structural edge records
      relationship-graph.json         # graph metrics and projections
      retrieval-structural.json       # structural BM25 inputs/postings
      retrieval-semantic.json         # enriched BM25 inputs/postings
      orientation.json                # full structural orientation map
      architecture.json               # deterministic enriched map
      conventions.json                # deterministic enriched map
      features.json                   # deterministic enriched map
    cache/semantic/<prefix>/<key>.json # path-neutral validated model payloads
  contexts/                           # generated saved context packages
  runs/                               # generated operational diagnostics
```

`initialize_index()` writes the approved default `config.toml` only if it is
missing. It never replaces an existing configuration. From that point the file
is user-owned and may be committed. Index generations, saved contexts, and run
diagnostics are generated data.

The scanner treats `.contextforge/` as one non-negatable protected root. The
user-owned configuration remains readable by the configuration loader and is
preserved by cleanup, but no file under `.contextforge/` can enter structural,
generic, or rich semantic analysis.

## Public API

`contextforge.intelligence` exports:

- CodeMaps: `extract_code_map`, `extract_code_maps`,
  `extract_python_code_map`, `extract_fallback_code_map`,
  `resolve_relationships`, `serialize_code_map`, and
  `deserialize_code_map`;
- structural persistence: `build_structural_index` and
  `load_file_code_map`;
- initialization and lifecycle: `initialize_index`, `begin_index_build`,
  `load_manifest`, `write_manifest`, `inspect_index_status`, and
  `clean_generated_index`;
- records and recovery: `write_index_record`, `load_index_record`, and
  `cleanup_stale_temporary_files`;
- locking: `acquire_index_lock` and the `IndexWriteLock` ownership token;
- comparison: `identify_added_files`, `identify_changed_files`,
  `identify_unchanged_files`, `identify_deleted_files`, and
  `identify_stale_analysis`; and
- construction and canonicalization: `build_index_manifest`,
  `calculate_source_snapshot_digest`, `calculate_generation_id`, and
  `canonical_json_bytes`.

Mutation APIs require an active `IndexWriteLock`. Readers do not take the lock.
`load_index_record()` accepts a caller-pinned manifest so a multi-record reader
does not need to reopen the active pointer between reads.

## Manifest schema version 3

New index pointers, manifests, records, CodeMaps, graph records, retrieval
postings, maps, and Semantic Cards use v3. A v2 index is recognized by status
only and reports `rebuild_required`; retrieval refuses it and `index update`
requires a fresh `index build`. Semantic v2 data is not migrated. Old immutable
generations are not deleted automatically.

Publication has two transactions in one writer-lock lifetime. The structural
generation becomes active first. Enrichment then publishes a second generation
or leaves the structural one active on failure, timeout, or cancellation.

Persisted models are frozen Pydantic models with unknown fields forbidden.
Canonical manifest JSON is UTF-8, sorted-key compact JSON with LF termination.
The manifest has no timestamp, hostname, PID, absolute repository root, random
ID, or credential field.

The main models are:

- `SchemaVersionMetadata`: independent index, manifest, and record versions;
- `ModelIdentity`: provider and model identifiers only;
- `AnalyzerIdentity`: analyzer ID/version, analysis-prompt version, response
  schema version, and optional model identity;
- `IndexedFileState`: portable source path, source SHA-256 and size, language,
  analyzer identity, record location/digest/status;
- `IndexBuildState`: the canonical last successful complete build inputs and
  digests, including relevant build-options digest and previous generation;
- `IndexStatistics`: counts, source bytes, and canonical language counts;
- `IndexManifest`: complete immutable generation metadata; and
- `ActiveIndexPointer`: generation ID, exact generation-manifest location, and
  source-snapshot digest.

Generation IDs are SHA-256 digests of the complete canonical manifest content
except the self-referential `generation_id` field. File-record content is bound
through each record's SHA-256. API keys, bearer tokens, headers, and credential
objects are not fields in any persisted schema and unknown fields are rejected.

## CodeMap schema version 3

CodeMaps also retain bounded deterministic `source_regions` and
`source_regions_truncated`, including when no model is configured. These are
source partitions, not inferred symbols. Structural reads are bounded to
16 MiB by default; semantic coverage has its separate 64-chunk limit.

`FileCodeMap` is a closed, frozen, model-free record containing the portable
path, raw-source SHA-256 and byte size, language, analyzer identity, parse
status, canonical line count, module docstring, imports, explicit and
conventional exports, uppercase top-level constant names, symbols,
relationships, and parser diagnostics. Nested Python symbols are ordered by
source position. Their qualified names append every lexical parent, for example
`pkg.module.Class.method.nested`; duplicate same-name declarations retain the
same qualified name and receive distinct deterministic ordinal-based IDs.

Python signatures and annotations are exact canonical source slices. Calls are
observed syntax facts. A call is `internal` only for an unambiguous local
lexical name or resolved import alias in the applicable lexical scope. Parameter
or local rebinding, cross-function imports, inexact dotted module prefixes, and
attributes of imported objects remain `unresolved`. Absolute imports absent
from the snapshot are `external`, which means only “outside this snapshot,” not
that the module exists or can be imported. Test links use an unambiguous
internal import or path/name convention and store that detection basis; they do
not claim runtime coverage.

CodeMap validation rejects duplicate fact IDs, dangling same-file symbol
references, relationship source-path mismatches, ranges beyond the verified
canonical source line count, and structural claims on parse-error or fallback
records. Relationship IDs include the resolver version; Python extractor and
resolver behavior changes therefore invalidate reusable records explicitly.

Ranges use one-based lines and zero-based half-open columns. Python records
preserve the standard AST's UTF-8 byte-column convention, including for Unicode
identifiers. Nested definitions are emitted immediately after their lexical
parent in source-position order.

Unsupported languages receive only verified file identity, line count,
fallback analyzer identity, empty fact collections, and a deterministic
diagnostic. ContextForge never imports or executes repository modules during
extraction.

## Invalidation

A same-path record is current only when all relevant inputs still match:

- source SHA-256, source byte size, and language;
- deterministic analyzer ID and version;
- analysis prompt version and response schema version;
- provider and model identity when present;
- index, manifest, and record schema versions; and
- the digest of relevant build options.

Path additions, deletions, and content changes are reported separately.
Modification time is neither stored nor compared, so a timestamp-only change
does not invalidate unchanged bytes. Renames are a deletion plus an addition.

## Atomicity and recovery

Every staged record is written to an identifiable sibling temporary file,
flushed, fsynced, and atomically replaced. A complete canonical generation
manifest is written only after all referenced file records exist and match
their declared digests. The staging directory is then moved to its immutable
content-addressed generation directory.

Only the final atomic replacement of `index/manifest.json` exposes a
generation. If record writing, generation materialization, or pointer
publication fails, the previous pointer and generation remain usable. A
complete inactive generation can be activated by retrying publication with the
same manifest. Temporary cleanup removes only files with the dedicated storage
suffix and never follows a symlink or junction.

All caller-supplied run IDs and record locations use strict portable path
validation. Absolute paths, POSIX or backslash roots, traversal, Windows
drive-absolute and drive-relative forms, UNC paths, NUL, and noncanonical
segments are rejected. Every generated directory component is checked without
following symlinks; junctions are rejected as directory components.

## Lock policy

`lock.json` is created exclusively and records schema version, run ID, PID,
host fingerprint, start time, and a random ownership nonce. Lock metadata is
operational and never enters canonical manifest comparisons.

Acquisition is bounded and non-waiting. An apparently active same-host process
produces `IndexLockActiveError` and is never removed. A stopped same-host
process requires the explicit `recover_stale=True` policy. Malformed or
other-host metadata requires separate explicit confirmation. Recovery compares
file identity before removal, and a released or replaced ownership token cannot
mutate the index. This avoids permanent crash lockout without silently stealing
an active lock or relying on flaky elapsed-time thresholds.

## Current limitations

- The CLI orchestrates build, update, status, and policy-bounded cleanup for a
  single repository root. Full multi-root workspaces remain deferred.
- Python provides declarations plus conservative call/import relationships.
  JavaScript, TypeScript, Java, C#, Go, Rust, C, C++, PHP, and Ruby provide
  verified Tree-sitter declarations; their relationship extraction remains
  explicitly unsupported. Other selectable text files receive file-level
  fallback records.
- Name and call resolution remains conservative and incomplete for dynamic
  dispatch, rebinding, wildcard imports, and ambiguous module layouts.
- File/symbol semantics, repository architecture/feature maps, and
  task-specific indexed/fresh/hybrid discovery are implemented.
- Validated semantic task checkpoints can be resumed by reopening the same run
  ID. General phase journals are not implemented yet.
- Generation retention policy is not automatic; explicit cleanup resets only
  generated index truth and preserves config, saved contexts, and runs.
- Lock recovery can prove same-host process absence only as well as the host
  process check; other-host or malformed ownership always needs confirmation.
