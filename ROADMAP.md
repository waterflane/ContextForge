# Roadmap

This roadmap describes broad milestones only. It does not promise dates.

## v0.1: Project foundation (milestone complete)

- [x] Establish repository structure.
- [x] Provide a minimal Python package.
- [x] Add CLI and local API skeletons.
- [x] Add configuration, logging, documentation, tests, and CI.

## v0.2: Repository scanning and file inventory (milestone complete)

- [x] Discover repository files.
- [x] Respect ignore rules.
- [x] Produce a reviewable project file inventory.

## v0.3: Context selection and export (milestone complete)

- [x] Select files and metadata manually for a development task.
- [x] Build bounded, portable context packages with verified source content.
- [x] Export deterministic Markdown and schema-versioned JSON.
- [x] Inspect and validate JSON packages without the original repository.

## v0.4: Repository intelligence and read-only integrations (milestone complete)

- [x] Build deterministic Python CodeMaps with an honest fallback elsewhere.
- [x] Persist immutable incremental index generations and global repository maps.
- [x] Support bounded local/fake provider workflows with semantic provenance.
- [x] Discover context in indexed, fresh, and hybrid modes.
- [x] Build reviewable handoffs with optional Git diff and compiled prompts.
- [x] Expose thin CLI commands and a local read-only stdio MCP foundation.

### v0.4.1 maintenance release (complete)

- [x] Expose structured progress events with weighted percentages for CLI and
  future interface adapters.
- [x] Correct nested `.gitignore` scope, inheritance, and negation behavior.
- [x] Provide consistent `version`/`--version` output and the `ctxf` alias from
  one authoritative package version.
- [x] Add structured diagnostics without mixing logs, progress, and command
  output streams.

### v0.4.2 first public release (release candidate validated)

- [x] Prepare the `contextforge-repo` PyPI distribution while preserving the
  `contextforge` import package and the `contextforge` and `ctxf` commands.
- [x] Stabilize bounded discovery in fresh, indexed, and hybrid modes, including
  candidate coverage, role-aware ranking, typed model actions, and minimal
  deterministic fallback.
- [x] Add versioned discovery benchmark manifests and text, Markdown, and
  schema-versioned JSON benchmark reports without changing the existing public
  discovery formats.
- [x] Validate the ASP discovery suite in three consecutive complete runs with
  fixed source, index, model, and context-window provenance.
- [x] Add locked distribution builds, clean-install validation, SBOM generation,
  Trusted Publishing workflows, and public contribution/security policies.
- [x] Correct ANSI-sensitive CLI help validation for consistent Linux and
  Windows release CI.

## v0.5: Generic local integration bridge (milestone complete)

- [x] Expose immutable model-free candidate, expansion, verified-read, and
  package application contracts without changing model-assisted discovery.
- [x] Add a persistent workspace-bound JSON-RPC 2.0 bridge over UTF-8 NDJSON
  stdio with explicit protocol v1 negotiation and cancellation.
- [x] Keep repository truth, source identity verification, path policy, and
  read-only guarantees inside ContextForge while consumers own model selection.
- [x] Keep MCP independent and preserve its existing read-only protocol.

### v0.5.1 repository intelligence and host-integration release (complete)

- [x] Add verified Tree-sitter declaration extraction for ten common polyglot
  languages and publish explicit relationship-coverage limitations.
- [x] Persist index schema v2 source regions, resumable semantic chunks, and
  complete required-symbol budgeting.
- [x] Add bridge protocol 1.1 expansion candidates and index coverage while
  retaining protocol 1.0 compatibility.
- [x] Harden exact-symbol discovery, lexical dependency resolution, Kotlin
  definition ranking, and reviewed deterministic fallback finalization.
- [x] Adapt structured model requests to truncation and reported runtime context
  limits without weakening strict validation.

- [x] Add independent fail-fast and bounded-failure index policies while
  preserving the existing `--fail-on-error` contract.
- [x] Classify provider failures, stop retrying terminal authentication, quota,
  model, and configuration errors, and add a job-scoped circuit breaker.
- [x] Expose clean JSONL index progress and bounded Bridge progress
  notifications that coalesce synchronous bursts while detecting sustained
  client backpressure.
- [x] Add opt-in Bridge 2 tracked build/update jobs with cooperative
  cancellation, caller-deadline responses, background lock-safe cleanup,
  snapshot preconditions, and atomic publication guards.
- [x] Publish readable/current index-family schema capabilities, stabilize
  analyzer identity across endpoint changes, and return safe typed Bridge
  integration errors.

## v0.6: Index v3.1 and evidence compilation (beta)

- [x] Retain the `contextforge-repo` PyPI distribution, `contextforge` import,
  and existing `contextforge` / `ctxf` commands.
- [x] Publish structural-first immutable Index v3.1 generations, followed by
  optional source-grounded semantic enrichment with explicit partial coverage.
- [x] Add deterministic exact/BM25/verified-graph retrieval and a bounded planner
  with search, symbol, graph, map, and grounded query expansion actions.
- [x] Compile Context Capsule v2 with frozen evidence requirements, source
  identity checks, effective sufficiency, and a hard token budget.
- [x] Expose source-bound function descriptions and function-level discovery in
  the Python API, plus optional compact source prompt export.
- [x] Preserve mandatory material within the hard budget and verified discovery
  after planner failure; reduce repeated planner input.
- [x] Prepare standard `0.6.0b1` wheel and source distributions and validate clean
  installations, public schemas, and the existing aggregate coverage gate.
- [ ] Improve descriptive function selection and delivery of implementation
  ranges through standard retrieval; finding a file is not complete source
  coverage. Keep planner request/session limits visible when they cause fallback.
- [ ] Complete independent full-semantic quality acceptance in both deterministic
  and planned modes across the pinned real repositories.
- [ ] Confirm citation support, groundedness, oracle comparison, and token savings
  on quality-passed tasks; practical lookup checks alone do not close this gate.
- [ ] Publish the beta through the existing PyPI release workflow after separate
  authorization. Local artifacts are not a published release.

See the [beta package guide](docs/guides/release-0.6.0b1.md) and
[practical function lookup guide](docs/guides/function-lookup.md). The presentation
web application remains separate from the Python package.

## Later

- Full multi-root workspaces.
- Additional deterministic language extractors based on measured demand.
- Optional supplementary retrieval strategies that do not replace complete
  allowed-tree/text access.
- Graphical workspace review UI.
- Richer external integrations that consume bridge results and handoffs under
  their own authority.

Autonomous edits, coding-agent orchestration, Git worktree management, and Git
mutation are not part of the current roadmap.
