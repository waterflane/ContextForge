"""Opt-in, read-only-source Index v3 review on disposable pinned Git clones.

Print JSON lines so interrupted live runs retain every completed observation.
No external repository is modified; all index state lives inside each clone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from contextforge.application import build_repository_index
from contextforge.benchmarks import (
    BenchmarkSourceRange,
    load_real_repository_benchmark_manifest,
    run_paired_answer_regression,
)
from contextforge.benchmarks.real_repositories import temporary_read_only_clone
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import (
    ContextPlanningMode,
    load_relationship_graph,
    retrieve_context_candidates,
)
from contextforge.models import OpenAICompatibleModelProvider, ProviderConfiguration

REPORT_PATH: Path | None = None


def fresh_process_reloads(root: Path, task: str, count: int) -> tuple[int, int]:
    """Compare persisted graph and retrieval output across distinct hash seeds."""

    code = (
        "import asyncio,hashlib,json,sys;"
        "from contextforge.intelligence import "
        "load_manifest,load_relationship_graph,retrieve_context_candidates;"
        "root=sys.argv[1];task=sys.argv[2];manifest=load_manifest(root);"
        "graph=load_relationship_graph(root,manifest=manifest);"
        "result=asyncio.run(retrieve_context_candidates(root,task,manifest=manifest));"
        "payload=json.dumps({'graph':graph.model_dump(mode='json'),"
        "'candidates':[(item.path,item.score) for item in result.candidates[:5]]},"
        "sort_keys=True,separators=(',',':'));"
        "print(hashlib.sha256(payload.encode()).hexdigest())"
    )
    expected: str | None = None
    passed = 0
    for seed in range(count):
        completed = subprocess.run(
            [sys.executable, "-c", code, str(root), task],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            timeout=30,
        )
        digest = completed.stdout.strip() if completed.returncode == 0 else ""
        if expected is None and digest:
            expected = digest
        passed += bool(digest and digest == expected)
    return passed, count


def emit(**values: object) -> None:
    line = json.dumps(values, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    if REPORT_PATH is not None:
        with REPORT_PATH.open("a", encoding="utf-8") as output:
            output.write(line + "\n")


def required_recall(required: tuple[str, ...], observed: tuple[str, ...]) -> float:
    return len(set(required) & set(observed)) / len(required)


def range_recall(
    required: tuple[BenchmarkSourceRange, ...],
    selected: tuple[BenchmarkSourceRange, ...],
) -> float | None:
    if not required:
        return None
    total = sum(item.end_line - item.start_line + 1 for item in required)
    covered = 0
    for item in required:
        lines = {
            line
            for observed in selected
            if observed.path == item.path
            for line in range(
                max(item.start_line, observed.start_line),
                min(item.end_line, observed.end_line) + 1,
            )
        }
        covered += len(lines)
    return covered / total


def source_ranges(
    root: Path, paths: tuple[str, ...]
) -> tuple[BenchmarkSourceRange, ...]:
    return tuple(
        BenchmarkSourceRange(
            path=path,
            start_line=1,
            end_line=max(
                1, len((root / path).read_text(encoding="utf-8").splitlines())
            ),
        )
        for path in paths
    )


def material_ranges(compiled: object) -> tuple[BenchmarkSourceRange, ...]:
    capsule = compiled.capsule
    return tuple(
        BenchmarkSourceRange(
            path=item.path, start_line=region.start_line, end_line=region.end_line
        )
        for item in (*capsule.working_set, *capsule.task_context)
        for region in (
            item.ranges
            if item.representation == "slice"
            else source_ranges_for_full(item)
        )
    )


def source_ranges_for_full(item: object) -> tuple[object, ...]:
    # A MAP or SUMMARY is not verbatim source range evidence.
    if item.representation != "full":
        return ()
    from contextforge.context import CapsuleRange

    return (CapsuleRange(start_line=1, end_line=len(item.content.splitlines())),)


async def review_repository(
    root: Path,
    repository_id: str,
    tasks: tuple[object, ...],
    provider: OpenAICompatibleModelProvider | None,
    repeats: int,
    context_window: int,
    semantic_max_requests: int,
    hash_reloads: int,
) -> None:
    started = time.perf_counter()
    structural = await build_repository_index(
        root, provider=None, provider_configuration=None, semantic_scope="none"
    )
    structural_seconds = time.perf_counter() - started
    manifest = structural.manifest
    original_generation = manifest.generation_id
    started = time.perf_counter()
    noop = await build_repository_index(
        root,
        provider=None,
        provider_configuration=None,
        semantic_scope="none",
        update_only=True,
    )
    noop_seconds = time.perf_counter() - started
    graph = load_relationship_graph(root, manifest=manifest)
    edge_counts = Counter(item.kind for item in graph.edges)
    provenance = Counter((item.kind, item.provenance) for item in graph.edges)
    emit(
        phase="cold_structural",
        repository=repository_id,
        seconds=round(structural_seconds, 3),
        source_files=len(structural.snapshot.files),
        graph_edges=dict(sorted(edge_counts.items())),
        edge_provenance={
            f"{kind}:{proof}": count
            for (kind, proof), count in sorted(provenance.items())
        },
        extracted=len(structural.structural.extracted_paths),
        reused=len(structural.structural.reused_paths),
    )
    emit(
        phase="noop_update",
        repository=repository_id,
        seconds=round(noop_seconds, 3),
        generation_unchanged=noop.manifest.generation_id == original_generation,
        extracted=len(noop.structural.extracted_paths),
        reused=len(noop.structural.reused_paths),
    )
    if provider is not None:
        started = time.perf_counter()
        enriched = await build_repository_index(
            root,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
            force_reanalyze=True,
            semantic_scope="priority",
            semantic_max_requests=semantic_max_requests,
            semantic_max_input_tokens=256_000,
        )
        manifest = enriched.manifest
        semantic = enriched.semantic
        emit(
            phase="semantic_offline",
            repository=repository_id,
            seconds=round(time.perf_counter() - started, 3),
            model=provider.configuration.model_id,
            endpoint=provider.configuration.endpoint,
            context_window=context_window,
            request_count=getattr(semantic, "request_count", None),
            estimated_tokens=getattr(semantic, "estimated_tokens", None),
            failed_paths=getattr(semantic, "failed_paths", ()),
            partial=enriched.partial,
        )
    source_bytes = sum(item.size_bytes for item in structural.snapshot.files)
    active_dir = (
        root / ".contextforge" / "index" / "generations" / manifest.generation_id
    )
    active_files = tuple(path for path in active_dir.rglob("*") if path.is_file())
    active_bytes = sum(path.stat().st_size for path in active_files)
    shards = tuple(path for path in active_files if path.suffix == ".jsonl")
    artifact_bytes: Counter[str] = Counter()
    for path in active_files:
        relative = path.relative_to(active_dir)
        artifact_bytes[relative.parts[0]] += path.stat().st_size
    emit(
        phase="storage",
        repository=repository_id,
        source_bytes=source_bytes,
        active_bytes=active_bytes,
        amplification=round(active_bytes / max(source_bytes, 1), 3),
        maximum_shard_bytes=max((path.stat().st_size for path in shards), default=0),
        artifact_bytes=dict(sorted(artifact_bytes.items())),
    )
    budget = ContextBudget(
        context_window_tokens=context_window,
        response_tokens=2048,
        safety_margin_tokens=1024,
    )
    if hash_reloads:
        passed, count = fresh_process_reloads(root, tasks[0].task, hash_reloads)
        emit(
            phase="fresh_process_hash_seed_reload",
            repository=repository_id,
            passed=passed,
            total=count,
        )
    for task in tasks:
        for repetition in range(1, repeats + 1 if provider is not None else 2):
            await retrieve_context_candidates(
                root,
                task.task,
                manifest=manifest,
                planning_mode=ContextPlanningMode.OFF,
            )
            started = time.perf_counter()
            deterministic = await retrieve_context_candidates(
                root,
                task.task,
                manifest=manifest,
                planning_mode=ContextPlanningMode.OFF,
            )
            warm_ms = round((time.perf_counter() - started) * 1000, 2)
            deterministic_top5 = tuple(
                item.path for item in deterministic.candidates[:5]
            )
            emit(
                phase="deterministic_warm_query",
                repository=repository_id,
                task=task.task_id,
                split=task.dataset_split,
                repeat=repetition,
                milliseconds=warm_ms,
                provider_calls=deterministic.provider_calls,
                top5=deterministic_top5,
                required_file_recall=required_recall(
                    task.required_files, deterministic_top5
                ),
                precision_at_5=len(set(task.relevant_top5) & set(deterministic_top5))
                / max(len(deterministic_top5), 1),
            )
            if provider is None:
                selected = deterministic
            else:
                started = time.perf_counter()
                selected = await retrieve_context_candidates(
                    root,
                    task.task,
                    manifest=manifest,
                    provider=provider,
                    planning_mode=ContextPlanningMode.AUTO,
                )
                diagnostics = selected.planning_diagnostics
                emit(
                    phase="agentic_planner",
                    repository=repository_id,
                    task=task.task_id,
                    split=task.dataset_split,
                    repeat=repetition,
                    milliseconds=round((time.perf_counter() - started) * 1000, 2),
                    provider_calls=selected.provider_calls,
                    estimated_input_tokens=getattr(diagnostics, "input_tokens", None),
                    reported_output_tokens=getattr(diagnostics, "output_tokens", None),
                    status=getattr(diagnostics, "status", None),
                    fallback_reasons=getattr(diagnostics, "messages", ()),
                    top5=tuple(item.path for item in selected.candidates[:5]),
                )
            try:
                compiled = compile_context_capsule(
                    root, task.task, selected, budget=budget, manifest=manifest
                )
            except Exception as exc:
                emit(
                    phase="materialization_error",
                    repository=repository_id,
                    task=task.task_id,
                    repeat=repetition,
                    reason=type(exc).__name__,
                    detail=str(exc)[:500],
                )
                continue
            material = (*compiled.capsule.working_set, *compiled.capsule.task_context)
            paths = tuple(item.path for item in material)
            actual_ranges = material_ranges(compiled)
            effective = compiled.compilation_sufficiency
            emit(
                phase="materialized_capsule",
                repository=repository_id,
                task=task.task_id,
                split=task.dataset_split,
                repeat=repetition,
                paths=paths,
                materialized_required_file_recall=required_recall(
                    task.required_files, paths
                ),
                range_recall=range_recall(task.required_ranges, actual_ranges),
                capsule_tokens=compiled.token_count,
                effective_sufficiency=getattr(effective, "effective_status", None),
                sufficiency_reasons=getattr(effective, "reason_codes", ()),
            )
            if provider is None:
                continue
            oracle_ranges = task.required_ranges or source_ranges(
                root, task.required_files
            )
            started = time.perf_counter()
            try:
                paired = await run_paired_answer_regression(
                    root,
                    task.task,
                    task.answer_assertions,
                    oracle_ranges,
                    compiled,
                    provider,
                    ordinary_paths=task.required_files,
                )
            except Exception as exc:
                emit(
                    phase="final_answer_error",
                    repository=repository_id,
                    task=task.task_id,
                    repeat=repetition,
                    milliseconds=round((time.perf_counter() - started) * 1000, 2),
                    reason=type(exc).__name__,
                    detail=str(exc)[:500],
                )
                continue
            answer = paired.contextforge
            judge = paired.contextforge_groundedness
            candidate_top5 = tuple(item.path for item in selected.candidates[:5])
            measured_range_recall = range_recall(task.required_ranges, actual_ranges)
            passed = (
                required_recall(task.required_files, candidate_top5) >= 0.90
                and len(set(task.relevant_top5) & set(candidate_top5))
                / max(len(candidate_top5), 1)
                > 0.80
                and required_recall(task.required_files, paths) >= 0.90
                and (
                    not task.required_ranges
                    or (
                        measured_range_recall is not None
                        and measured_range_recall >= 0.85
                    )
                )
                and answer.citation_validity == 1.0
                and bool(judge and judge.passed)
                and paired.quality_not_lower
                and selected.provider_calls <= 3
            )
            emit(
                phase="final_answer",
                repository=repository_id,
                task=task.task_id,
                split=task.dataset_split,
                repeat=repetition,
                milliseconds=round((time.perf_counter() - started) * 1000, 2),
                ordinary_input_tokens=getattr(paired.ordinary, "input_tokens", None),
                capsule_input_tokens=answer.input_tokens,
                estimated_input_tokens=answer.estimated_input_tokens,
                reported_input_tokens=answer.provider_input_tokens,
                answer_http_calls=answer.provider_http_calls,
                judge_input_tokens=getattr(judge, "input_tokens", None),
                judge_http_calls=getattr(judge, "provider_http_calls", None),
                citation_containment=answer.citation_validity,
                assertion_recall=answer.assertion_recall,
                lexical_support=answer.lexical_identifier_support,
                groundedness_majority=getattr(judge, "passed", None),
                quality_not_lower_than_oracle=paired.quality_not_lower,
                quality_gate_failed=not passed,
                valid_input_savings=paired.input_token_reduction if passed else None,
            )


async def main() -> None:
    global REPORT_PATH

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/real-repository-v1.json")
    )
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repository", action="append")
    parser.add_argument("--task", action="append")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--context-window", type=int, default=8203)
    parser.add_argument("--semantic-max-requests", type=int, default=24)
    parser.add_argument("--hash-reloads", type=int, default=0)
    parser.add_argument("--endpoint", default="http://127.0.0.1:1919/v1")
    parser.add_argument("--model", default="Qwen3.6-35B-A3B-NVFP4")
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.open("x", encoding="utf-8").close()
        REPORT_PATH = arguments.report
    manifest = load_real_repository_benchmark_manifest(arguments.manifest)
    emit(
        phase="settings",
        endpoint=arguments.endpoint,
        model=arguments.model if arguments.live else None,
        context_window=arguments.context_window,
        temperature=0.0,
        reasoning_effort="off",
        repeats=arguments.repeats if arguments.live else 1,
        semantic_max_requests=arguments.semantic_max_requests if arguments.live else 0,
    )
    configuration = ProviderConfiguration(
        provider_id="openai-compatible",
        endpoint=arguments.endpoint,
        model_id=arguments.model,
        context_window=arguments.context_window,
        reasoning_effort="off",
        timeout_seconds=240,
        retry_limit=0,
    )
    provider = OpenAICompatibleModelProvider(configuration) if arguments.live else None
    try:
        for repository in manifest.repositories:
            if (
                arguments.repository is not None
                and repository.repository_id not in arguments.repository
            ):
                continue
            source = (
                arguments.sources
                / {
                    "contextforge": "ContextForge",
                    "dsh-contextforge": "dsh-contextforge",
                    "planup": "PlanUp",
                    "syncplayer": "SyncPlayer",
                }[repository.repository_id]
            )
            tasks = tuple(
                task
                for task in manifest.tasks
                if task.repository_id == repository.repository_id
                and (arguments.task is None or task.task_id in arguments.task)
            )
            try:
                with temporary_read_only_clone(source, repository.revision) as clone:
                    await review_repository(
                        clone,
                        repository.repository_id,
                        tasks,
                        provider,
                        arguments.repeats,
                        arguments.context_window,
                        arguments.semantic_max_requests,
                        arguments.hash_reloads,
                    )
            except Exception as exc:
                emit(
                    phase="repository_error",
                    repository=repository.repository_id,
                    reason=type(exc).__name__,
                    detail=str(exc)[:500],
                )
    finally:
        if provider is not None:
            await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
