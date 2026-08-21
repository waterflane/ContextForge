# ContextForge v0.3.0 — Context Packages
> [!NOTE]
> Historical implementation plan. It is retained for design context and is
> not current user documentation or a release procedure.


Status: approved plan, implemented through the CLI adapter stage. Section 18
records the final public CLI spellings where implementation differs from the
original proposal.

## 1. Current repository assessment

ContextForge is a small Python 3.12+ modular monolith. The tracked tree contains
61 files under `src/`, `tests/`, `docs/`, and project metadata. The worktree was
clean when this plan was prepared. The current package version is still
`0.1.0`; v0.2 is complete but unreleased according to the README and changelog.

The relevant current boundaries are:

- `contextforge.repositories` owns repository inventory. Its public
  `scan_repository()` function returns an immutable `ProjectSnapshot` whose
  `files` are deterministically ordered `ProjectFile` records with portable
  relative paths, byte sizes, language labels, SHA-256 hashes, and text status.
- `ProjectSnapshot.files` contains only regular, bounded, binary-screened files.
  Ignored, protected, oversized, binary-like, unreadable, symlink/junction, and
  unsupported entries are recorded separately and are not selectable.
- `contextforge.repositories.files` already contains the safety primitives to
  normalize relative paths, reject absolute/traversing/Windows drive-relative
  paths, compare pre-open and opened file identity, cap reads, classify the
  initial sample, and hash bytes in one pass.
- The scanner never follows symbolic links or Windows directory junctions. It
  prunes protected VCS roots and ignored directories and preserves Git-style
  ignore precedence.
- `contextforge.context.package.ContextPackage` is only a frozen placeholder
  (`title` and `items`). v0.3 replaces that placeholder with the real domain
  model. `contextforge.prompts` remains a separate placeholder and is outside
  this milestone.
- `contextforge.cli.main` is one Typer application. The `scan` command creates
  domain options, calls the repository scanner, delegates rendering and atomic
  output to `cli.scan_output`, translates expected failures to exit codes, and
  otherwise lets unexpected exceptions remain visible to tests. This thin
  adapter shape should be retained.
- `cli.scan_output.write_output_atomic()` already renders before writing, uses
  a sibling temporary file plus `fsync`, publishes with a hard link so a racing
  destination cannot be overwritten, removes the temporary file, requires the
  parent to exist, and refuses all existing destinations including symlinks.
- ADR-001 requires inward dependencies, independent core/application logic,
  thin entry points, and no speculative plugins, factories, event buses, or
  dependency-injection container.
- Runtime dependencies are FastAPI, PathSpec, Pydantic, Pydantic Settings, and
  Typer. All required v0.3 behavior can be built with those dependencies and the
  standard library; no runtime dependency is needed.

Recent commits added and then hardened repository scanning, directory pruning,
the `.uv-cache/` default exclusion, scan reporting, race-resistant file
inspection, and cross-platform CLI tests. v0.3 must preserve those contracts.

Baseline validation on 2026-07-14:

```text
ruff check .             passed
ruff format --check .    passed (40 files)
mypy                     passed (40 source files)
pytest                   passed (166 passed, 3 skipped)
branch coverage          98.12%
git diff --check         passed
```

The test run reported an upstream Starlette/httpx deprecation warning and a
sandbox permission warning for `.pytest_cache`; neither affected the result.

## 2. Milestone objective

v0.3.0 will turn a `ProjectSnapshot` into deterministic, portable, reviewable
context without coupling ContextForge to an AI model or provider. It will:

1. derive and render a project tree from the snapshot's selectable files;
2. select snapshot files by exact path, directory, or glob, then apply
   exclusions;
3. safely re-open and decode selected files while detecting changes since the
   scan;
4. optionally select explicit inclusive line ranges;
5. construct one canonical `ContextPackage` model;
6. render that model as Markdown or versioned JSON;
7. parse, inspect, and validate an existing JSON package without consulting a
   repository; and
8. expose the behavior through thin `tree`, `context create`, and
   `context inspect` CLI adapters.

No timestamp, random identifier, current working directory, resolved root, or
other machine-local value participates in a package. The same snapshot bytes,
selection, ranges, limits, and explicit title produce byte-identical output.

## 3. Scope

In scope:

- a portable project-tree domain model and text renderer;
- exact-path, recursive-directory, GitWildMatch-style glob, and exclusion
  selection over `ProjectSnapshot.files` only;
- strict selector and internal-path validation on both POSIX and Windows;
- stable, bounded file reads with identity, size, and SHA-256 verification;
- strict UTF-8 and UTF-8 BOM decoding and canonical LF newlines;
- one-based inclusive line ranges;
- immutable Pydantic domain models with semantic invariants;
- deterministic Markdown and JSON renderers;
- strict offline JSON package inspection;
- selected-file count, content-size, and serialized-package limits;
- no-overwrite atomic output; and
- thin Typer commands and comprehensive offline tests.

Existing scan APIs, scan output schema version 1, ignore behavior, counters,
CLI flags, and exit behavior remain backward compatible.

## 4. Non-goals

This milestone does not add:

- LLM integration or provider SDKs, including Ollama, OpenAI, Anthropic, or
  Gemini;
- embeddings, semantic search, automatic relevance ranking, or retrieval;
- Tree-sitter, AST analysis, dependency graphs, import analysis, summaries, or
  codemaps;
- prompt profiles or model-specific tokenizers;
- SQLite or other persistence;
- IDE extensions, knowledge graphs, autonomous agents, or automatic code
  modification; or
- plugin systems, event buses, generalized pipelines, or extension registries.

