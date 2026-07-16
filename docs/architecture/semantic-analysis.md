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
timeout_seconds = 120
max_response_bytes = 1000000
concurrency_limit = 2
retry_limit = 2
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
bytes per request, output tokens, chunks per file, provider retries, and
cancellation. Provider limits may be stricter than analysis limits.

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
its retention policy. Sending repository content to an external provider
requires a separate explicit external-data policy and acknowledgement; this
file-analysis component does not grant that permission automatically.
