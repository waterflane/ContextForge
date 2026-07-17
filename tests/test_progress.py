import asyncio
import io
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import contextforge.application as application_module
from contextforge import (
    NO_OP_PROGRESS_OBSERVER,
    NoOpProgressObserver,
    ProgressEvent,
    ProgressReporter,
    ProgressStatus,
)
from contextforge.application import build_repository_index, inspect_repository_index
from contextforge.cli.progress import CLIProgressRenderer
from contextforge.discovery import DiscoveryRequest, DiscoveryRunRecord
from contextforge.intelligence import load_manifest
from contextforge.models import ModelProvider, ModelProviderError
from contextforge.repositories import ProjectSnapshot


def _event(**overrides: object) -> ProgressEvent:
    values: dict[str, object] = {
        "operation_id": "operation-1",
        "operation_type": "repository.scan",
        "phase_id": "inventory",
        "message": "Scanning repository files.",
        "completed": 1,
        "total": 4,
        "percentage": 25,
        "status": ProgressStatus.RUNNING,
        "parent_operation_id": None,
        "metadata": {"units": "phases", "cached": False},
        "sequence": 2,
    }
    values.update(overrides)
    return ProgressEvent.model_validate(values)


def test_progress_event_is_closed_frozen_and_json_serializable() -> None:
    event = _event()

    payload = event.model_dump(mode="json")
    assert json.loads(event.model_dump_json()) == payload
    assert ProgressEvent.model_validate_json(event.model_dump_json()) == event
    assert payload == {
        "schema_version": 1,
        "operation_id": "operation-1",
        "operation_type": "repository.scan",
        "phase_id": "inventory",
        "message": "Scanning repository files.",
        "completed": 1.0,
        "total": 4.0,
        "percentage": 25.0,
        "status": "running",
        "parent_operation_id": None,
        "metadata": {"units": "phases", "cached": False},
        "sequence": 2,
        "indeterminate": False,
    }
    with pytest.raises(ValidationError):
        event.message = "Changed"
    with pytest.raises(ValidationError):
        _event(unknown=True)


@pytest.mark.parametrize("percentage", [-0.01, 100.01, float("nan")])
def test_progress_event_rejects_invalid_percentages(percentage: float) -> None:
    with pytest.raises(ValidationError):
        _event(percentage=percentage)


def test_progress_event_validates_work_terminal_state_and_metadata() -> None:
    assert _event(status="running").status is ProgressStatus.RUNNING
    with pytest.raises(ValidationError, match="completed work cannot exceed"):
        _event(completed=5, total=4)
    with pytest.raises(ValidationError, match="must report 100 percent"):
        _event(status=ProgressStatus.COMPLETED)
    with pytest.raises(ValidationError, match="only completed"):
        _event(status=ProgressStatus.FAILED, percentage=100)
    with pytest.raises(ValidationError, match="own parent"):
        _event(parent_operation_id="operation-1")
    with pytest.raises(ValidationError):
        _event(metadata={"not_json": Path("repository")})
    with pytest.raises(ValidationError, match="JSON serializable"):
        _event(metadata={"not_finite": float("inf")})


def test_reporter_enforces_monotonic_percentages_and_one_terminal_event() -> None:
    events: list[ProgressEvent] = []
    reporter = ProgressReporter(
        "operation-1", "repository.scan", observer=events.append
    )

    reporter.report("scan", "Starting scan.", percentage=10)
    reporter.report("scan", "Continuing scan.", percentage=10)
    reporter.report("summarize", "Summarizing scan.", percentage=75)
    with pytest.raises(ValueError, match="cannot decrease"):
        reporter.report("scan", "Regressing scan.", percentage=74)
    completed = reporter.complete(message="Scan completed.")

    assert [event.percentage for event in events] == [10, 10, 75, 100]
    assert [event.sequence for event in events] == [0, 1, 2, 3]
    assert completed.status is ProgressStatus.COMPLETED
    assert completed.percentage == 100
    with pytest.raises(RuntimeError, match="terminal event"):
        reporter.report("extra", "Too late.", percentage=100)


def test_scaled_child_events_use_the_same_monotonic_parent_contract() -> None:
    events: list[ProgressEvent] = []
    parent = ProgressReporter(
        "parent", "repository.index.build", observer=events.append
    )
    child = ProgressReporter(
        "child",
        "repository.semantic_index",
        observer=parent.scaled_observer(40, 70, phase_prefix="semantic"),
    )

    parent.report("structural", "Structural work completed.", percentage=40)
    child.report("analyze", "Analyzing files.", percentage=50)
    child.complete(message="Semantic analysis completed.")
    parent.complete()

    assert [event.percentage for event in events] == [40, 55, 70, 100]
    assert events[1].phase_id == "semantic.analyze"
    assert events[1].metadata["child_operation_id"] == "child"


def test_failure_and_cancellation_preserve_last_percentage() -> None:
    failed = ProgressReporter("failed-1", "repository.index")
    failed.report("build", "Building index.", percentage=60)
    failed_event = failed.fail(metadata={"error_type": "OSError"})

    cancelled = ProgressReporter("cancelled-1", "repository.index")
    cancelled.report("build", "Building index.", percentage=35)
    cancelled_event = cancelled.cancel()

    assert (failed_event.status, failed_event.percentage) == (
        ProgressStatus.FAILED,
        60,
    )
    assert failed_event.metadata == {"error_type": "OSError"}
    assert (cancelled_event.status, cancelled_event.percentage) == (
        ProgressStatus.CANCELLED,
        35,
    )


