# Model-provider foundation

## Implemented boundary

ContextForge has a provider-independent foundation for bounded, schema-bound
model analysis. It defines the request/response/configuration contracts, a
deterministic scripted fake, a local Ollama adapter, and a local
OpenAI-compatible adapter with an LM Studio alias. The incremental file
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
- `OpenAICompatibleModelProvider` for non-streaming JSON Schema chat
  completions and model diagnostics; and
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

[models.structured_response]
max_repair_attempts = 5
```

Generic OpenAI-compatible APIs do not standardize context-window discovery.
ContextForge therefore uses a conservative 4,096-token default unless
`context_window` or CLI `--context-window` supplies the actual loaded-model
limit. Precedence is CLI, supported `CONTEXTFORGE_MODEL_*` environment values,
`config.local.toml`, `config.toml`, then defaults. Increasing a compatible
window alone does not invalidate semantic records.

Context diagnostics retain every candidate separately: CLI, environment,
local config, shared config, provider-reported value, model metadata, and the
built-in default. The event also names the effective value and source. A
provider/model value such as 98,304 is not silently substituted for a
ContextForge `config.toml` value of 16,384; diagnostics show both and identify
`config.toml` as the effective source.

Connection, response-read, and complete-operation defaults are 10, 300, and
360 seconds. The retained `timeout_seconds` value is a compatibility operation
ceiling. `ProviderConfiguration` also enforces a response cap in
`[1, 16,000,000]`, concurrency in `[1, 8]`, and at most two retries after the
first attempt. An individual request may lower its response cap. Ollama's
`local_only=true` policy accepts only `127.0.0.1`, `::1`, or `localhost`.
For a non-loopback endpoint, `local_only=false` is not sufficient by itself:
`external_data_policy="allow_repository"` is also required. The
`allow_selected` value is reserved and does not authorize remote transport in
this release because path-level transmission enforcement is not implemented.

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
2. checks the provider finish state and rejects truncation;
3. deterministically trims whitespace and, when enabled by the request,
   extracts one exact whole-response JSON fence;
4. parses exactly one JSON value while rejecting duplicate keys, non-finite
   numbers, trailing prose, and competing objects;
5. validates top-level type, required fields, field types, limits, additional
   properties, and the supported schema version;
6. inserts a missing constant version only when it is the sole safe omission;
7. checks declared paths and task-specific identifiers against the pinned
   request allowlists;
8. performs strict closed-model validation and internal-result conversion; and
9. emits sorted-key, compact UTF-8 JSON with one final LF.

Every model-facing schema requires `schema_version`; a default and `const`
alone are not considered sufficient provider instructions.

Text-producing operations use the closed
`{"schema_version":1,"content":"..."}` envelope. The deterministic prompt
compiler also validates its completed text through this envelope locally; the
prompt body is neither sent for repair nor written to diagnostics.

Fenced extraction is enabled for normal model tasks. When enabled, the complete
response may contain exactly one ` ``` ` or lowercase ` ```json ` fence, with
only whitespace outside it and a newline between the fence and JSON. Prose,
multiple fences, other language labels, trailing content, and arbitrary
malformed-output repair are rejected.

Malformed JSON, duplicate fields, unsupported versions, unknown fields,
unknown paths, schema violations, and oversize output are typed structured
failures. ContextForge never guesses a path or silently repairs model prose.

## Retry, timeout, and cancellation

Retry classification is explicit on typed failures:

- connection reset, timeout, HTTP 429, and selected 5xx failures are transient;
- context overflow, model-not-found, invalid request schemas, rejected grammar,
  wrong response shapes, and unsupported versions are deterministic until the
  request changes;
- malformed or schema-invalid output receives up to the independently configured
  five compact repair generations by default (safe range 0–10); every response
  is revalidated through the same gateway and the instruction progresses.

The configured transport retry limit includes zero to two retries after the
first transport call and never consumes JSON repair attempts.
Default deterministic backoff is 250 ms, then 1 second. Every provider attempt,
concurrency wait, and retry delay is deadline- and cancellation-aware. An
explicit cancellation event or cancellation of the caller task cancels the
active transport task and raises `ProviderCancelledError`. There is no
unbounded queue, unbounded response read, infinite retry, or live-model
requirement in tests.

Every attempt emits the shared structured `ProgressEvent` lifecycle before the
provider call, after validated acceptance, on bounded retry, and on terminal
failure. The event exposes attempt counts and safe error codes such as
`provider_timeout`, `connection_error`, `http_error`, `model_not_found`,
`malformed_json`, and `structured_output_validation_failed`; it never includes
raw responses. Semantic requests select an adaptive `max_output_tokens` from
128 through 512 according to category, size, and structural complexity. The
configured `semantic_max_output_tokens` or CLI `--max-output-tokens` is a
ceiling, not a target. Global repository maps use a separate 512-token bound.
OpenAI-compatible requests transmit the selected limit as `max_tokens`.

Before dispatch, ContextForge adds conservative message/input tokens, native
schema grammar cost, requested output, provider-wrapper overhead, and a
256-token reserve. With no exact tokenizer, code and JSON are estimated at one
token per three UTF-8 bytes. A known overflow fails locally as
`context_window_exceeded`; source, structural metadata, or prior summaries are
reduced deterministically before that final guard. Debug metrics include each
cost and the total without prompts or source.

Debug logs contain only source path, analyzer kind, estimated input tokens,
selected output limit, attempt, provider-reported response tokens, duration,
validation result, and truncation state. They never contain prompts, source,
complete responses, credentials, or headers. Normal CLI output remains concise.

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

`ModelRequest.messages()` can include the schema for adapters that require a
prompt-level contract. Ollama `format` and OpenAI-compatible
`response_format.json_schema` carry it natively, so those adapters omit the
duplicate schema prose. The user contract still requires concise JSON only,
with no Markdown, reasoning, source repetition, or surrounding text.

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

## OpenAI-compatible and LM Studio adapter

`OpenAICompatibleModelProvider` defaults to the LM Studio base URL
`http://localhost:1234/v1`; `lmstudio` is accepted by configuration and CLI as
an alias for the canonical persisted provider ID `openai-compatible`. It first
checks the configured model against the exact IDs in `GET /v1/models`, then
sends non-streaming `POST /v1/chat/completions` requests with
`response_format.type=json_schema`. If grammar construction is explicitly
rejected, it retries once using `json_object`, validates locally against the
same model, and caches the capability for that provider/model/base-URL adapter
identity. Arbitrary parsed JSON is never accepted. No model name is supplied by
default.

