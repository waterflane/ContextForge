"""Opt-in Index v3 regression against a user-selected local Qwen server."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from contextforge.benchmarks import (
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
    run_paired_answer_regression,
)
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import (
    ContextPlanningMode,
    load_file_code_map,
    load_manifest,
    retrieve_context_candidates,
)
from contextforge.models import (
    OpenAICompatibleModelProvider,
    ProviderConfiguration,
)


@dataclass(frozen=True, slots=True)
class LiveCase:
    task: str
    expected_path: str
    expected_symbol: str
    assertion: str


CASES = (
    LiveCase(
        task=(
            "Explain how retrieve_context_candidates obtains and validates an "
            "Evidence Plan, including deterministic fallback."
        ),
        expected_path="src/contextforge/intelligence/retrieval.py",
        expected_symbol="retrieve_context_candidates",
        assertion=(
            "retrieve_context_candidates performs deterministic retrieval before "
            "optional bounded evidence planning and preserves a safe fallback."
        ),
    ),
    LiveCase(
        task=(
            "Как compile_context_capsule выбирает минимально достаточные MAP, SLICE "
            "и FULL материалы, не нарушая hard token budget?"
        ),
        expected_path="src/contextforge/context/capsule.py",
        expected_symbol="compile_context_capsule",
        assertion=(
            "compile_context_capsule materializes selected evidence under freshness, "
            "representation, soft-ceiling, and hard-budget checks."
        ),
    ),
    LiveCase(
        task=(
            "Trace structural-first publication in build_repository_index and explain "
            "what remains usable when semantic enrichment fails."
        ),
        expected_path="src/contextforge/application.py",
        expected_symbol="build_repository_index",
        assertion=(
            "build_repository_index publishes a structural generation before the "
            "separate semantic enrichment stage."
        ),
    ),
    LiveCase(
        task=(
            "Show where Bridge 2.2 planning_mode is translated into Index v3 search "
            "and compile behavior."
        ),
        expected_path="src/contextforge/bridge/server.py",
        expected_symbol="_retrieve_v21",
        assertion=(
            "The Bridge server maps the negotiated planning mode into read-only "
            "retrieval and compilation behavior."
        ),
    ),
)


async def main(
    root: Path, endpoint: str, repeats: int, selected_case: int | None = None
) -> None:
    root = root.resolve()
    manifest = load_manifest(root)
    probe_configuration = ProviderConfiguration(
        provider_id="openai-compatible",
        endpoint=endpoint,
        model_id="probe",
        context_window=32_768,
        reasoning_effort="off",
        timeout_seconds=120,
        retry_limit=0,
    )
    probe = OpenAICompatibleModelProvider(probe_configuration)
    try:
        models = await probe.list_models()
    finally:
        await probe.close()
    model = next(value for value in models if "qwen" in value.casefold())
    provider = OpenAICompatibleModelProvider(
        probe_configuration.model_copy(update={"model_id": model})
    )
    failures = 0
    try:
        for repeat in range(1, repeats + 1):
            for case_number, case in enumerate(CASES, start=1):
                if selected_case is not None and selected_case != case_number:
                    continue
                started = time.perf_counter()
                deterministic = await retrieve_context_candidates(
                    root,
                    case.task,
                    manifest=manifest,
                    planning_mode=ContextPlanningMode.OFF,
                )
                planned = await retrieve_context_candidates(
                    root,
                    case.task,
                    manifest=manifest,
                    provider=provider,
                    planning_mode=ContextPlanningMode.AUTO,
                )
                budget = ContextBudget(
                    context_window_tokens=32_768,
                    response_tokens=2_048,
                    safety_margin_tokens=1_024,
                )
                compiled = compile_context_capsule(
                    root,
                    case.task,
                    planned,
                    budget=budget,
                )
                top_five = tuple(item.path for item in planned.candidates[:5])
                material = tuple(
                    (*compiled.capsule.working_set, *compiled.capsule.task_context)
                )
                material_paths = tuple(item.path for item in material)
                code_map = load_file_code_map(
                    root, case.expected_path, manifest=manifest
                )
                symbol = next(
                    (
                        item
                        for item in code_map.symbols
                        if item.name == case.expected_symbol
                    ),
                    None,
                )
                paired = None
                if symbol is not None and case.expected_path in material_paths:
                    paired = await run_paired_answer_regression(
                        root,
                        case.task,
                        (
                            BenchmarkExpectedAssertion(
                                assertion_id=f"case-{case_number}",
                                description=case.assertion,
                            ),
                        ),
                        (
                            BenchmarkSourceRange(
                                path=case.expected_path,
                                start_line=symbol.source_range.start_line,
                                end_line=symbol.source_range.end_line,
                            ),
                        ),
                        compiled,
                        provider,
                    )
                plan = planned.evidence_plan
                plan_valid = plan is not None and plan.diagnostics.status == "planned"
                retrieval_ok = case.expected_path in top_five
                capsule_ok = case.expected_path in material_paths
                paired_ok = paired is not None and paired.quality_not_lower
                failures += not (
                    plan_valid and retrieval_ok and capsule_ok and paired_ok
                )
                row = {
                    "case": case_number,
                    "repeat": repeat,
                    "model": model,
                    "task": case.task,
                    "deterministic_top_5": [
                        item.path for item in deterministic.candidates[:5]
                    ],
                    "planned_top_5": list(top_five),
                    "provider_calls": planned.provider_calls,
                    "plan_valid": plan_valid,
                    "plan_sufficiency": plan.sufficiency if plan else None,
                    "planning_diagnostics": (
                        plan.diagnostics.model_dump(mode="json") if plan else None
                    ),
                    "material": [
                        {
                            "path": item.path,
                            "representation": item.representation,
                            "tokens": item.token_count,
                            "evidence_ids": list(item.evidence_ids),
                        }
                        for item in material
                    ],
                    "capsule_tokens": compiled.token_count,
                    "soft_ceiling_tokens": int(budget.available_tokens * 0.30),
                    "elapsed_ms": round((time.perf_counter() - started) * 1_000),
                    "retrieval_ok": retrieval_ok,
                    "capsule_ok": capsule_ok,
                    "paired": paired.model_dump(mode="json") if paired else None,
                    "paired_ok": paired_ok,
                }
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await provider.close()
    if failures:
        raise SystemExit(f"{failures} live Index v3 checks failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:1919/v1")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--case", type=int, choices=range(1, len(CASES) + 1))
    arguments = parser.parse_args()
    asyncio.run(
        main(arguments.root, arguments.endpoint, arguments.repeats, arguments.case)
    )
