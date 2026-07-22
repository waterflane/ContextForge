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
model = "qwen2.5-coder"
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
bytes per file, one model request per file, provider retries, and cancellation.
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

Python files use rich model analysis. Meaningful readable JavaScript/JSX,
TypeScript/TSX, Markdown, HTML, CSS, PowerShell, batch, JSON, TOML, YAML, XML,
shell, and other text files use generic schema-bound model analysis without a
rich structural extractor. Their placeholder uses `generic-text-structure`
with reason `no_structural_extractor`; successful semantics use the distinct
`generic-text-semantic` identity and `generic_model_analysis` route, never an
unsupported-language fallback.

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

The maximum candidate excerpt is 65,536 UTF-8 bytes, but it is not a dispatch
target. Smaller files are sent completely only when the complete request fits.
Larger or over-budget requests use a deterministic line-preserving selection weighted
toward the beginning, verified declarations, and ending. Selection works on
decoded text and whole encoded lines, with a codepoint-safe prefix fallback, so
it cannot create invalid UTF-8. Structural metadata is reduced after source
when necessary. Every resulting request must fit messages, schema, output,
wrapper, and safety reserve within the configured model context. Progress
records the cost breakdown and truncation state without exposing source.

Each file makes at most one provider request. Adaptive output caps, also
limited by the caller's lower ceiling, are:

- deterministic metadata/control files: no provider output;
- LICENSE: 128 tokens;
- small README, Markdown, TXT, and configuration: 160 tokens, or 192 for a
  larger document;
- generic source: 192 tokens when small, otherwise 256;
- Python rich analysis: 256 for trivial files, 320 for normal files, and at
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

A completed interpretation is checkpointed atomically in staging only after
the entire response validates. Publication copies structural facts unchanged,
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

The build lifecycle distinguishes `pending`, `analyzing`, `complete`, `failed`,
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
