import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from contextforge.application import (
    expand_discovery,
    package_verified_context,
    prepare_discovery_candidates,
    read_verified_context,
)
from contextforge.context import SelectedFileTooLargeError
from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryCandidatePreparation,
    DiscoveryExpansionRequest,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoverySelection,
    DiscoverySelectionItem,
    DiscoveryUnavailableError,
    discover_repository,
)
from contextforge.intelligence import (
    acquire_index_lock,
    build_structural_index,
    load_manifest,
)
from contextforge.models import (
    FakeModelProvider,
    ModelRequest,
    ProviderConfiguration,
)
from contextforge.repositories import ProjectSnapshot, scan_repository


def _snapshot(root: Path, files: dict[str, str]) -> ProjectSnapshot:
    for path, content in files.items():
        destination = root.joinpath(*path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="")
    return scan_repository(root)


def _index(snapshot: ProjectSnapshot) -> None:
    with acquire_index_lock(snapshot.root, "application-contract") as lock:
        build_structural_index(snapshot, lock)


def _selection(preparation: DiscoveryCandidatePreparation) -> DiscoverySelection:
    return DiscoverySelection(
        preparation_id=preparation.preparation_id,
        items=(
            DiscoverySelectionItem(
                candidate_id=min(
                    preparation.candidates,
                    key=lambda item: item.candidate_id,
                ).candidate_id
            ),
        ),
    )


@pytest.mark.parametrize(
    ("mode", "build_index", "expected_status", "expected_origin"),
    [
        (DiscoveryMode.FRESH, False, "not_used", "fresh"),
        (DiscoveryMode.INDEXED, True, "current", "indexed"),
        (DiscoveryMode.HYBRID, True, "current", "indexed"),
        (DiscoveryMode.HYBRID, False, "unavailable", "hybrid"),
    ],
)
def test_model_free_candidate_preparation_preserves_mode_semantics(
    tmp_path: Path,
    mode: DiscoveryMode,
    build_index: bool,
    expected_status: str,
    expected_origin: str,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        {"src/service.py": "def serve():\n    return 1\n", "README.md": "service\n"},
    )
    if build_index:
        _index(snapshot)

    preparation = prepare_discovery_candidates(
        snapshot,
        DiscoveryRequest(task="Find service", mode=mode),
    )

    assert preparation.mode is mode
    assert preparation.index_status == expected_status
    assert preparation.candidates
    assert {item.evidence_origin for item in preparation.candidates} == {
        expected_origin
    }
    assert preparation.budget_usage.model_calls == 0
    assert all(item.source_sha256 for item in preparation.candidates)
    assert "provider" not in inspect.signature(prepare_discovery_candidates).parameters
    assert (
        preparation.index_generation_id == load_manifest(tmp_path).generation_id
        if build_index
        else preparation.index_generation_id is None
    )
    if mode is DiscoveryMode.HYBRID and not build_index:
        assert any(
            item.code == "hybrid-index-unavailable" for item in preparation.warnings
        )


def test_preparation_is_immutable_and_serializes_deterministically(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"a.py": "A = 1\n", "b.py": "B = 2\n"})
    request = DiscoveryRequest(task="Find A", mode=DiscoveryMode.FRESH)
    first = prepare_discovery_candidates(snapshot, request)
    second = prepare_discovery_candidates(snapshot, request)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert json.dumps(
        first.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ) == json.dumps(
        second.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ValidationError):
        first.task = "changed"
    with pytest.raises(ValidationError):
        first.candidates[0].path = "changed.py"


def test_model_free_preparation_honors_cooperative_cancellation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"a.py": "A = 1\n"})
    cancellation = asyncio.Event()
    cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        prepare_discovery_candidates(
            snapshot,
            DiscoveryRequest(task="A", mode=DiscoveryMode.FRESH),
            cancellation=cancellation,
        )


def test_expansion_reuses_path_policy_and_returns_verified_source_identity(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"pkg/a.py": "def alpha():\n    return 1\n"})
    preparation = prepare_discovery_candidates(
        snapshot, DiscoveryRequest(task="alpha", mode=DiscoveryMode.FRESH)
    )
    candidate = preparation.candidates[0]
    result = expand_discovery(
        snapshot,
        preparation,
        DiscoveryExpansionRequest(
            preparation_id=preparation.preparation_id,
            operation="read_file",
            arguments={"path": candidate.path},
        ),
    )
    invalid = expand_discovery(
        snapshot,
        preparation,
        DiscoveryExpansionRequest(
            preparation_id=preparation.preparation_id,
            operation="read_file",
            arguments={"path": "../secret"},
        ),
    )

    assert result.ok
    assert result.data["source_sha256"] == candidate.source_sha256
    assert result.budget_usage.model_calls == 0
    assert invalid.code == "invalid_input"
    assert "action_id" not in result.model_dump()
    assert "tool_name" not in result.model_dump()
    assert "step" not in result.model_dump()


