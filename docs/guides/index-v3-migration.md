# Migrating to Index v3 and Context Capsule v2

## Index rebuild

Index v2 is not used by v3 retrieval. `contextforge index status PATH` recognizes
it and reports `rebuild_required`. `contextforge index update PATH` rejects it;
run a full build instead:

```bash
contextforge index build PATH --provider none
# or bounded enrichment
contextforge index build PATH --semantic-scope priority
```

The build creates a new immutable v3 generation. It does not migrate v2
semantics and does not delete old generations. Only explicit
`contextforge index clean PATH` removes generated index data.

The structural generation becomes active before model enrichment. If
enrichment fails, status may be partial but `map`, deterministic search, symbol
lookup, and compilation against structural evidence remain usable.

## CLI behavior

With an active v3 index:

- `context suggest --task ...` returns RetrievalResult/CandidateCards v3;
- task-based `context create --task ...` returns Context Capsule v2;
- manual `context create` without a task remains ContextPackage v1; and
- `context inspect`/`context review` accept both artifact generations.

Use `--legacy-discovery` and `--legacy-handoff` during migration if a consumer
still expects `FinalContextSelection` or `TaskHandoff`. Remove those flags after
the consumer accepts CandidateCards and Capsule v2.

## Budget migration

Capsule creation requires an explicit or configured model context window.
Account for prior conversation and expected response separately:

```bash
contextforge context create PATH --task "..." \
  --context-tokens 32768 \
  --history-tokens 6000 \
  --response-tokens 4096 \
  --safety-margin-tokens 1024
```

`--working-file` influences retrieval and reserves Working Set context.
`--working-lines PATH:START-END` requests exact source ranges. `--full-file`
allows FULL for an explicitly pinned file; otherwise automatic FULL applies
only to files of at most 200 lines and only when it fits.

Automatic task evidence has a soft target of 30% of the available budget.
Explicit Working Set material, requested ranges, pinned FULL files, and required
Git material may exceed the soft target but never the hard budget. Reranker
representation suggestions are advisory and receive only a bounded 10% utility
bonus.

Package, Capsule, and prompt files created inside the repository are recorded
in `.contextforge/generated-artifacts.json`. An unchanged registered artifact
is omitted from later scans and updates; editing it changes the digest and makes
it ordinary source again. No ignore-file migration is required.

## Integration migration

Bridge clients may keep negotiating 1.0, 1.1, or 2.0 unchanged. Negotiate 2.1
to use `map`, `search`, `symbol`, and `compile`, semantic scheduler options, and
`operation_timeout`. Continue correlating a timed-out index job by its
`operation_id`; client `timeout_ms` no longer cancels that job.

MCP clients should refresh `tools/list` and accept the four new read-only tools.
Development HTTP clients may use `/v1/map`, `/v1/search`, `/v1/symbol`, and
`/v1/compile`. No new interface grants source-write, shell, or Git-mutation
authority.

Python callers can migrate incrementally:

```python
from contextforge import (
    ContextBudget,
    compile_context_capsule,
    load_orientation_map,
    retrieve_context_candidates,
)


async def build_capsule(root, task):
    retrieval = await retrieve_context_candidates(root, task)
    return compile_context_capsule(
        root,
        task,
        retrieval,
        budget=ContextBudget(
            context_window_tokens=32768,
            history_tokens=6000,
            response_tokens=4096,
            safety_margin_tokens=1024,
        ),
    )
```

Legacy `ContextPackage`, `TaskHandoff`, and `compile_prompt` APIs remain
available for the deprecation period.
