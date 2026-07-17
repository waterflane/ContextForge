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

The project is organized as a modular monolith with these boundaries:

- `core`: shared domain concepts and application-level contracts;
- `context`: project trees, explicit selection, verified source reads, context
  package construction, rendering, and offline JSON inspection;
- `intelligence`: deterministic index schemas, invalidation, immutable local
  generations, atomic record publication, and single-writer locking;
- `handoff`: discovery review, verified package materialization, optional Git
  context, task refinement provenance, and deterministic prompt compilation;
- `prompts`: portable compiled prompt text models;
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

The repository scanner is contained in the `repositories` boundary. The
completed v0.3 context-package application logic is contained in
the `context` boundary. Typer commands and table/JSON scan presentation remain
in the thin CLI boundary.

Repository intelligence currently includes deterministic CodeMaps and local
immutable storage, documented in
[Repository intelligence storage](repository-intelligence-storage.md). The
[model-provider foundation](model-providers.md) adds bounded structured calls,
a deterministic fake, and a local Ollama adapter. Incremental model-assisted
[file and symbol semantic analysis](semantic-analysis.md) stores interpretations
separately from source facts. Bounded hierarchical
[repository architecture and feature maps](repository-maps.md) preserve the
same facts-versus-interpretation boundary. Model-guided repository discovery is
documented in [Repository discovery](repository-discovery.md), and its
review-to-package integration and pure prompt compiler are documented in
[Context handoffs and prompt compilation](context-handoffs.md). ContextForge
does not execute compiled prompts. The shared
[application progress contract](progress-reporting.md) exposes structured,
observer-isolated workflow phases without interface dependencies. Thin Typer
commands expose index and context workflows, while a bounded read-only MCP
adapter exposes the same core APIs without shell, source-write, Git-mutation,
or index-mutation capabilities.

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
subsequent v0.2 milestone. Context selection/export shipped in v0.3, and
repository intelligence, bounded discovery, handoffs, and read-only MCP are
implemented in v0.4.0.
