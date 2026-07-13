# ADR-001: Core as an independent modular application

## Status

Accepted

## Context

ContextForge is expected to grow multiple entry points and integration surfaces:
a CLI, a local HTTP API, storage adapters, model-provider adapters, repository
analysis adapters, and future IDE extensions.

If the core depends directly on any of these surfaces, future changes will be
harder to test and reason about. For example, domain logic should not require
FastAPI to be importable, and repository analysis should not require a specific
model provider.

## Decision

The ContextForge core must be independent from specific user interfaces, AI
providers, storage implementations, and IDE integrations.

The project will start as a straightforward modular monolith. Package boundaries
will be explicit, but the codebase will avoid premature abstractions.

## Reasons

- Keeps core concepts portable across CLI, API, and future IDE integrations.
- Makes tests faster and easier to write.
- Allows storage and model-provider choices to evolve.
- Reduces lock-in to a single interface or provider.
- Supports incremental releases without heavy infrastructure.

## Consequences

- Interface layers must stay thin and translate into core/application calls.
- Provider-specific code belongs outside the core.
- Some simple protocols may be introduced where they mark real boundaries.
- The project must resist adding speculative factories, event buses, plugin
  systems, or dependency injection containers before they are needed.

## Rejected alternatives

- Build the CLI as the central application: rejected because future API and IDE
  integrations would inherit CLI assumptions.
- Build the FastAPI service as the central application: rejected because local
  HTTP should be one interface, not the domain owner.
- Add a plugin system immediately: rejected as unnecessary complexity for the
  foundation release.
- Add provider SDKs immediately: rejected because v0.1.0 has no model
  integration behavior.
