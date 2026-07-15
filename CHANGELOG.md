# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
