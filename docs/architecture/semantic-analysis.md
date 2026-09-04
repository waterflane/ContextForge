# Incremental semantic analysis

ContextForge can enrich a current deterministic CodeMap generation with bounded
file and symbol interpretations from the configured structured model provider.
Semantic records are model interpretations, not verified facts. Source code is
authoritative; an interpretation never replaces a path, signature, symbol,
relationship, source range, or other deterministic CodeMap fact.

This component analyzes files and symbols only. It does not perform global
repository discovery, architecture or feature mapping, context selection, MCP,
or final prompt compilation.

## Local model configuration

The default `.contextforge/config.toml` model configuration targets Ollama on
the loopback interface:

```toml
[models]
provider = "ollama"
endpoint = "http://127.0.0.1:11434/api/chat"
model = "qwen2.5-coder:7b"
timeout_seconds = 360
connect_timeout_seconds = 10
read_timeout_seconds = 300
operation_timeout_seconds = 360
context_window = 4096
context_safety_margin = 256
max_response_bytes = 1000000
concurrency_limit = 2
retry_limit = 2
semantic_max_output_tokens = 512
reasoning_effort = "off"
local_only = true
external_data_policy = "deny"
store_raw_prompts = false
store_raw_responses = false
```

The caller constructs an `OllamaModelProvider` from these values, builds or
loads a current structural index, and passes the provider to
`build_semantic_index()`. Live-provider tests are optional; the normal test
suite uses `FakeModelProvider` and requires no model or network.

Keep local-model concurrency low. The semantic builder additionally bounds
scheduled files, simultaneous file tasks, request and response bytes, source
bytes per chunk, up to 64 logical model requests per file, provider retries,
and cancellation. Full coverage is the default; lower caller limits are explicit.
Provider limits may be stricter than analysis limits.

`--request-timeout` overrides the per-attempt deadline for one index command;
`--max-output-tokens` overrides the bounded semantic response budget. The
`--context-window` option overrides the configured loaded-model limit. The
default retry limit is two retries after the first attempt. Attempt elapsed time
resets on retry, while total operation elapsed time remains monotonic.

## Semantic routing and planning

The complete semantic work plan is classified before its denominator is
reported. Each candidate has exactly one route: rich model analysis, generic
model analysis, deterministic metadata summary, reusable record, skipped,
unsupported binary, oversized, invalid encoding, or preflight failure.
`.contextforge` paths never enter this plan.

Python plus JavaScript/JSX, TypeScript/TSX, Java, C#, Go, Rust, C, C++, PHP,
and Ruby use rich model analysis over verified declarations. Python retains its
standard-library AST extractor; the other languages use bundled Tree-sitter
grammars and require no runtime download. Routing is based on declarations in
each supplied chunk: chunks without declarations use generic region analysis,
including chunks in otherwise supported languages.

