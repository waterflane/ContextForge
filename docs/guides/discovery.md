# Discovery output and benchmarks

ContextForge discovery returns a validated structured selection and then renders
that selection for a person or another program. This guide covers the stabilized
discovery and benchmark behavior intended for v0.4.2.

## Interactive discovery output

`context suggest` defaults to concise text. The supported documented formats
are:

- `--format text`: the default terminal-oriented summary;
- `--format markdown`: a reviewable report; and
- `--format json`: canonical structured output for automation.

For example, all of these commands are valid in PowerShell:

```powershell
contextforge context suggest . `
  --task 'Trace configuration loading' `
  --discovery hybrid

contextforge context suggest . `
  --task 'Trace configuration loading' `
  --discovery indexed `
  --format markdown `
  --output 'selection.md'

contextforge context suggest . `
  --task 'Trace configuration loading' `
  --discovery fresh `
  --format json `
  --output 'selection.json'
```

Text includes the task, mode, confidence, provenance, selected paths and ranges,
concise warnings, and a short performance summary. `--explain` adds exact
confidence values, evidence and selection provenance, warning details, source
identities, and detailed counters. Markdown contains the same review information
in a document-oriented layout.

### Canonical results and presentation renderers

Discovery itself produces a `DiscoveryRunRecord` whose successful result is a
validated `FinalContextSelection`. This structured selection is the canonical
result. `--format json` serializes that selection directly; it is the format to
store or compare when field-level fidelity matters.

Text and Markdown are presentation renderers over the same selection. Rendering
does not run discovery again, reorder the selected-file ranking, or change the
structured result. The human renderers may omit internal identifiers from the
concise view and group repeated warnings for readability. Those presentation
choices are not additional discovery semantics.

## Discovery modes

All three modes operate on the current repository snapshot and verify selected
source before returning success. They differ in the evidence available during
the search:

- `fresh` does not load persisted file semantics or repository maps. It derives
  current structural CodeMaps in memory and uses current source inspection.
- `indexed` requires a readable active index with at least one current structural
  record. Available current indexed structure, semantics, and maps guide
  discovery; stale records and stale repository maps are unavailable and
  disclosed.
- `hybrid`, the default, starts with current index evidence, fills structural
  gaps from the current snapshot, and can investigate outside index candidates.
  If no valid index is available, it explicitly degrades to fresh structural
  discovery and reports that limitation.

The index is evidence, not source truth. A source identity mismatch during
inspection or final verification fails the run rather than returning a partial
successful selection.

### Why valid selections can differ

The modes are not expected to return identical file lists. Fresh discovery sees
current structural evidence; indexed discovery can see persisted semantic and
repository-map evidence; hybrid discovery can combine both. That can make a
different entry point, caller, test, configuration file, or supporting document
the best-supported choice in each mode.

A task can also have more than one valid answer. For example, either an
executable entry point or the manifest that invokes it may establish startup
behavior. Benchmarks represent this with `required_files_any` rather than
forcing an arbitrary single path. A mode can therefore select a different file
set and still satisfy the same required files, acceptable alternatives,
forbidden-file rules, warning policy, facets, and budgets.

## Exact determinism and semantic stability

Canonical serialization and the text and Markdown renderers are deterministic
for the same validated object. This does not make a model-backed discovery run
deterministic.

Benchmark reports distinguish:

- exact selected-file set agreement, which ignores ranking;
- exact ordered agreement, which includes ranking;
- pairwise Jaccard similarity, which measures set overlap;
- `semantic_stability`, used when comparable model-backed runs exist; and
- `deterministic_fallback_repeatability`, used only when every complete run in
  the comparable cohort came from deterministic fallback.

`insufficient_data` means there are fewer than two complete comparable runs.
Even a 100% exact match for observed model-backed repetitions is evidence of
stability for that cohort, not proof of deterministic execution.

## Discovery benchmarks

The benchmark root contains the repositories named by the manifest's
`repository_path` fields. The manifest is supplied separately with `--tasks`:

```powershell
contextforge benchmark discovery 'C:\Repositories' `
  --tasks '.\benchmarks\discovery.json' `
  --modes 'fresh,indexed,hybrid' `
  --repeat 3
```

Text is the default benchmark output. Markdown and canonical JSON use the same
format names as interactive discovery:

```powershell
contextforge benchmark discovery 'C:\Repositories' `
  --tasks '.\benchmarks\discovery.json' `
  --modes 'fresh,hybrid' `
  --format markdown `
  --output '.\benchmark-report.md'

$benchmark = contextforge benchmark discovery 'C:\Repositories' `
  --tasks '.\benchmarks\discovery.json' `
  --modes 'fresh' `
  --format json | ConvertFrom-Json
```

`--modes` selects a comma-separated subset of the manifest modes. `--repeat`
overrides task-level and per-mode repeat counts. `--config` accepts either an
explicit project configuration TOML file or a repository root containing one;
without it, provider configuration is resolved from the benchmark root. The
runner records failures alongside successful runs, and exit code 3 means the
benchmark completed with a task, expectation, or budget failure. The result is
still emitted. Invalid usage or configuration exits 2 before execution.

### Manifest structure

Manifests are closed, versioned JSON objects. Unknown fields and invalid
portable paths fail validation before any benchmark task runs. Tasks are sorted
by unique `task_id`; path, facet, and warning lists are sorted and unique; and
modes use `fresh`, `indexed`, `hybrid` order.

