# Contributing to ContextForge

ContextForge is currently developed and maintained by its owner as a
solo-maintainer project.

## Issues are welcome

If GitHub Issues are enabled, users are welcome to report reproducible bugs and
suggest focused improvements. Before filing an issue, search for an existing
report and remove credentials, repository contents, and other sensitive data
from logs or examples.

## Pull-request policy

External pull requests are not currently accepted. Please open an issue before
writing an implementation. Unsolicited implementation pull requests may be
closed without review so the maintainer can keep the project direction and
review workload manageable.

This policy may change as the project matures. Creating a fork or a fork-based
pull request does not make anyone an official ContextForge maintainer.

## Owner development workflow

The maintainer's local setup and validation commands are documented in
[Development](docs/guides/development.md). Changes should keep the core
independent from interface and adapter code, update public documentation when
behavior changes, and never commit secrets or machine-specific state.
