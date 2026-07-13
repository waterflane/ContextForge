**1. Current Architecture**

ContextForge is currently a small modular monolith foundation.

- [pyproject.toml](C:/Programming/Projects/ContextForge/pyproject.toml): Python 3.12+, Hatch build, strict mypy, Ruff, pytest. Runtime deps are `fastapi`, `pydantic`, `pydantic-settings`, and `typer`.
- [src/contextforge/core](C:/Programming/Projects/ContextForge/src/contextforge/core): shared domain-ish models, currently only `HealthStatus` and `VersionInfo`.
- [src/contextforge/cli/main.py](C:/Programming/Projects/ContextForge/src/contextforge/cli/main.py): thin Typer app with `version` and `doctor`.
- [src/contextforge/config.py](C:/Programming/Projects/ContextForge/src/contextforge/config.py): Pydantic settings with `app_name`, `environment`, `log_level`.
- [src/contextforge/logging.py](C:/Programming/Projects/ContextForge/src/contextforge/logging.py): minimal stdlib logging setup.
- [src/contextforge/api](C:/Programming/Projects/ContextForge/src/contextforge/api): FastAPI factory and `/health`, `/version` routes.
- [src/contextforge/repositories](C:/Programming/Projects/ContextForge/src/contextforge/repositories): currently only a placeholder `RepositoryAnalyzer` protocol.
- `context`, `prompts`, `storage`, and `models` are future-facing boundaries with minimal placeholder contracts.
- [tests](C:/Programming/Projects/ContextForge/tests): top-level pytest tests for API, CLI, config, and metadata.
- [docs/architecture/overview.md](C:/Programming/Projects/ContextForge/docs/architecture/overview.md) and ADR-001 require core independence, thin interfaces, and no premature abstractions.
- [CI](C:/Programming/Projects/ContextForge/.github/workflows/ci.yml): installs `.[dev]`, then runs Ruff lint, Ruff format check, mypy, and pytest on Python 3.12 and 3.13.

**2. Proposed Final File Tree For This Stage**

Add only scanner-focused modules and tests:

```text
src/contextforge/
  repositories/
    __init__.py
    analysis.py
    scanner.py          # high-level scan orchestration
    ignore.py           # ignore rule loading/matching
    language.py         # filename/extension language detection
    models.py           # scan domain models
  cli/
    main.py             # add thin scan command
  config.py             # add default scan max size setting

tests/
  test_api.py           # preserve
  test_cli.py           # preserve and extend for scan CLI
  test_config.py        # preserve and extend for scan setting
  test_metadata.py      # preserve
  test_repository_ignore.py
  test_repository_language.py
  test_repository_scanner.py
```

Optionally update docs later, but implementation should not require changing architecture docs unless you want the milestone documented.

**3. Responsibility Of Every New Module**

- `repositories.models`: frozen Pydantic domain models for scan results. No Typer/FastAPI imports.
- `repositories.ignore`: load `.gitignore`, optional `.contextforgeignore`, built-in exclusions, and answer “should this path be skipped?”
- `repositories.language`: small deterministic mapping from filename/extension to language labels.
- `repositories.scanner`: validate root path, traverse recursively, apply ignore rules, skip binary/oversized files, hash accepted files, collect metadata, and return a scan result.
- `cli.main`: expose `contextforge scan` and translate CLI args/output format to scanner calls. It should not contain scanning logic.
- `config`: add a setting like `scan_max_file_size_bytes`.

**4. Domain Models To Introduce**

In `contextforge.repositories.models`:

- `ScanOptions`
  - `max_file_size_bytes: int`
  - `respect_gitignore: bool = True`
  - `respect_contextforgeignore: bool = True`

- `FileMetadata`
  - `path: str`
  - deterministic POSIX-style relative path, e.g. `src/contextforge/config.py`
  - `size_bytes: int`
  - `language: str | None`
  - `sha256: str`

- `SkippedFile`
  - `path: str`
  - `reason: Literal["ignored", "binary", "too_large", "unreadable"]`
  - optional detail string if useful

- `ScanSummary`
  - `root: str`
  - `file_count: int`
  - `skipped_count: int`
  - `total_size_bytes: int`
  - `languages: dict[str, int]`

- `RepositoryScan`
  - `root: str`
  - `files: tuple[FileMetadata, ...]`
  - `skipped: tuple[SkippedFile, ...]`
  - `summary: ScanSummary`

Keep these in `repositories`, not `core`, because repository inventory is the current domain boundary and avoids expanding `core` too soon.

**5. Minimum Dependency Changes Required**

Add one runtime dependency:

- `pathspec>=0.12.0,<1.0.0`

Reason: correct `.gitignore` semantics are tricky, especially directory patterns, negation, anchored patterns, and cross-platform path handling. `pathspec` is small and purpose-built.

Add one dev dependency if enforcing coverage in CI now:

- `pytest-cov>=6.0.0,<7.0.0`

Then update pytest config to include branch coverage and a 90% threshold, for example conceptually: `--cov=contextforge --cov-branch --cov-report=term-missing --cov-fail-under=90`.

Avoid `python-magic`; binary detection can be implemented with a small byte sample heuristic.

**6. Public CLI Behavior**

Implemented contract:

```bash
contextforge scan [PATH] [OPTIONS]
```

`PATH` defaults to the current working directory. The CLI calls the public
`scan_repository(path, ScanOptions(...))` API and does not duplicate traversal,
hashing, binary detection, language detection, or ignore behavior.

