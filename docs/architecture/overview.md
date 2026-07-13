# Architecture Overview

## What ContextForge is

ContextForge is a context-management layer for software projects. Its long-term
purpose is to inspect repositories, organize project knowledge, and prepare
compact context packages for external AI models and agents.

## What ContextForge is not

ContextForge is not an AI coding assistant, model runtime, IDE extension, vector
database, or replacement for source control. It should not make code changes on
its own. It prepares context for tools that do.

## Main architectural boundaries

The project is planned as a modular monolith with these boundaries:

- `core`: shared domain concepts and application-level contracts;
- `context`: future context package construction;
- `prompts`: future prompt package construction;
- `repositories`: repository and language analysis adapters;
- `storage`: storage adapters;
- `models`: model-provider adapters;
- `cli`: command-line interface;
- `api`: local HTTP API;
- future IDE integrations outside the core.

## Dependency direction

Dependencies should point inward:

- CLI and API may depend on core and application packages.
- Adapters may depend on core contracts.
- Core must not depend on FastAPI, Typer, model providers, storage
  implementations, or editor integrations.

This keeps the core testable and reusable as new interfaces are added.

## Interfaces and the core

The CLI, local API, and future IDE integrations should act as thin entry points.
They translate user or tool requests into calls against the application/core
layer, then return results in their own format.

The v0.1.0 CLI and API prove that the package is installed and wired correctly.
The completed, unreleased v0.2 scanner is contained in the `repositories`
boundary, while its table/JSON presentation remains in the thin CLI boundary.

## Excluded from v0.1.0

The initial release deliberately excludes:

- repository scanning, indexing, and retrieval;
- Tree-sitter or language parsing;
- embeddings and vector databases;
- model SDKs and LLM integration;
- knowledge graphs;
- prompt generation;
- persistent storage;
- IDE extensions;
- plugin systems or complex dependency injection.

Repository scanning was excluded from v0.1.0 and is implemented by the
subsequent v0.2 milestone. Indexing and retrieval remain unimplemented.
