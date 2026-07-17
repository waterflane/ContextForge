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
timeout_seconds = 90
max_response_bytes = 1000000
concurrency_limit = 2
retry_limit = 2
semantic_max_output_tokens = 4096
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
bytes per request, output tokens, chunks and model requests per file, provider retries, and
cancellation. Provider limits may be stricter than analysis limits.

`--request-timeout` overrides the per-attempt deadline for one index command;
`--max-output-tokens` overrides the bounded semantic response budget. The
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
shell, and other text files use generic schema-bound model analysis even when
their structural CodeMap uses the unsupported-language fallback. Generic model
success is recorded as `generic_model_analysis`, never as a semantic fallback.

`.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, `.env.sample`,
lock files, `.gitkeep`, and empty files use deterministic metadata summaries and
make no provider call. Environment templates persist declared variable names
only; assigned values are neither placed in semantic records nor progress.
Secret-bearing `.env` files are skipped. Deterministic metadata work has one
cost unit; a model-routed file has eight base units plus one unit per 32 KiB of
source, capped at 16 source units. Thus model work dominates overall progress
without pretending that reused or deterministic items made an LLM request.

## Input and trust boundary

Every request contains only one bounded file, symbol, or deterministic chunk;
its trusted portable path and language; relevant verified CodeMap facts; and a
closed response schema. Repository source and comments are framed as untrusted
data. The model has no filesystem, network, Git, shell, command-execution,
write, discovery, or MCP tools, and cannot select another path or expand a
budget.

For a large file, ContextForge covers the complete canonical source with
deterministic bounded chunks, separately analyzes verified functions, methods,
and classes, and synthesizes the file view from validated chunk and symbol
analyses. Those prior model interpretations remain in an explicitly untrusted
context envelope during synthesis; they are not relabeled as CodeMap facts.
Synthesized evidence must match evidence already accepted from a bounded chunk
or symbol response. Exceeding a chunk or request bound is an explicit failed
analysis; source is never silently truncated and reported as successful.

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
`allow_selected` does not authorize remote transport in v0.4.0. Repository-wide
authorization can include secret-like selectable files; ContextForge does not
claim complete secret detection, so ignore rules and provider retention must be
reviewed first.
