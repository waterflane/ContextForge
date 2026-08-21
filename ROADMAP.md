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
- [ ] Publish the reviewed signed release through TestPyPI and PyPI.

## Later

- Full multi-root workspaces.
- Additional deterministic language extractors based on measured demand.
- Optional supplementary retrieval strategies that do not replace complete
  allowed-tree/text access.
- Graphical workspace review UI.
- External coding-agent integrations that consume handoffs under their own
  authority.

Autonomous edits, coding-agent orchestration, Git worktree management, and Git
mutation are not part of the current roadmap.