`.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, `.env.sample`,
lock files, `.gitkeep`, and empty files use deterministic metadata summaries and
make no provider call. Environment templates persist declared variable names
only; assigned values are neither placed in semantic records nor progress.
Secret-bearing `.env` files are skipped. Deterministic metadata work has one
cost unit; a model-routed file has eight base units plus one unit per 32 KiB of
source, capped at 16 source units. Thus model work dominates overall progress
without pretending that reused or deterministic items made an LLM request.

## Input and trust boundary

Every per-file request contains only a compact system instruction, normalized
path, language and category, bounded source or excerpt, minimal file-local
facts, and a compact closed response schema. It never contains the repository
tree, global maps, feature maps, unrelated files, or prior responses.

Chunks contain at most 65,536 UTF-8 bytes and follow verified symbol boundaries,
including source between symbols. Oversized regions split on lines with up to
eight overlapping lines. Oversized individual lines split at UTF-8 boundaries
and retain byte-column coordinates. Small neighboring regions share a request.
The provider context budget may require smaller chunks. At most 64 chunks are
processed in source order; the cap never silently implies full coverage.

Successful chunks are checkpointed with source SHA, range, fact digest,
provider/model/prompt identity and analysis options. Published partial results
retain checkpoints, so a subsequent run only requests missing chunks. Claims
are merged deterministically without another synthesis request; conflicting
interpretations retain provenance. Overlapping inferred regions keep the first
valid region and emit a warning. Internal checkpoints are not tool observations.

Adaptive output caps per request, also
limited by the caller's lower ceiling, are:

- deterministic metadata/control files: no provider output;
- LICENSE: 128 tokens;
- small README, Markdown, TXT, and configuration: 160 tokens, or 192 for a
  larger document;
- generic source: 192 tokens when small, otherwise 256;
- rich symbol analysis: 256 for trivial files, 320 for normal files, and at
  most 512 for large or structurally complex files.

README requests only project purpose, entry points, setup, and major
components. LICENSE requests only type, obligations, and restrictions; common
license markers are detected deterministically and supplied as a compact fact.
JSON/configuration requests only summary, sections, and important keys. Arrays
and strings are schema-bounded and responses may not quote source.

## Records and evidence

`FileSemanticAnalysis` and nested `SymbolSemanticAnalysis` records are stored
as `*.interpretation.json`, physically separate from `*.facts.json`. Each
accepted claim includes its text, confidence and rationale, available verified
source ranges and fact IDs, prompt version, provider ID, model ID, and source
SHA-256. Unknown symbols, facts, stale hashes, invalid ranges, unknown fields,
malformed JSON, non-finite confidence, and oversized responses are rejected.
Symbol evidence must also fall within that symbol's verified declaration range,
including when a small-file response analyzes all symbols in one request.

For an unsupported language or meaningful file without verified declarations,
the generic model may additionally return `InferredRegionRecord` values with a
label, kind, summary, confidence, and source range. These remain model-derived
and are never promoted to `SymbolRecord`. Ranges must be ordered, non-overlapping,
inside the supplied excerpt, and bound to the current source SHA-256.

A completed chunk is checkpointed atomically in staging only after
the entire response validates. File records expose `chunks_planned`,
`chunks_completed`, `covered_ranges`, `coverage_complete`, and safe coverage
warnings. A file with successful and failed chunks retains successful claims
but reports `semantic_status=partial` and incomplete coverage. Publication copies structural facts unchanged,
binds interpretation digests into a new immutable generation, and switches the
active pointer atomically. A failed or cancelled run cannot expose a partial
record as complete.

## Incremental updates and failures

A complete record is reused only when the source hash and size, language,
CodeMap record digest and analyzer, semantic schema, semantic analyzer and
prompt, provider/model identity, and relevant analysis-option digest all
match. Modification time alone does not matter. New and changed files are
analyzed; deleted files disappear from the next generation. A rename is
handled safely as deletion plus addition because paths participate in IDs and
evidence, so its semantics are reanalyzed rather than silently rebound.

The build lifecycle distinguishes `pending`, `analyzing`, `complete`, `partial`, `failed`,
`stale`, `skipped`, and `disabled`. Only terminal states are published in a
manifest. By default, individual failures are recorded and other files
continue; strict mode refuses semantic publication on any failure.
Validated staging checkpoints can resume an interrupted run, while failed
records are retried on a later run.

## Privacy

Even a local prompt contains repository source and may contain secrets. Treat
semantic records and any retained diagnostics as sensitive repository data.
Raw prompt and response retention is off by default. Loopback Ollama keeps the
provider path local, but users remain responsible for the model process and
its retention policy. Sending repository content to a non-loopback provider
requires `local_only=false` and `external_data_policy="allow_repository"`.
`allow_selected` does not authorize remote transport in this release. Repository-wide
authorization can include secret-like selectable files; ContextForge does not
claim complete secret detection, so ignore rules and provider retention must be
reviewed first.