The milestone does not select ignored/binary/oversized/skipped files, fetch
remote content, infer task relevance, or make Markdown round-trippable back
into the canonical model. JSON is the machine-readable package format.

## 5. Proposed file tree

The following is the proposed implementation tree, not a change made by this
planning step:

```text
src/contextforge/
  filesystem.py                 # shared stable reads and no-overwrite publish
  repositories/
    files.py                    # retain public APIs; delegate to shared read
  context/
    __init__.py                 # intentional public exports
    package.py                  # ContextPackage and nested domain models
    tree.py                     # snapshot-to-tree construction and rendering
    selection.py                # selectors, glob matching, and range requests
    reader.py                   # verified decoding and line extraction
    builder.py                  # deterministic package orchestration and limits
    renderers.py                # Markdown and JSON renderers only
    inspection.py               # strict JSON parse and semantic validation
  cli/
    main.py                     # register tree and context Typer commands
    context_commands.py         # thin command functions and error translation
    scan_output.py              # preserve API; import shared atomic publisher

tests/
  test_context_tree.py
  test_context_selection.py
  test_context_reader.py
  test_context_package.py
  test_context_renderers.py
  test_context_inspection.py
  test_cli_context.py
  ...all existing test files retained...
```

`filesystem.py` is justified by two real callers: the existing scanner/output
path and context package construction. It is not a generic storage abstraction.
Public names currently imported from `repositories.files` and
`cli.scan_output` remain available as compatibility re-exports.

## 6. Module responsibilities

### `contextforge.filesystem`

- Hold the extracted low-level identity-checked bounded binary-read primitive.
- Hold the existing no-overwrite atomic text publisher.
- Use only the standard library and contain no Pydantic, Typer, FastAPI, or
  domain policy.
- Preserve `inspect_file()` behavior by making it consume the shared primitive,
  not by maintaining two subtly different safety implementations.

### `contextforge.context.package`

- Define the immutable canonical package models and validation invariants.
- Reject unknown fields and non-portable paths.
- Contain no filesystem access or rendering code.

### `contextforge.context.tree`

- Build a flat canonical tree from `ProjectSnapshot.files` and synthesized
  ancestor directories.
- Render an ASCII tree with optional depth limiting.
- Never inspect the repository filesystem.

### `contextforge.context.selection`

- Validate exact paths, directory paths, include globs, exclusion patterns, and
  line-range request syntax.
- Resolve selectors solely against a snapshot index.
- Return one sorted tuple of selected `ProjectFile` records plus canonical
  ranges keyed by path.

### `contextforge.context.reader`

- Re-open only a selected `ProjectFile` beneath `ProjectSnapshot.root`.
- Reuse the shared stable-read primitive, verify snapshot size and SHA-256,
  strictly decode UTF-8, normalize newlines, and extract requested ranges.
- Return domain-ready blocks or raise a path-specific deterministic error.

### `contextforge.context.builder`

- Orchestrate tree construction, selection, verified reads, limit checks,
  canonical ordering, and statistics.
- Return a complete `ContextPackage` or no package at all.
- Contain no Typer/FastAPI imports and perform no rendering or output writes.

### `contextforge.context.renderers`

- Render an already valid `ContextPackage` to Markdown or JSON.
- Add no repository data and perform no selection or filesystem access.
- Guarantee LF newlines, stable ordering, deterministic escaping, and exactly
  one final newline.

### `contextforge.context.inspection`

- Safely read a bounded JSON package, reject duplicate keys, dispatch by schema
  version, validate the Pydantic shape and cross-field semantics, and return a
  validated `ContextPackage` plus an inspection summary.
- Never access paths named inside the package.

### CLI modules

- Translate Typer values to domain requests, invoke one application function,
  print a rendered result/summary, optionally call the atomic publisher, and
  map known errors to stable exit codes.
- Keep all tree, selector, reader, range, package, and validation decisions out
  of CLI code.

## 7. Domain models

All models are frozen Pydantic models with `extra="forbid"`. Tuples are used for
ordered collections. Builders produce canonical order; validators reject
non-canonical order, duplicate paths, contradictory counts, and invalid hashes
when loading JSON.

### Tree models

```text
ProjectTree
  entries: tuple[ProjectTreeEntry, ...]
  file_count: non-negative int
  directory_count: non-negative int

ProjectTreeEntry
  path: portable relative path
  kind: "directory" | "file"
```

The root is implicit and rendered as `.`. It is never stored as an absolute
path or package entry.

### Selection and range models

```text
ContextSelection
  exact_paths: tuple[str, ...] = ()
  directories: tuple[str, ...] = ()
  globs: tuple[str, ...] = ()
  exclusions: tuple[str, ...] = ()
  line_ranges: tuple[LineRangeRequest, ...] = ()

LineRange
  start: positive int
  end: positive int, end >= start

LineRangeRequest
  path: portable exact file path
  range: LineRange

ContextLimits
  max_files: positive int = 100
  max_total_content_bytes: positive int = 1_000_000
```

The request model preserves user input long enough to report which selector
failed. The resolved internal selection is path-keyed and canonicalized.

### Package models

```text
ContextPackage
  schema_version: literal 1 = 1
  title: non-empty string = "Context package"
  tree: ProjectTree
  files: tuple[ContextFile, ...]
  statistics: ContextStatistics

ContextFile
  path: portable relative path
  language: str | None
  source_size_bytes: non-negative int
  source_sha256: 64 lowercase hexadecimal characters
  source_line_count: non-negative int
  selection: "full" | "ranges"
  blocks: tuple[ContextBlock, ...]
  included_line_count: non-negative int
  included_content_bytes: non-negative int

ContextBlock
  start_line: positive int | None
  end_line: positive int | None
  text: str
  line_count: non-negative int
  size_bytes: non-negative int
  sha256: 64 lowercase hexadecimal characters

ContextStatistics
  tree_file_count: non-negative int
  tree_directory_count: non-negative int
  selected_file_count: non-negative int
  ranged_file_count: non-negative int
  selected_source_bytes: non-negative int
  included_content_bytes: non-negative int
  included_line_count: non-negative int
  languages: dict[str, non-negative int]
```