def test_expansion_carries_budget_usage_and_enforces_step_limit(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"a.py": "A = 1\n"})
    preparation = prepare_discovery_candidates(
        snapshot,
        DiscoveryRequest(
            task="A",
            mode=DiscoveryMode.FRESH,
            budget=DiscoveryBudget(max_steps=1),
        ),
    )
    first = expand_discovery(
        snapshot,
        preparation,
        DiscoveryExpansionRequest(
            preparation_id=preparation.preparation_id,
            operation="get_repository_overview",
        ),
    )
    assert first.budget_usage.steps == 1
    with pytest.raises(RuntimeError, match="steps"):
        expand_discovery(
            snapshot,
            preparation,
            DiscoveryExpansionRequest(
                preparation_id=preparation.preparation_id,
                operation="get_repository_overview",
                budget_usage=first.budget_usage,
            ),
        )


def test_verified_read_and_package_are_pinned_and_budgeted(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"a.py": "A = 1\n", "b.py": "B = 2\n"})
    preparation = prepare_discovery_candidates(
        snapshot, DiscoveryRequest(task="Find A", mode=DiscoveryMode.FRESH)
    )
    selection = _selection(preparation)
    verified = read_verified_context(snapshot, preparation, selection)
    package = package_verified_context(snapshot, verified)

    assert verified.source_snapshot_digest == preparation.source_snapshot_digest
    assert verified.files[0].source_sha256 in {
        item.source_sha256 for item in preparation.candidates
    }
    assert package.files[0].source_sha256 == verified.files[0].source_sha256
    assert package.files[0].blocks[0].text == verified.files[0].blocks[0].text
    assert package.title == preparation.task

    limited = prepare_discovery_candidates(
        snapshot,
        DiscoveryRequest(
            task="Find A",
            mode=DiscoveryMode.FRESH,
            budget=DiscoveryBudget(max_context_bytes=1),
        ),
    )
    with pytest.raises(SelectedFileTooLargeError):
        read_verified_context(snapshot, limited, _selection(limited))


def test_source_changes_invalidate_preparation_and_verified_context(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"a.py": "A = 1\n"})
    preparation = prepare_discovery_candidates(
        snapshot, DiscoveryRequest(task="A", mode=DiscoveryMode.FRESH)
    )
    (tmp_path / "a.py").write_text("A = 2\n", encoding="utf-8", newline="")
    current = scan_repository(tmp_path)

    with pytest.raises(RuntimeError, match="snapshot"):
        read_verified_context(current, preparation, _selection(preparation))


def test_stale_index_is_rejected_or_disclosed_by_mode(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"a.py": "A = 1\n", "b.py": "B = 1\n"})
    _index(snapshot)
    (tmp_path / "b.py").write_text("B = 2\n", encoding="utf-8", newline="")
    current = scan_repository(tmp_path)

    hybrid = prepare_discovery_candidates(
        current, DiscoveryRequest(task="A", mode=DiscoveryMode.HYBRID)
    )
    assert hybrid.index_status == "stale"
    assert hybrid.stale_index_paths == ("b.py",)
    assert any(item.code == "stale-index-coverage" for item in hybrid.warnings)

    stale_only = _snapshot(tmp_path / "only", {"only.py": "A = 1\n"})
    _index(stale_only)
    (stale_only.root / "only.py").write_text("A = 2\n", encoding="utf-8", newline="")
    with pytest.raises(DiscoveryUnavailableError):
        prepare_discovery_candidates(
            scan_repository(stale_only.root),
            DiscoveryRequest(task="A", mode=DiscoveryMode.INDEXED),
        )


def test_existing_model_assisted_discovery_remains_compatible(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"service.py": "def serve():\n    return 1\n"})
    _index(snapshot)

    def responder(request: ModelRequest, _: int) -> str:
        records = cast(
            list[dict[str, Any]], request.trusted_code_map_facts["candidates"]
        )
        return json.dumps(
            {
                "schema_version": 1,
                "candidate_ids": [records[0]["candidate_id"]],
                "summary": "Selected service implementation.",
            }
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="discovery-v1",
            timeout_seconds=2,
            retry_limit=0,
        ),
        responder=responder,
    )
    result = asyncio.run(
        discover_repository(
            snapshot,
            provider,
            DiscoveryRequest(task="Find service", mode=DiscoveryMode.INDEXED),
        )
    )

    assert result.status == "complete"
    assert result.final_selection is not None
    assert result.budget_usage.model_calls == 1
