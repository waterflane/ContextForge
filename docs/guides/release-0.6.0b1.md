# ContextForge 0.6.0 beta 1

The Python distribution uses the canonical version `0.6.0b1`. This beta contains
Index v3.1 (storage/manifest schema 4), Context Capsule v2 and Bridge 2.2. These
protocol versions are separate from the Python package version.

## Scope

The release includes structural-first immutable generations, grounded semantic
enrichment, deterministic retrieval, bounded agentic evidence planning and strict
source/budget validation. Legacy manual packages and handoffs remain available.
Search, symbol, graph, map and grounded query-expansion actions are retained.

Selected improvements from the local presentation are part of the core package:

- smaller planner menus that retain required evidence and resolved anchors;
- preserved verified discovery after a later model failure;
- mandatory source material allowed above the automatic soft target only when
  it fits the unchanged hard budget;
- source-bound function descriptions and deterministic function-level discovery;
- compact, numbered model-input source projection with a separate full capsule.

The presentation chat, themes, language switches, saved conversations and model
answer worker are not part of this release. No presentation-specific planner
replaces the existing agentic planner. Function catalogue matches are discovery
hints; declarations and annotations do not prove runtime behavior.

## Install a local artifact

Create a new Python 3.12 or newer environment and install the built wheel:

```bash
python -m venv .venv
python -m pip install /path/to/contextforge_repo-0.6.0b1-py3-none-any.whl
python -m contextforge --version
contextforge doctor
```

On Windows, use `.venv/Scripts/python.exe`; on Linux use `.venv/bin/python`.
The source distribution can be installed instead of the wheel. Install runtime
dependencies from a network connection or an appropriate local wheel cache.

## Use existing source context

```bash
contextforge index build /path/to/repository --provider none
contextforge context create /path/to/repository \
  --task "Explain the available context budget calculation" --planning off \
  --compact-prompt --format json --output capsule.json --prompt-output source.xml
```

`source.xml` is the compact model input; `capsule.json` retains provenance,
source identities, materialization coverage and effective sufficiency. Compact
export does not change selection, fill missing evidence or weaken the budget.
Callers adding instructions/history must budget their complete model request.

Python users can inspect functions with `FunctionCatalogue` from
`contextforge.intelligence`, or render an existing compiled capsule with
`render_capsule_source_context` from `contextforge.context`.

## Index migration and quality

Indexes using storage schemas 1-3 remain status-readable but require a fresh
build. Existing schema-4 generations retain their normal freshness and analyzer
checks. Upgrading the package alone does not require structural reanalysis of
unchanged source when its analyzer identities still match.

Semantic enrichment is optional and its completeness is reported explicitly.
An incomplete context remains insufficient. Citation containment, assertion
support and semantic groundedness are separate checks. This beta does not claim
full semantic acceptance, universally sufficient contexts, or quality-gated token
savings. A single-repository presentation is not the acceptance benchmark.

Private test/review outputs remain local and are excluded from distributions.
Publishing, signed release tags and public acceptance claims are separate from
preparing these local artifacts.