A full file has exactly one block whose `start_line` and `end_line` are null.
That also represents an empty file cleanly. A ranged file has one block per
canonical disjoint range and non-null inclusive bounds. Block text retains
whether its final selected source line ended in LF. Block `size_bytes` and hash
are calculated from `text.encode("utf-8")` after BOM removal/newline
normalization. `source_size_bytes` and `source_sha256` describe the original raw
file bytes from the snapshot.

The existing placeholder model test must remain present and continue to assert
immutability. It may be extended to construct the completed model, but no
existing test file or substantive assertion is deleted or weakened.

## 8. Data flow

```text
PATH
  -> scan_repository(PATH) -> ProjectSnapshot
       |                         |
       |                         +-> build_project_tree(snapshot)
       |                         +-> resolve_selection(snapshot, request)
       |                                  |
       |                                  +-> verified safe reads + ranges
       |                                                |
       +----------------------------------------------> build package
                                                        |
                                +-----------------------+------------------+
                                |                                          |
                         render Markdown                             render JSON
                                |                                          |
                         stdout/atomic file                         stdout/atomic file

JSON package -> bounded read -> duplicate-key/schema validation
             -> ContextPackage -> semantic recomputation -> inspection summary
```

No renderer receives `ProjectSnapshot.root`. No inspector resolves or opens a
path found in JSON. A write happens only after scanning, selection, all reads,
package validation, and complete rendering succeed.

## 9. Project-tree contract

### Authority and contents

- The tree is derived exclusively from `ProjectSnapshot.files`.
- Every file entry corresponds to exactly one selectable snapshot file.
- Directory entries are synthesized from file path prefixes. Empty, ignored,
  protected, skipped, and filesystem-only directories are absent.
- The implicit root is `.`. Absolute snapshot roots never enter the model.
- Tree entries contain structure only. Selected content metadata lives in
  `ContextFile`, avoiding duplicated hashes and sizes across the package.

### Ordering

Tree order is a deterministic pre-order traversal. Within each directory,
directory children sort before file children; within each kind, names sort by
case-sensitive Unicode code-point order. This policy is independent of host
filesystem enumeration and locale. Paths use `/` separators.

Example:

