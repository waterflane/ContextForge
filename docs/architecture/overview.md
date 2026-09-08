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
- `bridge`: trusted-local JSON-RPC/NDJSON transport over the model-free
  application boundary;
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
same facts-versus-interpretation boundary. Model-free and model-assisted
repository discovery are
documented in [Repository discovery](repository-discovery.md), and its
review-to-package integration and pure prompt compiler are documented in
[Context handoffs and prompt compilation](context-handoffs.md). ContextForge
does not execute compiled prompts. The shared
[application progress contract](progress-reporting.md) exposes structured,
observer-isolated, weighted workflow phases without interface dependencies. One
shared stderr-only Typer renderer adapts those events without contaminating
structured stdout. The separate
[structured diagnostics contract](diagnostics.md) records safe facts,
decisions, request budgets, and causal errors for CLI and future interfaces
without turning progress refreshes into logs. Thin Typer commands expose index and context workflows,
while a bounded read-only MCP
adapter exposes the same core APIs without shell, source-write, Git-mutation,
or index-mutation capabilities. The independent
[generic bridge v1](../guides/bridge.md) is a second local stdio adapter. It
binds one workspace, keeps repository truth and source verification inside
ContextForge, and lets the consumer own model selection and orchestration. MCP
and bridge do not depend on one another and neither introduces transport logic
into the Python core.

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
implemented in v0.4. Version 0.4.1 adds maintenance-level progress,
diagnostics, nested-ignore, and CLI usability improvements; it does not claim
future GUI, remote transport, orchestration, or source-mutation functionality.
Version 0.5.0 adds the trusted-local, model-free, read-only bridge v1 without
changing the existing model-assisted discovery or MCP semantics. Version 0.5.1
adds verified polyglot declarations, resumable semantic coverage, bridge
protocol 1.1, and stricter exact-symbol and dependency-aware discovery.
