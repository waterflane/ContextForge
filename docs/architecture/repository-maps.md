# Deterministic repository maps v3

Index v3 stores `orientation`, `architecture`, `conventions`, and `features`
maps as deterministic projections of the pinned relationship graph, CodeMaps,
and validated grounded Semantic Cards. There are no repository-wide model
calls. Model prose can contribute only after it has been accepted into a
grounded card and retains interpretation provenance.

## Orientation

The structural transaction always publishes the full structured orientation
map. It contains every indexed file, repository/module hierarchy, centrality,
relationship counts, entrypoint/test/config signals, and compact verified
structure. A renderer shows every file record when the caller's token budget
permits it. Otherwise it emits the complete repository/module hierarchy and
deepens the most central modules without silently presenting a partial flat
file list as complete.

`load_orientation_map()` pins one manifest for the whole read. The top-level
`contextforge map` command and Bridge/MCP/HTTP `map` operations expose the same
read-only projection.

## Architecture, conventions, and features

The enriched transaction aggregates module responsibilities, cross-module
relationships, entrypoint/handler flows, configuration consumers, source/test
connectivity, conventions, and grounded feature concepts. Deterministic facts
and interpretations remain visibly separate. A missing or failed card reduces
map detail but cannot create an ungrounded replacement claim.

Maps use only paths and identities in the pinned generation. A changed or
deleted participating file invalidates affected module projections. Incremental
update rebuilds changed-file edges, neighboring projections, and affected
module maps without rereading unchanged source.

## Graph provenance and centrality

Relationship edges cover file/symbol imports, references, calls,
source-to-test, entrypoint-to-handler, and config-to-consumer links. Every edge
is `verified`, `best-effort-structural`, or `model-inferred`. Reverse
dependencies, fan-in/out, and test connectivity are stored alongside the graph.

PageRank is deterministic: damping `0.85`, at most 100 iterations, tolerance
`1e-9`, canonical node order, and no contribution from `model-inferred` edges.
These scores are retrieval hints, never authority to read a stale or
unauthorized source file.
