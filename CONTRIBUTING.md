# Contributing to ContextForge

Thank you for your interest in ContextForge.

ContextForge remains intentionally small after its v0.2 repository-scanning
milestone. Contributions should preserve the project's current goal: a clear
foundation without speculative business logic.

## Development workflow

1. Create a virtual environment with Python 3.12 or newer.
2. Install development dependencies:

   ```bash
   python -m pip install -e ".[dev]"
   ```

3. Run checks before opening a pull request:

   ```bash
   ruff check .
   ruff format --check .
   mypy
   pytest
   ```

## Contribution guidelines

- Keep the core independent from CLI, API, provider, storage, and editor code.
- Avoid adding heavy dependencies without a clear architectural need.
- Prefer small, reviewable pull requests.
- Update documentation when changing project structure or public behavior.
- Do not add secrets, tokens, local paths, or environment-specific settings.

## Commit style

Use concise, descriptive commit messages. Conventional Commits are welcome but
not required.
