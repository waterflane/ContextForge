import asyncio
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from rich.console import Console

import contextforge.application as application_module
import contextforge.cli.progress as cli_progress_module
from contextforge import (
    NO_OP_PROGRESS_OBSERVER,
    NoOpProgressObserver,
    ProgressActivity,
    ProgressEvent,
    ProgressReporter,
    ProgressStatus,
)
from contextforge.application import build_repository_index, inspect_repository_index
from contextforge.cli.progress import CLIProgressRenderer, ProgressMode
from contextforge.discovery import DiscoveryRequest, DiscoveryRunRecord
from contextforge.intelligence import load_manifest
from contextforge.models import (
    FakeModelProvider,
    ModelProvider,
    ModelProviderError,
    ProviderConfiguration,
)
from contextforge.project_config import create_model_provider
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
    assert payload["schema_version"] == 3
    assert {
        "operation_id",
        "operation_type",
        "status",
        "overall_percent",
        "phase_id",
        "phase_label",
        "phase_percent",
        "phase_weight",
        "completed_units",
        "total_units",
        "unit_type",
        "current_item",
        "last_completed_item",
        "active_items",
        "active_item_count",
        "reused_units",
        "skipped_units",
        "failed_units",
        "planned_units",
        "processed_units",
        "succeeded_units",
        "fallback_units",
        "active_units",
        "current_attempt",
        "max_attempts",
        "lifecycle_state",
        "safe_error_code",
        "safe_error_message",
        "request_elapsed_seconds",
        "operation_elapsed_seconds",
        "analyzer_kind",
        "estimated_input_tokens",
        "output_token_budget",
        "input_truncated",
        "elapsed_seconds",
        "metadata",
    } <= payload.keys()
    assert {
        "overall_percent": 25.0,
        "phase_label": "Scanning repository files",
        "phase_percent": 25.0,
        "phase_weight": 100.0,
        "completed_units": 1.0,
        "total_units": 4.0,
        "unit_type": "percent",
        "active_items": [],
        "active_item_count": 0,
        "elapsed_seconds": 0.0,
    }.items() <= payload.items()

    legacy = ProgressEvent.model_validate(
        {
            **{
                key: value
                for key, value in payload.items()
                if key
                in {
                    "operation_id",
                    "operation_type",
                    "phase_id",
                    "message",
                    "completed",
                    "total",
                    "percentage",
                    "status",
                    "parent_operation_id",
                    "metadata",
                    "sequence",
                    "indeterminate",
                }
            },
            "schema_version": 1,
        }
    )
    assert legacy.schema_version == 1
    assert legacy.overall_percent == legacy.percentage
    assert legacy.phase_label == "Scanning repository files"
    with pytest.raises(ValidationError):
        event.message = "Changed"
    with pytest.raises(ValidationError):
        _event(unknown=True)


def test_progress_api_values_are_complete_and_untruncated() -> None:
    current = "deep/path/to/the/actual-current-filename.json"
    completed = "docs/README-with-a-complete-name.md"
    failed = "licenses/LICENSE-with-a-complete-name.txt"
    event = _event(
        current_item=current,
        last_completed_item=completed,
        last_failed_item=failed,
        analyzer_kind="generic-text-semantic",
        estimated_input_tokens=123,
        output_token_budget=160,
        input_truncated=True,
        lifecycle_state="waiting_for_provider",
    )

    payload = event.model_dump(mode="json")
    assert payload["current_item"] == current
    assert payload["last_completed_item"] == completed
    assert payload["last_failed_item"] == failed
    assert payload["analyzer_kind"] == "generic-text-semantic"
    assert payload["estimated_input_tokens"] == 123
    assert payload["output_token_budget"] == 160
    assert payload["input_truncated"] is True


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
    with pytest.raises(ValidationError, match="overall_percent"):
        _event(overall_percent=24)


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
    failed.report(
        "build",
        "Building index.",
        percentage=60,
        current_item="app.py",
        active_items=("app.py",),
        active_item_count=1,
    )
    failed_event = failed.fail(metadata={"error_type": "OSError"})

    cancelled = ProgressReporter("cancelled-1", "repository.index")
    cancelled.report("build", "Building index.", percentage=35)
    cancelled_event = cancelled.cancel()

    assert (failed_event.status, failed_event.percentage) == (
        ProgressStatus.FAILED,
        60,
    )
    assert failed_event.metadata == {"error_type": "OSError"}
    assert failed_event.current_item is None
    assert failed_event.active_item_count == 0
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
        if event.phase_id == "structural_index" and event.phase_percent == 100
    )
    assert structural.metadata["extracted"] == 0
    assert structural.metadata["reused"] == structural.total
    assert events[0].percentage == 0
    assert events[-1].percentage == 100
    assert [event.percentage for event in events] == sorted(
        event.percentage for event in events
    )
    assert structural.phase_weight == 60
    assert not any(event.phase_id == "semantic_analysis" for event in events)


