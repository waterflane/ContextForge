# Model-provider foundation

## Implemented boundary

ContextForge has a provider-independent foundation for bounded, schema-bound
model analysis. It defines the request/response/configuration contracts, a
deterministic scripted fake, and a local Ollama adapter. The incremental file
and symbol semantic builder, repository maps, task discovery, and optional task
refinement compose this foundation. Providers do **not** receive tools, mutate
source, execute compiled prompts, or acquire arbitrary network access through
the read-only MCP server.

Provider output is interpretation input, never a verified CodeMap fact. The
canonical repository-intelligence index remains independent of request
durations, retry counts, token usage, and other operational diagnostics.

## Public contract

`contextforge.models` exports:

- `ModelProvider`, the small asynchronous adapter protocol;
- `ModelRequest`, `ModelResponse`, and `ModelUsage`;
- `ProviderCapabilities`, `ProviderConfiguration`, and `ProviderDiagnostic`;
- `StructuredResponseError`, `ProviderTimeoutError`,
  `ProviderUnavailableError`, and `ProviderCancelledError`;
- `FakeModelProvider` for deterministic offline tests;
- `OllamaModelProvider` for Ollama's local non-streaming chat endpoint; and
- `classify_retry()` and `parse_structured_response()` for shared policy.

An adapter supplies raw response text and optional usage/finish metadata to
the shared runtime. The runtime—not provider-specific response shapes—owns the
concurrency bound, deadline, cancellation race, finite retry loop, response
byte limit, strict parsing, schema validation, path authorization, canonical
normalization, and diagnostics. A future external adapter implements the same
protocol and may compose the same runtime; no provider hierarchy, SDK, or
private coding-agent API is required.

## Configuration

The default `.contextforge/config.toml` model section documents the local
policy:

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

`ProviderConfiguration` enforces a timeout in `(0, 600]`, a response cap in
`[1, 16,000,000]`, concurrency in `[1, 8]`, and at most two retries after the
first attempt. An individual request may lower its response cap. Ollama's
`local_only=true` policy accepts only `127.0.0.1`, `::1`, or `localhost`.

Credentials are optional and indirect. `credential_env` names an environment
variable; the configuration stores only that name and resolves its value at
request time. Endpoint user-info is rejected. There is no configuration field
for an API key, bearer value, password, or arbitrary headers. Loaded values are
wrapped as secrets, omitted from diagnostics, and redacted if a typed adapter
error contains one. Raw provider exceptions are translated to stable errors
without their text. Secrets are not fields in index models and are never
written to `.contextforge/index`, contexts, or run artifacts by this layer.

## Structured response behavior

Every `ModelRequest` names a closed Pydantic response model with integer
`schema_version = 1`. Its generated JSON Schema is passed to providers that
support native structured output. ContextForge then independently:

1. applies the raw UTF-8 byte cap;
2. optionally extracts one exact whole-response JSON fence;
3. parses JSON while rejecting duplicate keys and non-finite numbers;
4. requires an object root and supported integer schema version;
5. checks declared response path pointers against the request allowlist;
6. performs strict closed-model validation; and
7. emits sorted-key, compact UTF-8 JSON with one final LF.

Fenced extraction is off by default. When explicitly enabled, the complete
response may contain exactly one ` ``` ` or lowercase ` ```json ` fence, with
only whitespace outside it and a newline between the fence and JSON. Prose,
multiple fences, other language labels, trailing content, and arbitrary
malformed-output repair are rejected.

Malformed JSON, duplicate fields, unsupported versions, unknown fields,
unknown paths, schema violations, and oversize output are typed structured
failures. ContextForge never guesses a path or silently repairs model prose.

## Retry, timeout, and cancellation

Retry classification is explicit on typed failures:

- transient unavailability, timeout, and invalid structured output are
  retryable;
- cancellation, local configuration failure, and invalid request failure are
  not retryable.

The configured retry limit includes zero to two retries after the first call.
Default deterministic backoff is 250 ms, then 1 second. Every provider attempt,
concurrency wait, and retry delay is deadline- and cancellation-aware. An
explicit cancellation event or cancellation of the caller task cancels the
active transport task and raises `ProviderCancelledError`. There is no
unbounded queue, unbounded response read, infinite retry, or live-model
requirement in tests.

## Trust boundary

`ModelRequest` keeps six inputs separate:

1. system instructions;
2. the analysis task;
3. trusted, deterministic CodeMap facts;
4. untrusted source records with portable path, SHA-256, UTF-8 byte length, and
   collision-safe deterministic delimiters;
5. optional validated prior model output, still framed as untrusted context with
   its own label, SHA-256, UTF-8 byte length, and collision-safe delimiter; and
6. the expected output schema.

Each source is capped at 1,000,000 transmitted UTF-8 bytes. At most 100 source
records, 100 prior-context records, and 4,000,000 combined untrusted bytes may
enter one request; trusted CodeMap JSON is separately capped at 4,000,000 bytes.

Only system instructions occupy the provider system message. Source appears
inside the user message as inert delimited data. Prior model output used for
bounded synthesis has a distinct untrusted envelope and is never labeled as a
CodeMap fact. Both are followed by an explicit reminder that untrusted data is
never instruction. The provider layer offers no filesystem, network, Git,
shell, execution, mutation, discovery, or MCP tools and does not interpret
repository instructions as actions.

## Ollama adapter

`OllamaModelProvider` defaults to
`http://127.0.0.1:11434/api/chat`. It sends `stream=false`, the configured
model identifier, the separated system/user messages, deterministic generation
options, and the request JSON Schema in `format`. It accepts the assistant
message content, optional `done_reason`, and reported prompt/evaluation token
counts. Transport reads, HTTP headers, and response bodies are bounded, and
task cancellation closes the active asyncio stream.

The adapter accepts an async transport function for offline contract tests.
The production default uses Python's standard-library asyncio HTTP client, so
this foundation adds no provider SDK dependency.