Implemented options:

- `--format table|json`, defaulting to `table`.
- `--output PATH`, which writes to a new destination only.
- `--max-file-size INTEGER`, defaulting to the `ScanOptions` model default of
  1,000,000 bytes and rejecting non-positive values.
- `--show-excluded`, which lists paths and reasons in table output.
- `--fail-on-error`, which exits non-zero only for unreadable entries.

JSON uses a stable versioned envelope and preserves the existing domain model
field names:

```json
{
  "schema_version": 1,
  "options": {
    "max_file_size_bytes": 1000000,
    "respect_gitignore": true,
    "respect_contextforgeignore": true
  },
  "snapshot": {
    "root": "<resolved project root>",
    "files": [],
    "ignored_files": [],
    "skipped_files": [],
    "summary": {}
  }
}
```

Output parents must already exist. Existing destinations are refused; there is
no force or overwrite option. Content is fully rendered before a temporary
sibling is written and atomically published, and the output file is created
only after scanning completes.

Exit-code policy:

- `0`: completed successfully, including ordinary exclusions.
- `1`: scanner or output operational failure.
- `2`: invalid command input, including an invalid root or file-size limit.
- `3`: completed with unreadable entries while `--fail-on-error` was enabled.

This differs from the original proposal by using the task-approved name
`table` instead of `text`, serializing the implemented `ProjectSnapshot` rather
than the earlier proposed `RepositoryScan`, and using the domain model default
instead of an unimplemented configuration setting.

**7. Detailed Test Matrix**

- Existing tests:
  - `test_api.py`, `test_cli.py`, `test_config.py`, `test_metadata.py` must remain valid.

- Scanner success:
  - scans nested directories recursively.
  - returns POSIX-style relative paths.
  - output order is deterministic.
  - computes correct SHA-256.
  - records file size.
  - produces correct summary counts and language totals.

- Ignore behavior:
  - excludes `.git`, `.venv`, `__pycache__`, `node_modules`, `build`, `dist`.
  - respects `.gitignore`.
  - respects `.contextforgeignore`.
  - handles directory ignore patterns.
  - handles file ignore patterns.
  - handles negation patterns if using `pathspec`.
  - works with Windows-style filesystem paths while output remains `/` separated.

- Binary and size handling:
  - excludes files containing NUL bytes.
  - excludes likely binary byte samples.
  - accepts normal UTF-8 text.
  - handles empty files.
  - excludes files larger than configured max.
  - accepts files exactly at max size.

- Language detection:
  - detects by extension: `.py`, `.js`, `.ts`, `.md`, `.json`, `.toml`, `.yaml`, `.yml`, `.html`, `.css`, etc.
  - detects by filename: `Dockerfile`, `Makefile`, `README.md`, `pyproject.toml`.
  - returns `None` or `"text"` consistently for unknown files.
  - handles mixed-case extensions deterministically.

- Invalid input and failures:
  - root path does not exist.
  - root path is a file.
  - unreadable file is reported as skipped when possible.
  - unreadable directory does not crash the whole scan if it can be skipped safely.
  - invalid max file size raises a clear error.

- CLI:
  - `contextforge scan <tmp_path>` exits 0.
  - text output contains summary and relative file paths.
  - JSON output parses and contains files/summary/skipped.
  - CLI honors `--max-file-size`.
  - invalid root exits non-zero.
  - existing `version` and `doctor` commands still pass.

**8. Risks And Edge Cases**

- `.gitignore` semantics are easy to get subtly wrong; use `pathspec`.
- Directory pruning matters for performance and for avoiding accidental traversal into `.git` or `node_modules`.
- Symlinks can create cycles. Minimal first-stage behavior should not follow directory symlinks.
- Hashing must read files in chunks, not load large files into memory.
- Binary detection is heuristic; document it as “binary-like” detection, not perfect MIME detection.
- Permissions behave differently on Windows and Linux, so tests for unreadable files need care.
- Case sensitivity differs by filesystem; language detection should normalize extensions.
- Deterministic ordering should sort by relative POSIX path, independent of OS traversal order.

**9. Step-By-Step Implementation Order**

1. Add `pathspec` and optional `pytest-cov` dependency/config.
2. Add repository scan models.
3. Add language detection with focused tests.
4. Add ignore rule loading/matching with focused tests.
5. Add scanner traversal, binary detection, hashing, metadata, and summary.
6. Add scanner edge-case tests using `tmp_path`.
7. Add config setting for default max file size.
8. Add `contextforge scan` CLI as a thin adapter.
9. Extend CLI/config tests.
10. Run Ruff, mypy, and pytest with branch coverage.
11. Only then update README/docs if desired.

**10. Files That Should Not Be Changed**

Do not change these unless a later documentation task explicitly asks for it:

- Existing test files should be preserved, not replaced: `test_api.py`, `test_cli.py`, `test_config.py`, `test_metadata.py`.
- API implementation: `src/contextforge/api/app.py`, `src/contextforge/api/routes.py`.
- Placeholder future modules: `context`, `prompts`, `storage`, `models`.
- Metadata/version files unless doing a release: `src/contextforge/_metadata.py`, `CHANGELOG.md`.
- Architecture/ADR docs should remain stable unless documenting the completed v0.2 decision.
- CI workflow should only change if adding coverage enforcement.