```text
.
|-- docs/
|   `-- guide.md
|-- src/
|   `-- app.py
`-- pyproject.toml
```

The ASCII connectors are normative. Entries are displayed by name, directories
end with `/`, and a final LF is always emitted. No color or terminal-width
wrapping is added by core rendering.

### Depth semantics

- Depth is measured in edges below the implicit root.
- `depth=None` means unlimited.
- `depth=0` renders only `.`.
- `depth=1` additionally renders direct root children.
- A file or directory with N path segments has depth N.
- Entries deeper than the limit are omitted. No synthetic ellipsis is inserted,
  so the requested maximum is never exceeded.
- Negative depths and non-integers are invalid input (CLI exit 2).
- Depth changes presentation only; it never changes the `ProjectTree` model or
  package selection.

## 10. Selector contract

### Snapshot-only resolution

Selection starts by indexing `ProjectSnapshot.files` by their already portable
paths. No selector is joined to the filesystem, globbed with `Path.glob()`, or
allowed to discover a file absent from the snapshot. Ignored and skipped paths
are therefore unselectable even if they exist on disk later.

### Path validation and portability

Exact paths, directory selectors, range target paths, and literal pattern
segments:

- accept `/` and user-entered `\` separators, then store `/`;
- must be repository-relative and non-empty;
- reject POSIX-rooted paths (`/etc/passwd`), backslash-rooted/UNC paths
  (`\server\share`), Windows drive-absolute paths (`C:\repo\file`), and
  Windows drive-relative paths (`C:file`);
- reject every `..` segment instead of collapsing it, even when it would remain
  lexically inside the root;
- discard harmless `.` and duplicate separator segments only for exact and
  directory selectors; and
- reject NUL characters. Package model validators apply the same rules again.

The existing `normalize_relative_path()` logic is reused after the stricter
selector pre-check. Glob normalization is separate because wildcard segments
are not filesystem paths.

### Exact paths

- `--include PATH` (alias `--file PATH`) matches exactly one
  `ProjectFile.path`.
- Matching is case-sensitive on every OS.
- A path absent from `ProjectSnapshot.files` is an error; it is not opened to
  decide whether it exists.
- A directory path supplied to `--include` or `--file` is an
  unmatched-selector error.

### Directories

- `--directory PATH` selects all snapshot files whose path begins with
  `PATH + "/"`.
- Directory selection is recursive and case-sensitive.
- `.` is the one special selector for the implicit root and selects all snapshot
  files.
- A non-root directory is considered to exist only when it is synthesized in
  the project tree. Empty filesystem directories are not in the snapshot and
  match nothing.
- A trailing slash is accepted and removed during normalization.

### Include globs

- `--glob PATTERN` uses PathSpec's GitWildMatch syntax already present in the
  runtime dependencies: `*`, `?`, bracket classes, and `**` are supported.
- Backslashes normalize to `/`; backslash escape syntax is intentionally not
  supported. An exact selector remains available for literal metacharacters.
- A pattern with no slash matches a filename at any depth (`*.py`). A pattern
  with a slash is rooted at the snapshot root (`src/**/*.py`).
- Globs match file paths, not the host filesystem or synthesized directories.
- Leading `!` is rejected. Negation is unnecessary because exclusions have a
  separate field and fixed precedence.
- Absolute, drive-qualified, NUL-containing, empty, `.`-segment, and
  `..`-segment patterns are rejected before compilation.
- Glob matching is case-sensitive on all platforms.

### Exclusions and precedence

- `--exclude PATTERN` uses the same normalized GitWildMatch syntax and matches
  snapshot file paths.
- All exact, directory, and glob includes are unioned first. If no include of
  any kind is supplied, the initial set is all `ProjectSnapshot.files`.
- The union of all exclusions is subtracted second. Exclusion always wins,
  independent of option order. There is no re-inclusion syntax.
- An exclusion may be a literal path, a basename pattern, or a rooted glob.

### Duplicates and empty matches

- The same file matched by multiple includes appears once, keyed by its exact
  portable path.
- Repeated identical selectors have no effect on output.
- Every exact path, directory, and include glob must independently match at
  least one snapshot file before exclusions; an unmatched include is a
  `SelectorNoMatchError`. This catches misspellings.
- An exclusion that matches nothing is accepted so reusable exclusion sets do
  not fail on repositories without that artifact.
- If exclusions leave no selected files, package creation fails with
  `NoFilesSelectedError`; it does not emit an empty package.
- Selector input order never controls package order. Selected files are sorted
  by case-sensitive portable path.

## 11. Safe-reader contract

For each selected `ProjectFile`, the reader performs these steps:

1. Revalidate the snapshot path as a portable relative path and look it up in
   the snapshot index.
2. Form the candidate only as `snapshot.root / path segments`; never accept an
   independent caller-supplied filesystem path.
3. Obtain non-following metadata and require a regular file.
4. Open in binary mode, compare pre-open and opened handle identity using the
   existing `stat`/`fstat` and `os.path.samestat` technique, and reject a
   symlink, junction substitution, or replacement race.
5. Require the opened size to equal `ProjectFile.size_bytes` before reading.
6. Read at most `expected_size + 1` bytes in bounded chunks while hashing. A
   file that grows is rejected without an unbounded read.
7. Recheck handle identity/type/size after the read, require the byte count to
   equal the snapshot size, and require the raw SHA-256 to equal the snapshot
   hash.
8. Decode the complete bytes and build canonical lines only after every raw
   consistency check passes.

Any missing file, type change, identity change, size change, or hash mismatch
raises `FileChangedError` for that portable path and aborts the entire build.
There is no partial/best-effort package. A same-size in-place rewrite is caught
by SHA-256. A rewrite that restores exactly the scanned bytes is equivalent to
the scanned input and is accepted.

This is a read-after-scan consistency guarantee, not a filesystem transaction:
files can still change after they have been verified. The package remains
self-contained and records the verified raw source hash.

### Text decoding

- Decode the entire verified byte sequence as strict UTF-8.
- If the file begins with the UTF-8 BOM bytes `EF BB BF`, accept and remove that
  single BOM. An embedded U+FEFF remains content.
- Invalid UTF-8 anywhere in the file raises `TextDecodingError` and aborts the
  build. This is necessary because the scanner currently classifies only an
  initial sample and can therefore inventory a file with invalid bytes later.
- Do not fall back to a locale codec, replacement characters, Latin-1, or
  encoding detection.
- Normalize CRLF and lone CR to LF before range extraction and package hashing.
  Preserve all other Unicode code points and preserve whether the content ends
  in LF.
- Source size/hash always describe raw bytes; block size/hash describe canonical
  BOM-free LF-normalized UTF-8 bytes.

## 12. Line-range contract

### CLI syntax

`--include-lines PATH:START-END` (alias `--lines`) is repeatable. The parser
splits at the final colon
whose suffix matches two decimal integers separated by `-`, so a legal POSIX
filename containing an earlier colon remains addressable. Examples:

```text
--include-lines pyproject.toml:1-20
--include-lines README.md:1-5
```

Rules:

- line numbers are one-based and inclusive;
- both bounds are required; single-number, open-ended, zero, negative,
  reversed, non-decimal, and overflowed values are invalid;
- the path is an exact portable path, not a glob;
- the target must remain selected after exclusions;
- ranges are validated against the fully decoded canonical source line count;
- an empty file cannot have a range; and
- line boundaries are LF after newline normalization. A trailing LF does not
  create a phantom additional line.

### Canonicalization

Requests for the same path are sorted by `(start, end)`. Exact duplicates,
overlaps, and adjacent ranges are merged into their inclusive union. Thus
`1-3`, `3-5`, and `6-8` become `1-8`. This makes equivalent range sets produce
identical packages and avoids duplicated content.

When no range targets a selected file, the whole canonical file is included as
one full block. When ranges target it, only the canonical ranged blocks are
included, in ascending line order. Non-contiguous blocks remain distinct in the
model and Markdown so the output never implies that separated source lines were
adjacent.

## 13. ContextPackage contract

- `ContextPackage` is the sole renderer input and the sole successful JSON
  inspection result.
- `schema_version` is part of the model, not a CLI envelope around it.
- `tree` represents all selectable files in the source snapshot, even when only
  a subset has content in `files`. This gives portable project orientation
  without exposing ignored/skipped entries or the absolute root.
- `files` are unique and sorted by path. Every file path must occur as a file
  entry in `tree`.
- `blocks` follow the full/ranged invariants in section 7 and are in ascending
  line order.
- Statistics are stored for convenient consumers but are not trusted on input;
  inspection recomputes and compares every statistic.
- Language counts count selected files with a non-null language, not blocks.
- `selected_source_bytes` sums full raw source sizes even for ranged files.
- `included_content_bytes` and `included_line_count` sum included blocks after
  canonical decoding/ranging.
- No root path, scan timestamp, file modification time, host OS, username,
  hostname, random ID, model, provider, token count, or prompt field is stored.
- Title is explicit package metadata. Its default is the constant
  `"Context package"`, not the local directory name. It must be non-empty after
  trimming and cannot contain NUL or ASCII control characters other than tab.
- Model/provider-specific metadata is forbidden rather than accepted as an
  unstructured extension map in schema version 1.

Determinism applies to successful output. Different source bytes, explicit
titles, selectors, ranges, or limit values are different inputs. Selector order
and redundant selectors/ranges are canonicalized and do not change output.

## 14. Markdown format

The renderer emits these sections in exactly this order:

````text
# <title>

Schema version: 1

## Project tree

```text
<ASCII tree>
```

## Statistics

- Selectable files: ...
- Selected files: ...
- Ranged files: ...
- Selected source bytes: ...
- Included content bytes: ...
- Included lines: ...

## Files

### <portable path as a safe code span>

- Language: ...
- Source bytes: ...
- Source SHA-256: ...
- Included lines: all | START-END[, ...]

<one fenced block for a full file, or one labeled fenced block per range>
````

The illustrative outer fence above is not literal output. Normative details:

- Headings and labels are fixed English text in v0.3.
- The title is Markdown-escaped. Paths use a code-span delimiter one backtick
  longer than the longest run in the path, with CommonMark padding when needed.
- Content fences use backticks. For each block, the opening/closing fence length
  is `max(3, longest consecutive backtick run in content + 1)`. Therefore source
  content can never close its own fence.
- No language info string is attached in v0.3. Existing language labels such as
  `C++` and `Git ignore` are presentation labels, not a safe standardized fence
  vocabulary.
- Empty blocks still have an opening line, an empty content line, and a closing
  line.
- If block text lacks a final LF, the renderer adds a separator LF before the
  closing fence without changing the block stored in the model.
- File order, range order, language statistics, and tree order are canonical.
- Output uses LF and ends in exactly one LF.
- The Markdown renderer receives no absolute path and cannot print one.

## 15. JSON schema

JSON output is UTF-8 without a BOM, indented by two spaces, uses
`ensure_ascii=False`, sorts object keys lexicographically, uses LF, and ends in
exactly one LF. Arrays retain canonical domain order. No NaN/infinity values are
possible. These rules make serialization byte-deterministic.

The normative schema is Pydantic-generated JSON Schema (draft 2020-12) for the
models in section 7, with these additional pinned constraints:

| Location | Type and constraints |
| --- | --- |
| root | object; required `schema_version`, `title`, `tree`, `files`, `statistics`; no additional properties |
| `schema_version` | integer constant `1` |
| `title` | non-empty string with the domain control-character validation |
| `tree` | object with canonical `entries`, `file_count`, `directory_count`; no additional properties |
| `tree.entries[]` | object with portable `path` and enum `kind`; no additional properties |
| `files` | array of unique-by-path, path-sorted `ContextFile` objects |
| `files[].source_sha256` | string matching `^[0-9a-f]{64}$` |
| `files[].selection` | enum `full`, `ranges` |
| `files[].blocks[]` | object containing nullable bounds, text, counts, byte size, and canonical-text SHA-256 |
| all sizes/counts | integer, minimum 0 (limits are positive where supplied as options) |
| `statistics.languages` | object from language string to non-negative integer; keys emitted sorted |

Representative shape:

```json
{
  "files": [
    {
      "blocks": [
        {
          "end_line": 2,
          "line_count": 2,
          "sha256": "<64 lowercase hex characters>",
          "size_bytes": 23,
          "start_line": 1,
          "text": "print('hello')\npass\n"
        }
      ],
      "included_content_bytes": 23,
      "included_line_count": 2,
      "language": "Python",
      "path": "src/app.py",
      "selection": "ranges",
      "source_line_count": 20,
      "source_sha256": "<64 lowercase hex characters>",
      "source_size_bytes": 200
    }
  ],
  "schema_version": 1,
  "statistics": {
    "included_content_bytes": 23,
    "included_line_count": 2,
    "languages": {"Python": 1},
    "ranged_file_count": 1,
    "selected_file_count": 1,
    "selected_source_bytes": 200,
    "tree_directory_count": 1,
    "tree_file_count": 1
  },
  "title": "Context package",
  "tree": {
    "directory_count": 1,
    "entries": [
      {"kind": "directory", "path": "src"},
      {"kind": "file", "path": "src/app.py"}
    ],
    "file_count": 1
  }
}
```

The example hash/size placeholders are explanatory, not a validation fixture.
Tests use real values.

Schema version policy:

- Version 1 is an integer constant and unknown versions fail with
  `UnsupportedSchemaVersionError`; they are never guessed or coerced.
- Backward-compatible additions within v1 are avoided because
  `additionalProperties` is false. A shape change requires schema version 2.
- A future reader may explicitly dispatch old versions to dedicated models;
  migration is not part of v0.3.
- The JSON package schema is independent of the existing scan report schema 1;
  sharing the integer does not imply the shapes are interchangeable.

## 16. Size-limit policy

Defaults:

```text
maximum selected files              100
maximum included canonical content  1,000,000 bytes
maximum JSON package input           10,000,000 bytes
```

- File-count is checked after include/exclude resolution and deduplication but
  before any content read.
- Total content is the sum of `ContextBlock.text.encode("utf-8")` lengths after
  decoding, newline normalization, and line-range selection. Metadata and tree
  bytes are not counted in this domain limit.
- The builder accumulates content in canonical file order and fails as soon as
  the total would exceed the limit. It returns no partial package.
- Exact equality with a limit succeeds; one byte/file over fails.
- `--max-files` and `--max-total-size` may lower or raise positive builder
  limits. Zero and negative values are usage errors.
- A selected ranged file counts as one file even if it has multiple blocks.
- Full source bytes still must fit the scanner's per-file maximum because only
  `ProjectSnapshot.files` can be selected. Ranges reduce package content, not
  the bytes needed for verification.
- JSON inspection checks the input file's non-followed size before reading,
  bounds the read at one byte beyond 10,000,000, and rejects a larger package.
- JSON rendering also rejects output above 10,000,000 bytes so ContextForge
  never creates a JSON package its default inspector refuses. Markdown is
  governed by selected content limits but can be larger due to metadata/tree.

The defaults are safety and usability policy, not token budgets. No tokenizer
is introduced.

## 17. Error model

Core code raises typed exceptions and never calls `typer.Exit`:

```text
ContextPackageError
  InvalidSelectorError
  SelectorNoMatchError
  NoFilesSelectedError
  InvalidLineRangeError
  LineRangeTargetError
  LineRangeBoundsError
  FileChangedError
  TextDecodingError
  ContextLimitError
  ContextRenderError
  PackageReadError
  PackageValidationError
  UnsupportedSchemaVersionError