def test_semantic_file_events_track_current_completion_and_weight(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("B = 2\n", encoding="utf-8")
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="semantic-progress",
        concurrency_limit=2,
        retry_limit=0,
    )
    events: list[ProgressEvent] = []

    async def exercise() -> None:
        delegate = cast(FakeModelProvider, create_model_provider(configuration))
        release = asyncio.Event()
        started = 0

        class GatedProvider:
            provider_id = "fake"
            configuration = delegate.configuration

            def capabilities(self) -> object:
                return delegate.capabilities()

            async def complete_structured(
                self, request: object, *, cancellation: asyncio.Event | None = None
            ) -> object:
                nonlocal started
                if getattr(request, "purpose", None) == "file-semantics":
                    started += 1
                    if started >= 2:
                        release.set()
                    await release.wait()
                return await delegate.complete_structured(
                    cast(Any, request), cancellation=cancellation
                )

            async def close(self) -> None:
                await delegate.close()

        provider = cast(ModelProvider, GatedProvider())
        await build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=configuration,
            concurrency=2,
            progress=events.append,
        )
        await provider.close()

    asyncio.run(exercise())

    semantic = [event for event in events if event.phase_id == "semantic_analysis"]
    starts = [event for event in semantic if event.current_item is not None]
    completions = [event for event in semantic if event.last_completed_item is not None]
    assert starts
    assert all(event.activity is ProgressActivity.WAITING for event in starts)
    assert completions
    assert completions[-1].completed_units == completions[-1].total_units
    assert all(event.current_item != event.last_completed_item for event in completions)
    assert all(event.phase_weight == 63 for event in semantic)
    assert max(event.active_item_count for event in semantic) >= 2
    assert max(event.percentage for event in semantic) <= 81
    assert [event.percentage for event in events] == sorted(
        event.percentage for event in events
    )
    assert events[-1].percentage == 100
    assert [event for event in events if event.percentage == 100] == [events[-1]]
    assert events[-1].status is ProgressStatus.COMPLETED
    assert events[-1].metadata["model_id"] == "semantic-progress"


def test_incremental_semantic_reuse_completes_empty_model_phase_without_jump(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="reuse-progress",
        retry_limit=0,
    )
    first_provider = cast(FakeModelProvider, create_model_provider(configuration))
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=first_provider,
            provider_configuration=configuration,
        )
    )
    asyncio.run(first_provider.close())

    events: list[ProgressEvent] = []
    update_provider = cast(FakeModelProvider, create_model_provider(configuration))
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=update_provider,
            provider_configuration=configuration,
            update_only=True,
            progress=events.append,
        )
    )
    asyncio.run(update_provider.close())

    semantic = next(
        event
        for event in events
        if event.phase_id == "semantic_index" and event.phase_percent == 100
    )
    assert semantic.percentage == 18
    assert semantic.phase_weight == 0
    assert semantic.total_units == 1
    assert semantic.processed_units == 1
    assert semantic.reused_units > 0
    assert events[-1].percentage == 100


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


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_stderr_chooses_dynamic_when_stdout_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = _TTYStringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    renderer = CLIProgressRenderer(ProgressMode.AUTO)

    assert stdout.isatty() is False
    assert renderer.rendering_mode == "dynamic"
    renderer.close()


