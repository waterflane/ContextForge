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
- [ ] Confirm `contextforge-cli` is still available on PyPI and the Trusted
      Publisher configuration matches `release.yml`.
- [ ] Update the separate Wiki repository when public behavior or commands
      changed.
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
```

Inspect wheel and sdist contents. They may contain only package source,
`pyproject.toml`, README, LICENSE, NOTICE, CHANGELOG, required packaging
metadata, and Hatch's backend-required `.gitignore` build-control file. They
must not contain tests, Wiki files, planning notes, GitHub metadata, caches,
local state, logs, reports, credentials, or heavy images.

Install the wheel and sdist in separate clean Python 3.12 and 3.13 environments.
Verify only package metadata, importability, and version entry points:

```bash
python -c "import contextforge"
contextforge --version
ctxf --version
python -m contextforge --version
```

Release validation must not require Ollama, LM Studio, a remote model API, an
API server, or model-backed smoke tests. Tests use structural-only behavior or
the deterministic fake provider.

## Merge and tag

- [ ] Merge the reviewed release pull request from `dev` into `main`.
- [ ] Verify the exact merge commit contains the intended version and changelog.
- [ ] Create a signed annotated `vX.Y.Z` tag on that exact commit.
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
