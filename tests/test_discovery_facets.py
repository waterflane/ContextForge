import asyncio
import json
from pathlib import Path
from typing import Any, cast

from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryMode,
    DiscoveryRequest,
    discover_repository,
)
from contextforge.intelligence import acquire_index_lock, build_structural_index
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration
from contextforge.repositories import ProjectSnapshot, scan_repository


def _snapshot(root: Path, files: dict[str, str]) -> ProjectSnapshot:
    for path, content in files.items():
        destination = root.joinpath(*path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="")
    snapshot = scan_repository(root)
    with acquire_index_lock(root, "facet-tests") as lock:
        build_structural_index(snapshot, lock)
    return snapshot


def _run(
    snapshot: ProjectSnapshot,
    task: str,
    model_paths: tuple[str, ...],
    *,
    budget: DiscoveryBudget | None = None,
) -> tuple[Any, FakeModelProvider]:
    def responder(request: ModelRequest, _: int) -> str:
        candidates = cast(
            list[dict[str, Any]], request.trusted_code_map_facts["candidates"]
        )
        ids = {item["path"]: item["candidate_id"] for item in candidates}
        return json.dumps(
            {
                "schema_version": 1,
                "candidate_ids": [ids[path] for path in model_paths],
                "summary": "Selected the requested model candidates.",
            }
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="facet-tests",
            timeout_seconds=2,
            retry_limit=0,
        ),
        responder=responder,
    )
    result = asyncio.run(
        discover_repository(
            snapshot,
            provider,
            DiscoveryRequest(
                task=task,
                mode=DiscoveryMode.HYBRID,
                budget=budget or DiscoveryBudget(),
            ),
        )
    )
    return result, provider


def _paths(result: Any) -> set[str]:
    assert result.final_selection is not None
    return {item.path for item in result.final_selection.selected if item.path}


def _coverage(result: Any) -> dict[str, Any]:
    return next(
        item.data for item in result.observations if item.code == "facet_coverage"
    )


def test_compound_task_adds_startup_coverage_and_reports_facets(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "server.js": "function startServer() {}\nstartServer();\n",
            "public/app.js": "export function bootApplication() {}\n",
            "media-search.js": "export function searchMedia() {}\n",
            "public/media-source.js": "export function loadMedia() {}\n",
            "test/media-search.test.js": "test('media search', () => {});\n",
        },
    )
    before = (
        "media-search.js",
        "public/media-source.js",
        "test/media-search.test.js",
    )
    result, provider = _run(
        snapshot,
        "Explain how the application starts, how media search works, "
        "and list the relevant files",
        before,
    )

    coverage = _coverage(result)
    assert provider.call_count == 1
    assert _paths(result) == {*before, "public/app.js"}
    assert coverage["detected_facets"] == [
        "application startup",
        "media search",
        "relevant tests/files",
    ]
    assert coverage["uncovered_facets"] == []
    assert coverage["files_added_for_coverage"] in [["server.js"], ["public/app.js"]]


def test_startup_plus_feature_query_covers_both_facets(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "server.js": "function startServer() {}\n",
            "media-search.js": "export function searchMedia() {}\n",
            "notes.md": "notes\n",
        },
    )
    result, _ = _run(
        snapshot,
        "Explain application startup and media search",
        ("media-search.js",),
    )
    assert {"server.js", "media-search.js"} <= _paths(result)
    assert _coverage(result)["covered_facets"] == [
        "application startup",
        "media search",
    ]


def test_feature_implementation_is_added_before_related_test(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "media-search.js": "export function searchMedia() {}\n",
            "test/media-search.test.js": "test('search media', () => {});\n",
        },
    )
    result, _ = _run(
        snapshot,
        "Explain media search",
        ("test/media-search.test.js",),
    )
    added = [
        item
        for item in result.final_selection.selected
        if item.path == "media-search.js"
    ]
    assert len(added) == 1
    assert added[0].added_by_completeness is True
    assert added[0].model_selected is False


def test_facet_enrichment_is_bounded(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "server.js": "function startServer() {}\n",
            "media-search.js": "function searchMedia() {}\n",
            "billing-sync.js": "function syncBilling() {}\n",
            "cache-invalidation.js": "function invalidateCache() {}\n",
            "misc.js": "export const misc = true;\n",
        },
    )
    result, _ = _run(
        snapshot,
        "Explain startup, media search, billing sync, and cache invalidation",
        ("misc.js",),
    )
    coverage = _coverage(result)
    assert len(coverage["files_added_for_coverage"]) == 2
    assert coverage["uncovered_facets"]
    assert len(_paths(result)) <= 5


def test_simple_single_facet_query_is_not_enriched(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        {
            "media-search.js": "export function searchMedia() {}\n",
            "test/media-search.test.js": "test('search media', () => {});\n",
        },
    )
    result, _ = _run(snapshot, "Explain media search", ("media-search.js",))
    coverage = _coverage(result)
    assert _paths(result) == {"media-search.js"}
    assert coverage["detected_facets"] == ["media search"]
    assert coverage["files_added_for_coverage"] == []