The base URL is configurable with `[models].base_url` or CLI `--base-url`.
Changing it changes the credential-free SHA-256 suffix on semantic and
repository-map analyzer identity versions, invalidating model-dependent records
without changing the persisted provider/model schema.
An optional bearer token is loaded only through the configured
`credential_env` name. Authentication failures, safe structured error bodies,
missing model IDs, malformed envelopes, structured-output rejection,
unavailability, timeout, and cancellation are translated to the shared typed
provider errors. The adapter uses the same bounded retry runtime as Ollama and
accepts an injectable async HTTP transport for offline tests.

## Troubleshooting local structured providers

`request exceeds the available context size` means the configured window is
larger than the model actually loaded by LM Studio, or an older client sent the
request without preflight. Set, for example, `context_window = 4096` under
`[models]` (or pass `--context-window 4096`) to match LM Studio. Current builds
reduce file excerpts and hierarchy context before dispatch and report
`context_window_exceeded` without spending transient retries.

The structured `budget.calculated` record splits input into system, user,
source/prior-context, and selected-index tokens, then adds schema, requested
output, protocol overhead, and safety margin. These components reproduce the
total exactly. `budget.rejected` explicitly records
`request_dispatched=false`; it never attributes a local rejection to LM Studio.

`structured output grammar rejected` means the server could not compile the
strict schema. Current repository-map schemas are compact. The OpenAI-compatible
adapter also tries JSON-object mode once, validates the result locally, and
caches that capability for the current provider/model/base URL. If the fallback
still omits `title`, uses a nested `summary`, or returns the wrong version,
ContextForge rejects it with the corresponding shape, field, or version code;
normal output never prints the raw response.
