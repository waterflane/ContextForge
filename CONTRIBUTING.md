# Contributing to ContextForge

Thank you for helping improve ContextForge. The project is maintained by a
solo maintainer, so focused changes with clear motivation and tests are the
easiest to review.

## Before you start

- Search existing Issues, Discussions, and pull requests before opening a new
  one.
- Use the Q&A category in GitHub Discussions for usage and support questions;
  keep Issues focused on reproducible bugs and scoped improvements.
- Open an Issue before starting a large feature, architecture change, new
  dependency, or behavior-breaking change.
- Never include credentials, private repository content, prompts, or
  unsanitized diagnostics in an Issue or pull request.
- Report security vulnerabilities through the private process in
  [SECURITY.md](SECURITY.md), not a public Issue.

## Development workflow

1. Fork `waterflane/ContextForge`.
2. Create a focused branch from the current `dev` branch.
3. Install the development environment described in
   [docs/guides/development.md](docs/guides/development.md).
4. Add or update tests and public documentation with the implementation.
5. Open a pull request from your fork into `waterflane/ContextForge:dev`.

Release pull requests from `dev` into `main` are owner-managed. Pull requests
opened directly against `main` may be retargeted to `dev`.

Keep pull requests small enough to review as one coherent change. A pull
request should explain its purpose, its user-visible behavior, what it
intentionally does not change, and how it was validated.

## Validation

Run the repository's normal offline checks before requesting review:

```bash
ruff format --check .
ruff check .
mypy
pytest
git diff --check
```

Tests must not require Ollama, LM Studio, remote model APIs, credentials, or
network access. Use `provider none` for structural-only behavior or the
deterministic fake provider where a provider contract is required.

GitHub may hold Actions runs from first-time fork contributors for maintainer
approval. An `Awaiting approval` status does not mean that the pull request or
CI configuration is broken.

Changes should preserve the project's architectural boundaries: core and
application code must not depend on CLI, FastAPI, Rich, Typer, or concrete
model-provider adapters. Public behavior changes require corresponding
documentation.

## Review and merge policy

- One approving review from the code owner is required.
- The code owner may request changes or close work that is out of scope.
- All required CI checks and review conversations must be resolved.
- Pull requests are squash-merged; force pushes and direct pushes to protected
  branches are not part of the contribution workflow.
- Opening a pull request does not grant repository write access or maintainer
  status.

## Contribution license

ContextForge is licensed under the Apache License 2.0. Under section 5 of that
license, unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in ContextForge is provided under Apache-2.0 without
additional terms or conditions.

By submitting a contribution, you represent that you have the right to submit
it under those terms. Do not submit code, assets, or documentation copied from
sources whose terms are incompatible with Apache-2.0. The project does not
currently require a separate Contributor License Agreement or Developer
Certificate of Origin sign-off.
