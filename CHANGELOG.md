# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Feature releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html),
and Python distribution versions follow PEP 440.

## [Unreleased]

## [0.4.2.post1] - 2026-08-21

### Fixed

- Made the CLI help regression validation insensitive to ANSI terminal styling
  so the same documented options are verified consistently on Linux and
  Windows CI runners.

## [0.4.2] - 2026-08-21

### Added

- Added versioned discovery benchmark manifests and the
  `contextforge benchmark discovery` command with text, Markdown, and canonical
  JSON reports for quality, repeatability, performance, warnings, and provider
  accounting.

### Changed

- Relicensed ContextForge `0.4.2` and later under Apache License 2.0; earlier
  tagged releases remain under the license included in their snapshots.
- Prepared the first public PyPI release as `contextforge-repo` while preserving
  the `contextforge` import package and the `contextforge` and `ctxf` commands.
- Opened the contribution workflow to fork-based pull requests targeting
  `dev`, with owner review through CODEOWNERS and Apache-2.0 inbound terms.
- Enabled GitHub Discussions for ordinary usage questions and Q&A, and added a
  private project conduct-reporting address with a GitHub abuse-reporting
  fallback.
- Replaced the editable-install-only CI path with locked, provider-independent
  lint, type, test, distribution, and clean-install validation.
- Added owner-triggered OIDC publication for TestPyPI and PyPI, draft GitHub
  releases, checksums, SBOM generation, and non-PR dependency/security audits.
- Archived completed implementation plans and removed machine-specific paths
  from the public documentation set.
- Redesigned the public README, added a restrained ContextForge visual identity,
  prepared a complete English GitHub Wiki workspace, and clarified the current
  solo-maintainer review policy.
- Tightened distribution metadata and source-distribution exclusions so local
  state, tests, planning documents, Wiki files, and development-only repository
  metadata are not packaged.
- Stabilized `context suggest` around text as the default interactive output and
  documented text, Markdown, and JSON as presentation renderers over the same
  canonical structured selection.
- Documented fresh, indexed, and hybrid intent, valid cross-mode selection
  differences, exact agreement versus semantic stability, grouped warning and
  stdout/stderr behavior, and the limits of model-backed repeatability for the
  v0.4.2 discovery work.

### Fixed

- Kept Ollama native structured output within its grammar compiler limits while
  retaining strict local response validation, plus a one-shot bounded plain-JSON
  fallback with the schema in the model instruction and exact HTTP accounting.

## [0.4.1] - 2026-07-22

### Added

- Added a presentation-independent, schema-versioned structured progress event
  contract for CLI consumers and future API/GUI adapters.
- Added weighted overall and phase percentages, per-item accounting, provider
  lifecycle details, and safe cancellation/failure terminal events for
  long-running index and automatic context workflows.
- Added `contextforge version`, the eager `contextforge --version` option, and
  the interchangeable `ctxf` console alias. `python -m contextforge --version`
  uses the same authoritative package version.
- Added the centralized structured logging system with quiet through
  trace levels, pretty/JSON stderr rendering, component focus, rotating local
  JSON file logs, centralized structured redaction, correlation IDs, safe
  provider/request/budget/schema/retry/fallback events, and API-readable
  versioned diagnostic records.
- Added context-window provenance and reproducible request-budget diagnostics,
  including explicit local-rejection state and the 98,304-token regression
  path for LM Studio/OpenAI-compatible configurations.
- Added safe run summaries under `.contextforge/runs` and read-only
  `diagnostics last`, `show`, `config`, and `provider` commands.

### Changed

- Rendered interactive progress as a single Rich stderr panel and redirected
  progress as discrete, non-ANSI records while preserving parseable JSON and
  Markdown stdout.
- Kept progress and diagnostics as distinct contracts while routing stderr
  logging through the existing single Rich live-console owner. MCP stdout
  remains protocol-only, and JSON/Markdown command stdout remains parseable.
- Documented logging configuration, diagnostics, redaction, and context-window
  troubleshooting for the 0.4.1 maintenance release.

### Fixed

- Applied nested `.gitignore` files relative to their containing directories
  during repository scans, including inherited rules and Git-style negation.
- Kept provider/model progress accounting monotonic across reuse, fallback,
  retry, failure, publication, and cancellation paths.
- Excluded local index/run state, test and analysis caches, coverage shards,
  staging data, and temporary files explicitly from distribution builds.

## [0.4.0] - 2026-07-17

### Added

- Added `contextforge index build|update|status|clean` as thin adapters over
  deterministic CodeMaps, incremental semantic analysis, repository maps,
  immutable generation storage, locking, and policy-bounded cleanup.
- Added strict project/command provider configuration for local Ollama,
  structural-only operation, and a deterministic offline fake without storing
  credential values in generated artifacts.
