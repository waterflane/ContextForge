# Release checklist

ContextForge releases are owner-managed. This checklist prepares and verifies a
release; it does not authorize publishing to PyPI or changing GitHub settings.

## Prepare

- [ ] Confirm the working tree contains only intended changes.
- [ ] Set the version in `src/contextforge/_metadata.py`.
- [ ] Move relevant `CHANGELOG.md` entries from **Unreleased** into a dated
      version section.
- [ ] Confirm README and Wiki commands against recursive `--help` output.
- [ ] Confirm the supported Python versions in `pyproject.toml` and CI.

## Validate

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pytest --basetemp="$PWD\.pytest-release-tmp"
python -m build
python -m twine check dist/*
```

Install the wheel in a clean environment and check both entry points:

```powershell
python -m venv .release-venv
.\.release-venv\Scripts\python.exe -m pip install .\dist\contextforge-<version>-py3-none-any.whl
.\.release-venv\Scripts\contextforge.exe --help
.\.release-venv\Scripts\contextforge.exe --version
.\.release-venv\Scripts\ctxf.exe --version
```

Inspect both archives before publication. They must not contain `.env`,
`.contextforge`, caches, test output, `wiki/`, local reports, or credentials.

## Tag and publish

Only the repository owner should perform these steps after the validation above
passes:

```powershell
git tag -s v<version> -m "ContextForge v<version>"
git push origin v<version>
python -m twine upload dist/*
```

Uploading to PyPI, TestPyPI, or GitHub Releases requires explicit owner action.
After publication, verify the release artifacts and update the separate GitHub
Wiki repository if its pages changed.