def test_dynamic_panel_shows_overall_phase_activity_and_elapsed() -> None:
    stream = _TTYStringIO()
    console = Console(file=stream, force_terminal=True, width=120)
    now = [0.0]
    renderer = CLIProgressRenderer(
        ProgressMode.AUTO, console=console, clock=lambda: now[0]
    )
    waiting = _event(
        operation_type="repository.index.build",
        phase_id="semantic_analysis",
        phase_label="Semantic analysis",
        phase_percent=35,
        phase_weight=64,
        completed_units=9,
        total_units=26,
        unit_type="files",
        current_item="src/contextforge/application.py",
        last_completed_item="src/contextforge/project_config.py",
        active_items=("src/contextforge/application.py",),
        active_item_count=1,
        activity=ProgressActivity.WAITING,
        metadata={"provider_id": "lmstudio", "model_id": "qwen-test"},
    )
    renderer(waiting)
    initial_percent = waiting.percentage
    now[0] = 10.0
    console.print(renderer._render_dynamic())
    renderer.close()

    rendered = stream.getvalue()
    assert "Overall" in rendered
    assert "Phase" in rendered
    assert "Semantic analysis" in rendered
    assert "waiting for provider" in rendered
    assert "src/contextforge/application.py" in rendered
    assert "src/contextforge/project_config.py" in rendered
    assert "Elapsed: 00:10" in rendered
    assert waiting.percentage == initial_percent


def test_one_stream_has_one_idempotent_live_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"created": 0, "started": 0, "refreshed": 0, "stopped": 0}

    class FakeLive:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            calls["created"] += 1

        def start(self, *, refresh: bool) -> None:
            assert refresh is True
            calls["started"] += 1

        def refresh(self) -> None:
            calls["refreshed"] += 1

        def stop(self) -> None:
            calls["stopped"] += 1

    monkeypatch.setattr(cli_progress_module, "Live", FakeLive)
    stream = _TTYStringIO()
    console = Console(file=stream, force_terminal=True, width=100)
    owner = CLIProgressRenderer(console=console)
    nested = CLIProgressRenderer(console=console)

    owner(_event(operation_type="repository.index.build"))
    nested(
        _event(
            operation_type="repository.index.build",
            phase_id="provider_retry",
            current_attempt=2,
            max_attempts=3,
            sequence=3,
        )
    )
    nested.close()
    owner.close()
    owner.close()

    assert calls == {"created": 1, "started": 1, "refreshed": 1, "stopped": 1}


def test_logging_during_live_reuses_the_same_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = 0

    class FakeLive:
        def __init__(self, **kwargs: object) -> None:
            nonlocal created
            del kwargs
            created += 1

        def start(self, *, refresh: bool) -> None:
            del refresh

        def refresh(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli_progress_module, "Live", FakeLive)
    stream = _TTYStringIO()
    renderer = CLIProgressRenderer(
        console=Console(file=stream, force_terminal=True, width=100)
    )
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("contextforge.progress-test")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        renderer(_event(operation_type="repository.index.build"))
        assert handler.stream is not stream
        logger.warning("safe diagnostic")
        renderer(_event(operation_type="repository.index.build", sequence=3))
    finally:
        logger.removeHandler(handler)
        renderer.close()

    assert created == 1
    assert stream.getvalue().count("safe diagnostic") == 1
    assert handler.stream is stream


