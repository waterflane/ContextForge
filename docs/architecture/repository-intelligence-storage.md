# Repository intelligence storage

## Implemented boundary

ContextForge has a deterministic local storage foundation for future repository
intelligence. It provides strict manifest models, incremental invalidation,
atomic staged records, immutable generations, an atomic active pointer,
bounded single-writer locking, recovery, cleanup, and scanner protection.

This boundary does not extract CodeMaps, define fact-record payloads, call a
model, discover context, add CLI commands, or expose MCP. The placeholder
`architecture.json` and `features.json` values in a new staging area are JSON
`null`; they do not claim that model analysis ran.

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
      files/                          # per-file records
      symbols.jsonl
      relationships.jsonl
      architecture.json
      features.json
  contexts/                           # generated saved context packages
  runs/                               # generated operational diagnostics
```

`initialize_index()` writes the approved default `config.toml` only if it is
missing. It never replaces an existing configuration. From that point the file
is user-owned and may be committed. Index generations, saved contexts, and run
diagnostics are generated data.

The scanner treats only `.contextforge/index/`,
`.contextforge/contexts/`, and `.contextforge/runs/` as non-negatable protected
roots. It does not ignore `.contextforge/` as a whole, so
`.contextforge/config.toml` remains ordinary selectable repository input.

## Public API

`contextforge.intelligence` exports:

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

## Manifest schema version 1

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

- There is no index orchestration command or structural extractor yet.
- File-record JSON payload schemas, symbols, relationships, architecture maps,
  and feature maps belong to later approved components.
- Staging can be resumed by reopening the same run ID, but phase journals and
  semantic task checkpoints are not implemented yet.
- Generation retention policy is not automatic; explicit cleanup resets only
  generated index truth and preserves config, saved contexts, and runs.
- Lock recovery can prove same-host process absence only as well as the host
  process check; other-host or malformed ownership always needs confirmation.
