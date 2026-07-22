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