def test_no_op_observers_and_indeterminate_work_are_supported() -> None:
    for observer in (NoOpProgressObserver(), NO_OP_PROGRESS_OBSERVER):
        reporter = ProgressReporter("operation-1", "discovery", observer=observer)
        event = reporter.report(
            "model_call",
            "Waiting for bounded model analysis.",
            percentage=20,
            completed=0,
            total=None,
            indeterminate=True,
        )
        assert event.indeterminate is True
        assert event.total is None
        assert reporter.complete().percentage == 100


def test_observer_failures_are_isolated_and_parent_metadata_is_preserved() -> None:
    observed: list[ProgressEvent] = []

    def broken_observer(event: ProgressEvent) -> None:
        observed.append(event)
        raise RuntimeError("observer unavailable")

    reporter = ProgressReporter(
        "child-1",
        "repository.semantic_index",
        observer=broken_observer,
        parent_operation_id="parent-1",
        metadata={"repository": "example"},
    )
    first = reporter.report(
        "analyze",
        "Analyzing repository.",
        percentage=50,
        metadata={"phase_weight": 0.5},
    )
    reporter.complete()

    assert first.parent_operation_id == "parent-1"
    assert first.metadata == {
        "repository": "example",
        "phase_weight": 0.5,
    }
    assert reporter.observer_error_count == 2
    assert len(observed) == 2


def test_sync_application_operation_emits_progress_and_failure(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    events: list[ProgressEvent] = []

    result = inspect_repository_index(
        tmp_path,
        provider_configuration=None,
        progress=events.append,
        operation_id="inspect-1",
    )

    assert result.initialized is False
    assert events[-1].status is ProgressStatus.COMPLETED
    assert events[-1].percentage == 100
    failed_events: list[ProgressEvent] = []
    with pytest.raises(FileNotFoundError):
        inspect_repository_index(
            tmp_path / "missing",
            provider_configuration=None,
            progress=failed_events.append,
            operation_id="inspect-failed",
        )
    assert failed_events[-1].status is ProgressStatus.FAILED
    assert failed_events[-1].percentage == 0


def test_async_index_build_is_unchanged_by_broken_observer(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    events: list[ProgressEvent] = []

    def broken_observer(event: ProgressEvent) -> None:
        events.append(event)
        raise RuntimeError("optional observer failed")

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
            progress=broken_observer,
            operation_id="build-1",
        )
    )

    assert load_manifest(tmp_path) == report.manifest
    assert events[-1].status is ProgressStatus.COMPLETED
    assert events[-1].percentage == 100
    assert [event.percentage for event in events] == sorted(
        event.percentage for event in events
    )


def test_incremental_index_progress_credits_reused_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    events: list[ProgressEvent] = []

    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
            update_only=True,
            progress=events.append,
            operation_id="update-reuse",
        )
    )

    structural = next(
        event
        for event in events
        if event.phase_id == "structural_index" and event.percentage == 42
    )
    assert structural.metadata["extracted"] == 0
    assert structural.metadata["reused"] == structural.total
    assert events[0].percentage == 0
    assert events[-1].percentage == 100
    assert [event.percentage for event in events] == sorted(
        event.percentage for event in events
    )


def test_provider_failure_preserves_progress_and_is_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[ProgressEvent] = []

    async def fail(*args: object, **kwargs: object) -> DiscoveryRunRecord:
        del args, kwargs
        raise ModelProviderError("provider failed")

    monkeypatch.setattr(application_module, "discover_repository", fail)
    with pytest.raises(ModelProviderError, match="provider failed"):
        asyncio.run(
            application_module.suggest_repository_context(
                cast(ProjectSnapshot, object()),
                cast(ModelProvider, object()),
                cast(DiscoveryRequest, object()),
                progress=events.append,
            )
        )

    assert events[-1].status is ProgressStatus.FAILED
    assert events[-1].percentage < 100


def test_cli_renderer_is_discrete_non_ansi_and_suppresses_duplicate_updates() -> None:
    stream = io.StringIO()
    renderer = CLIProgressRenderer(stream)
    renderer(_event(operation_type="repository.index.build", percentage=10.2))
    renderer(_event(operation_type="repository.index.build", percentage=10.8))
    renderer(
        _event(
            operation_type="repository.index.build",
            percentage=11,
            sequence=3,
        )
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("Indexing repository: 10%")
    assert "\x1b" not in stream.getvalue()


def test_async_application_cancellation_emits_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[ProgressEvent] = []

    async def cancelled_discovery(
        *args: object, **kwargs: object
    ) -> DiscoveryRunRecord:
        raise asyncio.CancelledError

    monkeypatch.setattr(application_module, "discover_repository", cancelled_discovery)

    async def exercise() -> None:
        await application_module.suggest_repository_context(
            cast(ProjectSnapshot, object()),
            cast(ModelProvider, object()),
            cast(DiscoveryRequest, object()),
            progress=events.append,
            operation_id="suggest-cancelled",
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())
    assert events[-1].status is ProgressStatus.CANCELLED
    assert events[-1].percentage == 0