@pytest.mark.parametrize("width", [120, 34])
def test_dynamic_layout_keeps_complete_labels_and_wraps_values(width: int) -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, width=width)
    renderer = CLIProgressRenderer(console=console)
    event = _event(
        operation_type="repository.index.build",
        planned_units=26,
        processed_units=11,
        succeeded_units=3,
        failed_units=1,
        fallback_units=1,
        skipped_units=2,
        reused_units=4,
        current_item="very/long/path/config.json",
        last_completed_item="README.md",
        last_failed_item="very/long/path/failed-file.txt",
        current_attempt=2,
        max_attempts=3,
        lifecycle_state="waiting_for_provider",
        analyzer_kind="generic-text-semantic",
        metadata={"provider_id": "openai-compatible", "model_id": "long/model"},
    )

    renderer._event = event
    console.print(renderer._render_dynamic())
    rendered = stream.getvalue()

    for label in (
        "Processed:",
        "Succeeded:",
        "Failed:",
        "Fallback:",
        "Skipped:",
        "Reused:",
        "Current:",
        "Done:",
        "Last failure:",
        "Attempt:",
        "Model:",
    ):
        assert label in rendered
    assert "…" not in rendered
    assert "config.json" in rendered.replace("\n", "")
    assert "failed-file.txt" in rendered.replace("\n", "")


def test_renderer_labels_failed_semantic_work_as_processed() -> None:
    stream = io.StringIO()
    renderer = CLIProgressRenderer(stream)
    renderer(
        _event(
            operation_type="repository.index.build",
            phase_id="semantic_analysis",
            phase_label="Semantic analysis",
            phase_percent=4,
            completed=1,
            total=26,
            completed_units=1,
            total_units=26,
            unit_type="items",
            planned_units=26,
            processed_units=1,
            failed_units=1,
            last_failed_item=".env.example",
            current_item=".gitignore",
            current_attempt=1,
            max_attempts=3,
            lifecycle_state="waiting_for_provider",
            safe_error_code="structured_output_validation_failed",
            safe_error_message="structured response validation failed",
            activity=ProgressActivity.WAITING,
        )
    )

    output = stream.getvalue()
    assert "1/26 processed" in output
    assert "processed=1/26" in output
    assert "succeeded=0" in output
    assert "reason=structured_output_validation_failed" in output
    assert "0/26" not in output


def test_progress_never_suppresses_all_terminal_output() -> None:
    stream = _TTYStringIO()
    renderer = CLIProgressRenderer(ProgressMode.NEVER, stream=stream)
    renderer(_event(operation_type="repository.index.build"))
    renderer.close()

    assert renderer.rendering_mode == "disabled"
    assert stream.getvalue() == ""


@pytest.mark.parametrize("status", [ProgressStatus.FAILED, ProgressStatus.CANCELLED])
def test_dynamic_renderer_cleans_up_unsuccessful_terminal_state(
    status: ProgressStatus,
) -> None:
    stream = _TTYStringIO()
    console = Console(file=stream, force_terminal=True, width=100)
    renderer = CLIProgressRenderer(console=console)
    renderer(_event(operation_type="repository.index.build"))
    renderer(
        _event(
            operation_type="repository.index.build",
            status=status,
            sequence=3,
        )
    )
    renderer.close()

    assert renderer._live is None
    assert status.value in stream.getvalue()


def test_observer_error_does_not_leave_dynamic_renderer_live() -> None:
    stream = _TTYStringIO()
    renderer = CLIProgressRenderer(
        console=Console(file=stream, force_terminal=True, width=100)
    )

    def broken(event: ProgressEvent) -> None:
        renderer(event)
        raise RuntimeError("observer failed")

    reporter = ProgressReporter(
        "observer-cleanup", "repository.index.build", observer=broken
    )
    reporter.report("scan", "Scanning.", percentage=10)
    reporter.complete()

    assert reporter.observer_error_count == 2
    assert renderer._live is None


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


def test_keyboard_interrupt_is_reported_as_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    events: list[ProgressEvent] = []

    def interrupt(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(application_module, "build_structural_index", interrupt)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            build_repository_index(
                tmp_path,
                provider=None,
                provider_configuration=None,
                progress=events.append,
            )
        )

    assert events[-1].status is ProgressStatus.CANCELLED
    assert events[-1].percentage < 100
    assert events[-1].active_item_count == 0