```

Requirements:

- Messages identify the portable path or selector and stable reason, but do not
  include the absolute snapshot root by default.
- Validation errors are deterministic and do not contain traceback text in
  normal CLI output.
- Operational `OSError` details are chained for debugging, while public CLI
  messages avoid secrets and machine-local paths where a portable path exists.
- Expected errors abort before output publication. Unexpected programming
  errors are not broadly swallowed as successful command failures.
- Package inspection distinguishes invalid UTF-8, invalid JSON, duplicate JSON
  keys, unsupported schema, shape errors, and semantic invariant errors.

## 18. CLI contract

### Project tree

```text
contextforge tree [PATH] [--depth INTEGER]
```

- `PATH` defaults to `.`.
- The command scans with existing default `ScanOptions`, builds a full tree from
  the resulting snapshot, and prints the ASCII renderer.
- It does not duplicate traversal or selection logic and does not expose an
  output file option in v0.3.

### Context creation

```text
contextforge context create [PATH]
  [--include PATH]...
  [--directory PATH]...
  [--glob PATTERN]...
  [--exclude PATTERN]...
  [--include-lines PATH:START-END]...
  [--task TEXT]
  [--include-tree | --no-include-tree]
  [--format markdown|json]
  [--output PATH]
  [--force]
  [--max-files INTEGER]
  [--max-context-bytes INTEGER]