- Added `contextforge context suggest` for indexed, fresh, and hybrid discovery
  with reviewable paths/ranges, reasons, confidence, warnings, and byte budgets.
- Extended `contextforge context create` with opt-in automatic discovery,
  labelled task refinement, bounded read-only Git context, portable task
  handoffs, and deterministic compiled-prompt output while preserving manual
  behavior when automatic discovery is absent.
- Added `contextforge context review` for portable handoff validation and review
  without the original repository.
- Added a local read-only stdio MCP foundation with bounded overview, tree,
  search, summary, relationship, verified-read, Git-diff, suggestion,
  in-memory package-build, and portable package-inspection tools.
- Added offline CLI and MCP protocol/security tests covering structured stdout,
  stderr diagnostics, incremental reuse, path rejection, byte bounds,
  cancellation, overwrite policy, and absence of write capabilities.

- Added `contextforge context create [PATH]` as a thin adapter over the public
  context builder and Markdown/JSON renderers, with exact, directory, glob,
  exclusion, line-range, optional-tree, task, and byte/file-limit options.
- Added atomic package output with explicit `--force` replacement and clean,
  parseable JSON stdout when no output path is supplied.
- Added `contextforge context inspect PACKAGE` for bounded offline JSON schema
  and semantic validation, including task, statistics, selected paths, and
  included line-range display without the original repository.
- Added deterministic project-tree rendering, snapshot-only manual selection,
  verified UTF-8 source reads, canonical context packages, and pure Markdown
  and JSON renderers.

- Added deterministic recursive repository traversal with portable relative
  paths, SHA-256 hashes, basic language detection, and scan summary statistics.
- Added default exclusions, protected VCS metadata handling, repository-root
  `.gitignore` and `.contextforgeignore` support, binary-like detection,
  maximum-size enforcement, and non-followed symlink/junction reporting.
- Added `contextforge scan [PATH]` as a thin CLI adapter over the repository
  scanner, with deterministic file-inventory table and JSON output.
- Added maximum-file-size, excluded-entry display, unreadable-entry failure,
  and safe output-file options.
- Added a versioned JSON report envelope containing the scan options and the
  existing `ProjectSnapshot` model.

### Fixed

- Prune unreferenced crash leftovers before immutable generation publication
  and validate semantic interpretation records independently of structural
  record presence.
- Reserve discovery read budget for model-directed inspection and final source
  verification instead of allowing the fresh CodeMap prepass to exhaust it.
- Bound scheduled semantic tasks, per-file semantic requests, repository-map
  calls, and MCP request-line memory.
- Enforce external repository-data policy for non-loopback model endpoints and
  reject ASCII control characters in portable paths across persisted and
  portable artifacts.
- Report staged Git additions as `added`, validate required MCP initialize
  fields, and propagate MCP cancellation rather than converting it to an
  internal protocol error.

- Preserve the prior active index generation when a strict multi-phase CLI
  build fails after a previous valid generation exists.
- Keep JSON stdout free of operational messages for discovery, status, and
  automatic handoff output.

- Configure the shared console and module entry points for UTF-8 stdout and
  stderr so valid Unicode packages do not fail on legacy Windows code pages.
- Reject context packages whose selected files, source bytes, directory
  prefixes, or language counts exceed their claimed selectable project
  metadata, including packages that omit the optional tree.
- Reject control characters in package language labels so offline inspection
  cannot emit attacker-controlled terminal control lines.
- Update security and architecture documentation for verified source reads,
  portable paths, atomic publication, and concurrent-modification assumptions.

- Disable Click's native Windows argument expansion in the shared console and
  module entry wrapper so quoted GitWildMatch selectors reach ContextForge as
  one unchanged argument.
- Correct context CLI examples to distinguish exact files, directories, and
  glob patterns, and make unmatched exact-file errors explain the appropriate
  selector options.

- Prune directories ignored by default, `.gitignore`, or
  `.contextforgeignore` rules instead of traversing and serializing every
  descendant.
- Make detailed JSON exclusions conditional on `--show-excluded`, while
  retaining unreadable entries for diagnostics.
- Prevent Windows directory junction traversal and reject files whose identity
  changes while being opened.
- Keep file hashing bounded when a file grows during scanning, and derive size,
  binary classification, and SHA-256 from one consistent file read.

## [0.1.0] - 2026-07-13

### Added

- Initial Python package using a `src` layout.
- Minimal CLI with `contextforge version` and `contextforge doctor`.
- Minimal FastAPI application with `/health` and `/version`.
- Configuration and logging foundations.
- Placeholder package boundaries for core, context packages, prompts,
  repository analysis, storage, and model providers.
- Initial tests, documentation, GitHub metadata, and development tooling.
