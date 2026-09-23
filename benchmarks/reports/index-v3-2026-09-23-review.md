# Index v3 final review (2026-09-23)

Status: **release gates failed; the live acceptance run is incomplete**. No token-savings headline is valid.

## Method

- Pinned, disposable local clones of ContextForge, dsh-contextforge, PlanUp and SyncPlayer; external source repositories were not edited.
- Manifest: 16 tasks (13 broad, 3 exact-symbol), with tuning and holdout splits. The main Qwen run requested three repeats per task.
- Main run: `Qwen3.6-35B-A3B-NVFP4` at `http://127.0.0.1:1919/v1`, temperature 0, reasoning off, configured context window 131,072. The server actually rejected prompts above 8,203 tokens. The run stopped after 39/48 deterministic observations and 38/48 planner/materialization observations.
- A separate DSH exact-symbol diagnostic used the actual 8,203-token window, one semantic request and three repeats. Do not combine its figures with the main run as a single controlled dataset.
- Raw, append-only observations: `index-v3-2026-09-23-partial-live.jsonl` and `index-v3-2026-09-23-dsh-narrow-live.jsonl` in this directory.

## Lifecycle and storage

| Repository | Cold structural | No-op update | Semantic offline | Active/source | Largest shard |
| --- | ---: | ---: | ---: | ---: | ---: |
| ContextForge | 88.86 s | 8.15 s | 131.81 s; 13 calls; partial | 14.44x | 4,194,063 B |
| dsh-contextforge | 8.79 s | 1.14 s | 138.41 s; 24 calls; partial | 17.25x | 1,544,352 B |
| PlanUp | 6.06 s | 0.73 s | 3.11 s; 24 calls; partial | 25.86x | 1,205,857 B |
| SyncPlayer | 9.20 s | 0.99 s | 3.18 s; 24 calls; partial | 17.18x | 1,553,810 B |

All clone no-op updates preserved the generation and extracted zero CodeMaps. Every measured shard was below 4 MiB. The 12x active-index gate failed in all four repositories; the 2 s no-op gate failed for ContextForge. Semantic token accounting is unavailable in the current result contract (`estimated_tokens: null`), so that reporting gate remains unverified.

Graph import, call, reference and source-test routes were nonzero in all four clones. Their per-kind provenance is retained in the raw observations; best-effort TypeScript emitted-import substitutions were not promoted to verified. SyncPlayer had six verified entrypoint-handler edges.

## Retrieval, planning and answers

- Main-run completed deterministic observations: 39; mean required-file recall@5 0.523, mean precision@5 0.354, mean warm latency 346 ms, maximum 1,741 ms. Deterministic provider calls were zero.
- Main-run completed planner observations: 38; 9 planned and 29 fallback, mean latency 12.91 s, mean 1.24 calls, maximum 3 calls. Planner input/output estimates and fallback reasons are in the raw records.
- Main-run completed materializations: 38; mean required-file recall 0.368. All had effective status `insufficient`; no false `sufficient` was observed.
- Main-run final answers: 0 completed, 38 errors (12 request errors and 26 circuit-open errors). Citation containment, majority grounding, oracle quality and valid savings therefore cannot be claimed.
- DSH narrow diagnostic: semantic enrichment completed one Qwen request in 24.63 s. In all three repeats deterministic top-5 contained all three required files; the planner used three calls, selected a plan that materialized only unrelated `tests/config.spec.ts`, and produced materialized recall 0 and range recall 0. Each final answer failed with a truncated structured response.
- Independent fresh-process check: 100/100 graph and top-5 retrieval digests matched across distinct `PYTHONHASHSEED` values.

## Gate disposition

**Failed:** required-file recall >=0.90; precision@5 >0.80; materialized recall >=0.90; range recall >=0.85 on the DSH diagnostic; 30% quality-valid input savings (no qualifying answers); warm retrieval <1 s for every observation; no-op <2 s on ContextForge; active amplification <=12x; full branch coverage >=90% (89.75%).

**Passed in measured scope:** graph routes/provenance present; no-op identity and zero CodeMap extraction on disposable clones; shard <=4 MiB; deterministic provider calls zero; planner calls <=3; no sufficient-plus-partial evidence observed; 100/100 hash-seed reload; ruff format/check, mypy and diff whitespace checks.

**Unverified:** 100% citation containment, all-holdout majority groundedness, quality versus manual oracle, final-answer input savings and complete three-repeat 16-task live coverage. The required local-only index update passed after generated pytest fixtures were removed; it extracted 194 CodeMaps and reused 47 because this review changed the workspace, so it was not a no-op measurement.

Do not treat the observed capsule token reduction as savings: the coverage and answer-quality gates failed. No release commit, push or PR was created by this review.