```json
{
  "schema_version": 1,
  "suite_name": "configuration-discovery",
  "tasks": [
    {
      "task_id": "configuration-loading",
      "repository_path": "ContextForge",
      "task": "Find configuration loading and its focused tests.",
      "modes": [
        "fresh",
        "indexed",
        "hybrid"
      ],
      "repeat_count": 3,
      "include_paths": [],
      "exclude_paths": [],
      "required_files_all": [
        "src/contextforge/project_config.py"
      ],
      "required_files_any": [
        [
          "tests/test_config.py",
          "tests/test_model_providers.py"
        ]
      ],
      "forbidden_files": [],
      "expected_facets": [
        "configuration loading"
      ],
      "max_selected_files": 6,
      "max_files_read": 40,
      "max_model_generations": 6,
      "max_provider_http_calls": 10,
      "allowed_warnings": [],
      "required_warnings": [],
      "index_precondition": {
        "kind": "clean"
      },
      "mode_overrides": {
        "indexed": {
          "max_files_read": 20
        }
      }
    }
  ]
}
```

The main task fields have these roles:

- `include_paths` and `exclude_paths` constrain discovery with exact snapshot
  paths.
- `required_files_all` requires every listed file. Each `required_files_any`
  group requires at least one member. `forbidden_files` must not be selected.
- `expected_facets` checks required task facets against the selected candidates'
  paths, reasons, discovery sources, and evidence.
- `allowed_warnings` permits listed warning codes; `required_warnings` requires
  them to occur. A required code must also be allowed, or its occurrence is
  still an unexpected warning.
- `max_selected_files`, `max_files_read`, `max_model_generations`, and optional
  `max_provider_http_calls` are evaluated after each run.
- `mode_overrides` replaces only explicitly supplied fields for that mode.
- `index_precondition.kind` is `clean` or `isolated-stale`. The latter requires
  `drift_path`; the runner creates a temporary isolated copy for that scenario
  and does not change the source repository or its published index.

An index precondition is valid only when the task enables indexed or hybrid
mode. Ordinary `clean` preconditions fail if source/index drift is present,
which prevents repeatability numbers from silently mixing different fixture
states.

### Quality, repeatability, and performance metrics

Metrics are calculated per comparable cohort: task, repository, mode, source
snapshot digest, index generation, and effective configuration digest must all
match. Failed and cancelled runs count toward totals but are excluded from
quality, repeatability, confidence, duration, and range calculations.

Quality metrics are aggregated over complete runs:

- required-file recall is matched `required_files_all` entries divided by
  configured `required_files_all` entries; acceptable-any groups affect pass or
  fail but not this recall metric;
- forbidden-file selection rate is selected forbidden files divided by
  configured forbidden-file opportunities; lower is better;
- expected-facet coverage is covered facets divided by configured facets; and
- discovery confidence reports mean, minimum, maximum, and spread separately
  from benchmark expectation pass/fail.

An empty expectation denominator is reported as `n/a`, not as perfect quality.
Repeatability reports exact set and order rates, every pair's Jaccard similarity
and its mean, exact warning-record-set stability, and fallback rate. Performance
reports mean duration, nearest-rank p50/p90/p95 when at least two complete runs
exist, plus observed files-read and logical model-call ranges.

### Provider and model counters

The counters separate content-bearing model output from provider HTTP traffic:

- `model_calls` is the compatibility count of initial logical model requests
  initiated, including requests rejected locally or failed before model output.
- `model_generations` counts initial content-bearing model responses.
- `repair_generations` counts content-bearing structured-repair responses.
- `provider_discovery_calls` counts provider discovery requests such as model
  listing.
- `provider_capability_calls` counts explicit capability probes.
- `auxiliary_provider_calls` in benchmark JSON is the sum of discovery and
  capability calls.
- `transport_attempts` counts generation transport attempts, including retries
  and rejected structured modes.
- `total_provider_http_calls` counts all provider HTTP requests. The discovery
  result's compatibility field `provider_http_calls` is an equal alias.

`provider_id` and `model_id` identify the configured provider and model for the
run; they are not counters. The manifest's `max_model_generations` checks initial
`model_generations`, while `max_provider_http_calls` checks the full HTTP total.
These limits therefore answer different questions and should not be expected to
match.

### Grouped warnings

Text and Markdown discovery output group warnings that share code, severity,
message, and warning confidence, then show a sorted, deduplicated union of their
primary and related paths. Benchmark text and Markdown group warnings per run by
code, severity, and message, retaining record count, sorted primary affected
paths, related paths, and pathless-record count. Different reasons or severities
remain separate groups.

JSON never applies presentation grouping: it preserves each canonical warning
record. A warning-free result is not a proof that selection is complete.

## Stdout and stderr

For `context suggest` without `--output`, stdout contains only the selected
text, Markdown, or JSON result. Progress and logs use stderr, so JSON stdout can
be parsed directly. Fatal usage, configuration, provider, and discovery errors
also use stderr and leave stdout empty. With `--output`, the result is written
atomically to that file and stdout contains the success confirmation; replacing
an existing file requires `--force`.

For `benchmark discovery` without `--output`, stdout contains only the selected
result, including when exit code 3 reports a completed regression. Progress uses
stderr. With `--output`, only the result is written to the requested file and
stdout is empty. Invalid modes, manifests, and configuration fail before
execution with empty stdout.

## Limits of model-backed repeatability

Benchmark repeatability describes observations, not a guarantee about the next
model-backed run. It is meaningful only for the recorded snapshot, index,
effective configuration, provider, model, and task. Model responses, repair
paths, transport retries, provider implementations, and model revisions can
change selections while all benchmark quality expectations still pass.

Use exact set and order rates when byte-for-byte selection identity matters, and
use Jaccard, required-file recall, forbidden selection, and facet coverage to
evaluate semantic usefulness. Do not relabel model-backed agreement as
determinism. Deterministic fallback repeatability is reported separately and
does not establish model quality.