```

- `PATH` defaults to `.`, format defaults to `markdown`, task defaults to the
  constant `Context package`, and builder limits use section 16 defaults.
- `--include` is the exact-path selector and retains `--file` as an alias;
  directory and glob selectors stay explicit because the core does not guess a
  selector's kind. `--include-lines` retains `--lines` as an alias.
- Executable entry points call the shared typed `run()` wrapper, which invokes
  Typer with `windows_expand_args=False`. This prevents Click from expanding
  GitWildMatch values into working-directory paths on Windows; ContextForge's
  selector processing remains unchanged.
- With no include options, all selectable snapshot files are initially
  selected. Users can use exclusions alone.
- `--include-tree` is enabled by default and can be disabled without changing
  file selection. `--max-context-bytes` maps to the builder's included
  canonical-content byte limit.
- The output extension does not infer or override format.
- Without `--output`, the exact rendered package goes to stdout.
- With `--output`, stdout reports the resolved destination only after atomic
  publication; package content is not also printed.
- The destination is not part of the scan because creation and complete
  rendering happen before the output write.

### Context inspection

```text
contextforge context inspect PACKAGE
```

- `PACKAGE` is JSON only in v0.3.
- The command performs bounded strict parsing and full structural/semantic
  validation without accessing a repository or network.
- Success prints a concise deterministic summary: schema version, title,
  selectable tree files, selected files, ranged files, included bytes, included
  lines, and sorted language counts.
- Invalid packages print one clear error to stderr and no success summary.
- Inspection never rewrites, repairs, reformats, or upgrades the package.

### Exit codes

Global policy remains compatible with `scan`:

| Code | Meaning |
| --- | --- |
| 0 | command completed successfully |
| 1 | operational/read/write failure, changed selected file, decoding failure, render failure, or inspected package invalid |
| 2 | invalid CLI usage, root, selector, range, format, depth, or limit |
| 3 | reserved for the existing completed `scan --fail-on-error` behavior |

Typer parsing errors naturally use 2. An existing package that is syntactically
addressable but invalid uses 1. A missing `PACKAGE` argument/path is usage 2;
an unreadable existing file is operational 1.

### Output overwrite and atomicity

- Parent directories must already exist and are never created implicitly.
- Existing destinations are refused by default. `--force` atomically replaces
  an existing non-directory entry; directories remain invalid destinations.
- Fully render and enforce render limits before opening a temporary file.
- Reuse the existing sibling-temp, UTF-8, LF, flush, `fsync`, hard-link publish,
  racing-destination refusal, and cleanup policy.
- A failed scan, selection, read, decode, range, validation, render, or publish
  leaves no destination and no temporary artifact.

## 19. Detailed test matrix

All tests use the top-level `tests/` directory. All existing test files and
their substantive assertions are retained; scanning and scan CLI behavior must
continue to pass unchanged. No test accesses the network. Filesystem tests use
`tmp_path` and must not scan the developer's actual ContextForge checkout.

### Project tree

- Empty snapshot; one root file; nested files; Unicode names.
- Synthesized directories only, ignored/skipped paths absent.
- Directories before files and case-sensitive code-point ordering.
- Input file order permutations produce an identical model/render.
- Unlimited, depth 0, depth 1, exact deepest depth, excessive depth, and
  negative-depth failure.
- POSIX separators when tests provide Windows-style serialized paths.
- Duplicate/colliding tree entry validation failures.

### Selector success and precedence

- Exact root and nested path.
- Recursive directory and special `.` directory.
- `*.py`, `src/**/*.py`, `?`, bracket class, and Unicode globs.
- Includes union regardless of argument order.
- Exclusion beats exact, directory, and glob includes regardless of order.
- Exclusion-only request starts from all files.
- Duplicate selectors and multiply matched files appear once.
- Case-sensitive results are the same under simulated Windows and POSIX paths.
- Selector permutations produce byte-identical packages.

### Selector failure and path safety

- Unmatched exact path, directory, and include glob each identify the selector.
- Unmatched exclusion succeeds.
- Final empty selection fails.
- Reject empty/NUL, POSIX absolute, backslash-rooted, UNC, Windows
  drive-absolute, Windows drive-relative, and every traversal form.
- Reject `..` even in `safe/../file.py` and in glob patterns.
- Reject leading-negation glob and malformed pattern.
- Files created after scanning and ignored/skipped snapshot paths cannot be
  selected and are never opened.
- Case-only distinct snapshot paths remain distinct on a case-sensitive test
  model; wrong-case selectors fail universally.

### Safe reader and race conditions

- Exact raw size/hash verification success and bounded chunk reads.
- File missing after scan; replaced before open; symlink substitution;
  directory substitution; pre-open/open-handle identity mismatch.
- File grows before read, during first chunk, during later chunk, and after
  read; file shrinks; same-size rewrite; hash mismatch.
- Read/open/stat/fstat permission and generic `OSError` failures.
- One failing selected file aborts the complete package with no output.
- Existing scanner `inspect_file()` tests remain green after safety extraction.
- Symlink/junction behavior has portable monkeypatched coverage and real
  platform coverage where available.

### Decoding and canonical text

- ASCII, multibyte UTF-8, empty content, UTF-8 BOM, embedded U+FEFF.
- Invalid UTF-8 at start, after the scanner sample boundary, and at EOF.
- CRLF, lone CR, LF, mixed endings, trailing newline, and no trailing newline.
- Raw source hash/size remain pre-normalization; block hash/size are canonical.
- No replacement or locale fallback branch exists.

### Line ranges

- One line, whole file, first/last line, disjoint ranges, and multiple files.
- Filename containing a colon parsed using the final range delimiter.
- Duplicate, overlapping, nested, and adjacent ranges canonicalize identically.
- Preserve final-LF status in extracted block text.
- Reject missing bounds, zero, negative, reversed, non-decimal, overflow,
  beyond-EOF, empty-file, absent, unselected, and excluded targets.
- Ranged blocks and counts/hashes validate on JSON inspection.

### Package model and statistics

- Full and ranged file invariants; empty full file.
- Unique sorted files; selected path must occur in tree.
- Recompute every count, byte total, line total, ranged count, and sorted
  language count.
- Selected raw bytes versus included canonical bytes, including BOM/newline and
  ranged cases.
- Frozen models and `extra="forbid"` at every level.
- Reject invalid hashes, negative values, contradictory selection/block bounds,
  duplicate paths, noncanonical ordering, and absolute/local paths.
- Assert package dumps contain no snapshot root or other absolute temp path.

### Limits and boundaries

- Zero/negative limits rejected; limit 1; exactly at and one over both limits.
- Deduplication occurs before file-count enforcement.
- Ranged content uses included bytes for total-size enforcement.
- Multibyte UTF-8 counts bytes, not Python characters.
- Failure while accumulating leaves no package/output.
- JSON input exactly 10,000,000 bytes versus one byte over, using bounded reads.
- JSON rendering at versus over the inspection-compatible maximum.

### Markdown rendering

- Exact section and file ordering, LF, and one final newline.
- Empty package blocks, empty content, final/no-final source LF.
- Content with 1, 3, and long runs of backticks cannot escape the chosen fence.
- Path backticks and Markdown metacharacters cannot alter heading structure.
- Full file versus individually labeled disjoint range blocks.
- No language info-string injection and no absolute root leakage.
- Golden outputs are small inline literals, not developer-repository snapshots.

### JSON rendering and inspection

- Exact deterministic golden JSON and repeat serialization.
- UTF-8 Unicode remains unescaped; CRLF never emitted; final LF present.
- Different construction/selector orders serialize identically.
- Valid full/ranged/empty packages inspect successfully.
- Invalid UTF-8, BOM policy for input, empty/truncated JSON, duplicate object
  keys, trailing data, wrong root type, missing/extra fields, invalid enums,
  unsupported schema versions, and semantic statistic/hash tampering fail.
- Inspector does not open package-internal paths; monkeypatch filesystem opens
  to prove only `PACKAGE` is read.
- Inspection performs no writes or repairs.

JSON input should accept a single leading UTF-8 BOM for practical portability,
then parse strictly; the canonical renderer never emits a BOM.

### CLI

- Root help lists `tree` and `context`; existing version/doctor/scan tests pass.
- Help documents every option and repeatability.
- Default paths are exercised only after `monkeypatch.chdir(tmp_path)`.
- Tree success/depth/invalid root and no absolute path in package output.
- Create defaults, every selector type, exclusions, ranges, both formats,
  Unicode, stdout, and output file.
- Existing destination, missing parent, publish race, temporary creation/write
  failure, and cleanup.
- Create maps usage errors to 2, operational/consistency errors to 1, and never
  emits a traceback for expected failures.
- Inspect valid summary, every invalid package class, missing/unreadable input,
  and no repository access.
- Unexpected internal exceptions are not reported as success.

### Required validation gates

Run, without network access:

```text
ruff check .
ruff format --check .
mypy
pytest
git diff --check
```

Pytest continues to run with branch coverage and `--cov-fail-under=90`. Overall
branch coverage must remain at least 90%. New deterministic core modules should
have complete practical branch coverage: every meaningful success, validation,
failure, race, and boundary branch is covered; only genuinely platform-
unavailable branches may be skipped with a portable simulated equivalent.

## 20. Implementation sequence

Each step should be independently reviewable and keep the test suite green.

1. Pin the v0.3 contracts in tests for paths, tree order/depth, selectors,
   ranges, package models, and JSON shape. Do not change dependencies.
2. Extract the stable binary read and atomic publisher into
   `contextforge.filesystem`, retaining compatibility imports and all existing
   scan tests.
3. Replace the context placeholder with the immutable package/tree/range models
   and their local/cross-field validators. Preserve the existing immutability
   test while extending it to the completed model.
4. Implement snapshot-only project-tree construction and ASCII rendering.
5. Implement selector normalization, exact/directory/glob resolution,
   precedence, deduplication, unmatched behavior, and range parsing/
   canonicalization.
6. Implement verified raw reads, strict decoding, newline canonicalization,
   line indexing, and ranged block extraction.
7. Implement the builder, statistics recomputation, and file/content limits.
8. Implement Markdown/JSON renderers, dynamic fence/code-span escaping, and
   deterministic serialization.
9. Implement bounded duplicate-key-aware JSON inspection and semantic
   validation.
10. Add the thin Typer `tree`, `context create`, and `context inspect` adapters,
    reusing atomic output and the established error style.
11. Run focused tests after every module, then all 166+ top-level tests, Ruff,
    strict mypy, branch coverage, and `git diff --check` with no network.
12. Only after implementation is accepted, update README, changelog, roadmap,
    security notes, and release metadata as a separate release/documentation
    step. Do not mix provider or v0.4 work into v0.3.

## 21. Completion criteria

The milestone is complete only when:

- a `ProjectSnapshot` deterministically produces the specified portable tree;
- every selector, precedence, duplicate, unmatched, case, and path-safety rule
  is implemented exactly as documented;
- selected content is snapshot-authorized, identity/size/hash verified, strict
  UTF-8 decoded, and optionally range-limited;
- any post-scan inconsistency aborts without a partial package or output;
- the canonical immutable `ContextPackage` and statistics validate all stated
  invariants;
- Markdown and JSON are pure deterministic renderers of that model and cannot
  leak an absolute root;
- JSON schema version 1 is pinned and valid packages can be strictly inspected
  offline without repository access;
- limits and race-safe no-overwrite output work at exact boundaries;
- `contextforge tree`, `contextforge context create`, and
  `contextforge context inspect` are thin adapters with the specified exit
  codes;
- all existing scan behavior and all existing tests remain preserved;
- all new tests use `tmp_path`, no test scans the developer repository, and no
  test uses the network;
- overall branch coverage is at least 90% and new deterministic core modules
  have complete practical coverage; and
- Ruff, Ruff format check, strict mypy, pytest with branch coverage, and
  `git diff --check` all pass.

## 22. Risks and known limitations

- **The snapshot is not a transaction.** Per-file verification detects changes
  during each selected read, but another file may change after it was read. The
  package hashes prove what was included; a future atomic multi-file snapshot is
  out of scope.
- **Scanner text classification samples only the beginning.** Full strict decode
  can reject a file that v0.2 labeled text. This is intentional fail-closed
  behavior, not a scanner compatibility change.
- **Unicode normalization is not performed.** NFC/NFD-distinct filenames remain
  distinct. This preserves exact repository names but visually similar paths
  can coexist on some systems.
- **Case policy may surprise Windows users.** Universal case-sensitive matching
  is required for portable deterministic results; the CLI should report the
  unmatched selector clearly.
- **GitWildMatch is not shell globbing.** Patterns are matched to portable
  snapshot paths, never expanded by the shell or filesystem. Documentation and
  help examples must make the no-slash versus rooted-slash behavior clear.
- **Newline normalization changes rendered content bytes.** Raw source hashes
  retain file identity while block hashes identify canonical package text.
- **Ranges require reading the full file.** They reduce exported content, not
  verification I/O. The existing scanner per-file maximum bounds that work.
- **The full selectable tree can dominate package metadata.** Selected-content
  limits do not count tree bytes. JSON's 10 MB render/input ceiling fails safely
  for exceptionally large inventories; tree filtering/compression is not in
  v0.3.
- **Markdown is reviewable, not reversible.** Only JSON has a strict schema and
  inspection contract.
- **Hard-link publication can be unsupported on unusual filesystems.** Existing
  behavior is to fail safely without overwriting or leaving a partial file;
  weakening atomic no-overwrite semantics is not acceptable.
- **Schema v1 is intentionally closed.** Provider metadata, timestamps, and
  arbitrary extensions require a future explicit schema decision rather than
  silently changing deterministic output.

No unresolved architectural decision is required to begin implementation. The
defaults and policies above are deliberate v0.3 decisions; changing selector
defaults, case sensitivity, range syntax, limits, tree contents, or overwrite
behavior should be approved as a contract change before production work starts.
