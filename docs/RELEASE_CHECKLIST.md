# Release checklist

ContextForge releases are owner-managed. Publishing requires an explicit owner
action, protected GitHub environments, and PyPI Trusted Publishing. No package
index API token is stored in repository secrets.

## Prepare

- [ ] Confirm the release branch contains only intended, reviewed changes.
- [ ] Set the version in `src/contextforge/_metadata.py`.
- [ ] Move relevant `CHANGELOG.md` entries from **Unreleased** into a dated
      version section.
- [ ] Confirm the supported Python versions in `pyproject.toml` and CI.
- [ ] Confirm ownership of `contextforge-repo` on PyPI and TestPyPI, and verify
      both Trusted Publisher configurations still match `release.yml`.
- [ ] Confirm the SHA-pinned PyPI publishing action supports the Core Metadata
      version emitted by the current build backend.
- [ ] Update the separate Wiki repository when public behavior or commands
      changed; before changing visibility, replace its installation command with
      `python -m pip install contextforge-repo`.
- [ ] Confirm GitHub Discussions is enabled, its Q&A category accepts ordinary
      usage questions, and the README links to it.
- [ ] Confirm the private conduct address in `CODE_OF_CONDUCT.md` is monitored.
- [ ] Complete the full-history secret scan outside the source tree.

## Validate offline

The CI workflow is authoritative. Equivalent local checks may be run from a
clean environment with the locked dependency graph:

```bash
uv sync --locked --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
uv run twine check dist/*
uv run check-wheel-contents dist/*.whl
uv run python scripts/validate_distribution.py dist
```

Inspect wheel and sdist contents. They may contain only package source,
`pyproject.toml`, README, LICENSE, NOTICE, CHANGELOG, required packaging
metadata, and Hatch's backend-required `.gitignore` build-control file. They
must not contain tests, Wiki files, planning notes, GitHub metadata, caches,
local state, logs, reports, credentials, temporary bridge files, local paths,
or development artifacts. Both archives must contain the complete
`contextforge.bridge` package.

Install the wheel and sdist in separate clean Python 3.12 and 3.13 environments.
Verify only package metadata, importability, and version entry points:

```bash
python -m pip check
python -c "import contextforge"
contextforge --version
ctxf --version
python -m contextforge --version
contextforge bridge --help
```

Run an interactive subprocess smoke test that negotiates bridge protocol `1.0`,
captures a real `snapshot`, and performs clean `shutdown`. Confirm every stdout
line is JSON-RPC, stderr is separate, and the bound fixture workspace is byte-for-byte
unchanged. Run the independent MCP stdio tests in the full pytest suite.

Release validation must not require Ollama, LM Studio, a remote model API, an
API server, or model-backed smoke tests. Tests use structural-only behavior or
the deterministic fake provider.

## Validate model-backed discovery

The versioned ASP discovery benchmark remains a separate regression gate. Its
canonical v0.4.2 fixture is the clean ASP commit
`f4d2e49a639ec8230aae6d7ec25974d1082edd09` with source snapshot digest
`9ad90fc0bfe4e1d12d6116daf6fcef797693f32200a47c7a1da45a11341116f7`.
Create a temporary copy so the developer's previous index remains untouched,
remove only the copied `.contextforge/index`, and build a structural-only index
there with `contextforge index build <temporary-ASP> --provider none`.

Use the official Ollama model tag `qwen2.5-coder:7b`. Record the installed model
digest reported by `ollama list` alongside every retained report. Start Ollama
with an actual loaded context window of 32768 tokens, then give ContextForge the
same explicit value. Do not store provider settings or credentials in the
repository.

```powershell
$env:CONTEXTFORGE_MODEL_CONTEXT_WINDOW = "32768"
ollama list
uv run contextforge diagnostics provider 'C:\Repositories'
uv run contextforge benchmark discovery 'C:\Repositories' `
  --tasks '.\tests\fixtures\asp_discovery_benchmark.json' `
  --modes 'fresh,indexed,hybrid' `
  --format json `
  --output '.\benchmark-report.json'
```

- [ ] Confirm every run completed and all expectations and budgets passed.
- [ ] Confirm the ASP commit, source digest, index generation, exact model tag,
      installed model digest, and context window match across retained reports.
- [ ] Retain three consecutive fully passing reports made with that same setup.
- [ ] Inspect every failure; update an expectation only when repository evidence
      proves it stale, never merely to make the suite pass.
- [ ] Confirm the report and stderr contain no prompts, responses, source
      contents, credentials, or private absolute paths.

## Merge and tag

- [ ] Merge the reviewed release pull request from `dev` into `main`.
- [ ] Verify the exact merge commit contains the intended version and changelog.
- [ ] Create a signed annotated `vX.Y.Z` tag on that exact commit, or
      `vX.Y.Z.postN` for a PEP 440 post release.
- [ ] Push the tag without rewriting earlier tags.
- [ ] Synchronize `dev` with the released `main`.

## Publish

1. Dispatch `.github/workflows/release.yml` as the repository owner and supply
   the existing tag.
2. Approve the protected `testpypi` environment and verify the TestPyPI
   installation job.
3. Approve the protected `pypi` environment only after TestPyPI succeeds.
4. Inspect the draft GitHub Release, wheel, sdist, SBOM, and `SHA256SUMS`.
5. Publish the draft GitHub Release manually.
6. Verify the PyPI page, install command, release notes, checksums, and Wiki.
7. After the workflow is proven, enable immutable GitHub Releases.
