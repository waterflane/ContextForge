# Find functions and prepare their source context

ContextForge remains a Python package. Install `contextforge-repo` from PyPI for
the published version, or install the local beta wheel for `0.6.0b1`. The same
commands and Python imports work in both cases; a presentation server is not
required.

## Find files for a task

Build a structural index, then ask for an existing symbol or describe an action:

```bash
python -m contextforge index build /path/to/repository --provider none
python -m contextforge context suggest /path/to/repository \
  --task 'Find `redact_text`.' --planning off --format json
python -m contextforge context suggest /path/to/repository \
  --task 'Which function removes credentials from log messages?' \
  --planning off --format json
```

These examples target the ContextForge source repository. For another repository,
use its own symbol names and behavior. Explicit symbols have priority; descriptive
queries use the indexed vocabulary and verified graph. The CLI returns file
candidates and any resolved symbols, not a generated answer about the program.
`--planning off` makes no query-time provider calls. It does not translate queries;
descriptions using the source language and vocabulary are easier to match.

## Inspect specific function matches in Python

The optional function catalogue ranks individual Python functions using saved
CodeMaps and verified current source. It is a separate helper; its ranking does
not automatically replace CLI retrieval or the evidence planner.

```python
from pathlib import Path
from contextforge.intelligence import FunctionCatalogue, load_manifest

root = Path("/path/to/repository")
catalogue = FunctionCatalogue(root, load_manifest(root))

for query in (
    "redact_text",
    "remove credentials bearer tokens log messages",
):
    print(f"Query: {query}")
    for entry in catalogue.search(query, limit=5):
        card = entry.card(query)
        print(f"{card['path']}:{card['start_line']}-{card['end_line']}")
        print(card["name"])
        print(card["description"])
```

Descriptions summarize docstrings, declarations, and observed source calls.
Annotations are not runtime guarantees. A relevant lexical match is a candidate,
not proof that every dependency or requested behavior has been covered.

## Check the actual source selected for a task

```bash
python -m contextforge context create /path/to/repository \
  --task 'Find `redact_text`.' --planning off --compact-prompt \
  --format json --output capsule.json --prompt-output source.xml
```

Inspect `source.xml` for file paths, line numbers, and actual code. Inspect the
full JSON result for effective compilation sufficiency and missing evidence.
Finding the correct file does not mean the complete requested implementation is
present: it might have been represented by a map, excluded by budget, or require
additional dependencies. An `insufficient` capsule may still contain useful code.
The compact export does not change the selected material or sufficiency.

## Optional model-assisted planning

Configure a supported provider explicitly and use `--planning auto`. The planner
can search again, resolve symbols, inspect verified neighbors, and request source
ranges. Its selections are validated locally before compilation. Rewritten or
translated queries are search hints; they cannot create symbols or source facts.
`auto` falls back on provider failure, so inspect planning diagnostics to confirm
that a model actually completed a plan. The planner is limited to three actual
calls per query. It does not edit repository source or generate a coding-agent
answer.

Exact lookup, descriptive discovery, and source coverage should be tested
separately. Passing a few such tasks does not establish full semantic acceptance.
Descriptive ranking and complete implementation delivery remain beta limitations.
Planner request or session limits can cause fallback even when the model is
available; this is distinct from a model availability or authentication failure.
