import asyncio
import json
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from contextforge.application import (
    build_discovery_request,
    build_repository_index,
    suggest_repository_context,
)
from contextforge.cli.main import app
from contextforge.diagnostics import (
    DiagnosticStoreError,
    OperationDiagnosticSummary,
    load_last_summary,
    load_summary,
    persist_summary,
)
from contextforge.discovery import DiscoveryProtocolError
from contextforge.logging import (
    LogFormat,
    LoggingConfiguration,
    LogLevel,
    clear_recent_records,
    configure_logging,
    emit,
    recent_records,
    redact_mapping,
    sanitize_url,
)
from contextforge.models import (
    ContextWindowExceededError,
    FakeModelProvider,
    ModelRequest,
    ProviderConfiguration,
    estimate_request_context,
)
from contextforge.project_config import (
    configuration_resolution,
    load_project_configuration,
    resolve_logging_configuration,
    resolve_provider_configuration,
)
from contextforge.repositories import scan_repository


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    summary: str


def _request(*, size: int = 10) -> ModelRequest:
    return ModelRequest(
        operation_id="logging-regression-request",
        purpose="repository-discovery",
        system_instructions="Use verified facts.",
        analysis_task="Select bounded context.",
        trusted_code_map_facts={"records": [{"path": "src/app.py"}]},
        untrusted_sources=(),
        response_model=_Response,
        max_output_tokens=256,
        metadata={"padding_bytes": str(size)},
    )


def _valid_response() -> str:
    return json.dumps({"schema_version": 1, "summary": "ok"})


def test_default_logging_is_concise_and_debug_enables_budget_events() -> None:
    stream = StringIO()
    clear_recent_records()
    configure_logging(LoggingConfiguration(level=LogLevel.WARNING), stream=stream)
    emit("budget", "budget.calculated", "safe", level=LogLevel.DEBUG, data={"x": 1})
    assert stream.getvalue() == ""
    assert recent_records(component="budget")[0].event == "budget.calculated"

    configure_logging(LoggingConfiguration(level=LogLevel.DEBUG), stream=stream)
    emit("budget", "budget.calculated", "safe", level=LogLevel.DEBUG, data={"x": 2})
    assert "budget.calculated" in stream.getvalue()


def test_json_and_pretty_logs_use_only_the_configured_stderr_stream() -> None:
    pretty = StringIO()
    configure_logging(
        LoggingConfiguration(level=LogLevel.INFO, format=LogFormat.PRETTY),
        stream=pretty,
    )
    emit("provider", "provider.request.started", "started", data={"attempt": 1})
    assert "provider.request.started" in pretty.getvalue()
    assert "\x1b" not in pretty.getvalue()

    json_stream = StringIO()
    configure_logging(
        LoggingConfiguration(level=LogLevel.INFO, format=LogFormat.JSON),
        stream=json_stream,
    )
    emit("provider", "provider.request.completed", "done", status="accepted")
    lines = json_stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "provider.request.completed"


def test_component_filter_and_component_level_override() -> None:
    stream = StringIO()
    configure_logging(
        LoggingConfiguration(
            level=LogLevel.WARNING,
            components={"budget": LogLevel.TRACE},
            component_filter=frozenset({"budget"}),
        ),
        stream=stream,
    )
    emit("provider", "provider.request.started", "hidden", level=LogLevel.ERROR)
    emit("budget", "budget.calculated", "shown", level=LogLevel.TRACE)
    assert "shown" in stream.getvalue()
    assert "hidden" not in stream.getvalue()


def test_structured_redaction_covers_headers_tokens_urls_and_nested_fields() -> None:
    secret = "super-secret-value"
    value = redact_mapping(
        {
            "headers": {"Authorization": f"Bearer {secret}", "Cookie": secret},
            "api_key": secret,
            "endpoint": f"https://user:{secret}@example.test/v1?token={secret}&x=1",
            "safe": f"Bearer {secret}",
            "complete_prompt": "must-not-appear",
            "raw_response": "must-not-appear-either",
        }
    )
    encoded = json.dumps(value)
    assert secret not in encoded
    assert "must-not-appear" not in encoded
    assert value["api_key"] == "[REDACTED]"
    assert "user:" not in cast(str, value["endpoint"])
    assert sanitize_url("https://u:p@example.test/v1?api_key=x").endswith(
        "api_key=%5BREDACTED%5D"
    )


def test_rotation_retains_configured_number_of_files(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "contextforge.log"
    configure_logging(
        LoggingConfiguration(
            level=LogLevel.INFO,
            file_enabled=True,
            file=log_file,
            rotation_bytes=1_024,
            retained_files=2,
        ),
        stream=StringIO(),
    )
    for index in range(80):
        emit(
            "storage",
            "storage.rotation_test",
            "x" * 80,
            data={"index": index},
        )
    assert log_file.is_file()
    assert len(tuple(log_file.parent.glob("contextforge.log.*"))) == 2
    for line in log_file.read_text(encoding="utf-8").splitlines():
        assert isinstance(json.loads(line), dict)


def test_logging_failure_does_not_corrupt_an_index_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked", encoding="utf-8")
    configuration = LoggingConfiguration(
        level=LogLevel.INFO,
        file_enabled=True,
        file=blocker / "contextforge.log",
        repository_root=tmp_path,
    )
    configure_logging(configuration, stream=StringIO())
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    assert report.manifest.statistics.file_count == 2
    assert "logging failed; operation continues" in capsys.readouterr().err


def test_configuration_precedence_and_98304_are_preserved(tmp_path: Path) -> None:
    state = tmp_path / ".contextforge"
    state.mkdir()
    (state / "config.toml").write_text(
        "config_version = 1\n[models]\ncontext_window = 16384\n",
        encoding="utf-8",
    )
    (state / "config.local.toml").write_text(
        "[models]\ncontext_window = 32768\n", encoding="utf-8"
    )
    project = load_project_configuration(
        tmp_path,
        environment={"CONTEXTFORGE_MODEL_CONTEXT_WINDOW": "98304"},
    )
    assert project.models.context_window == 98_304
    resolution = configuration_resolution(project)
    candidates = resolution["candidates"]["models.context_window"]
    assert candidates["config.toml"] == 16_384
    assert candidates["config.local.toml"] == 32_768
    assert candidates["environment"] == 98_304
    provider = resolve_provider_configuration(project)
    assert provider is not None
    assert provider.context_window == 98_304
    assert provider.context_window_source == "environment"


def test_config_toml_mismatch_reports_provider_98304_and_effective_16384(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".contextforge"
    state.mkdir()
    (state / "config.toml").write_text(
        "config_version = 1\n[models]\ncontext_window = 16384\n",
        encoding="utf-8",
    )
    project = load_project_configuration(tmp_path, environment={})
    provider = resolve_provider_configuration(project)
    assert provider is not None
    provider = provider.model_copy(
        update={
            "provider_reported_context_window": 98_304,
            "model_metadata_context_window": 98_304,
        }
    )
    clear_recent_records()
    configure_logging(LoggingConfiguration(level=LogLevel.INFO), stream=StringIO())
    fake = FakeModelProvider(
        provider.model_copy(
            update={"provider_id": "fake", "endpoint": "fake://offline"}
        ),
        scripts=[_valid_response()],
    )
    asyncio.run(fake.complete_structured(_request()))
    event = next(
        item for item in recent_records() if item.event == "config.value_resolved"
    )
    assert event.data["provider_reported_context_window"] == 98_304
    assert event.data["effective_context_window"] == 16_384
    assert event.data["effective_context_window_source"] == "config.toml"


def test_budget_components_reproduce_total_and_local_rejection_skips_provider() -> None:
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="budget-test",
        context_window=1_024,
        context_safety_margin=64,
        retry_limit=0,
    )
    request = _request()
    budget = estimate_request_context(request, configuration)
    assert budget.estimated_input_tokens == (
        budget.estimated_system_tokens
        + budget.estimated_user_tokens
        + budget.estimated_source_tokens
        + budget.estimated_index_tokens
    )
    assert budget.estimated_total_tokens == (
        budget.estimated_input_tokens
        + budget.schema_overhead_tokens
        + budget.output_token_budget
        + budget.protocol_overhead_tokens
        + budget.safety_margin_tokens
    )

    called = 0

    def responder(request: ModelRequest, index: int) -> str:
        nonlocal called
        del request, index
        called += 1
        return _valid_response()

    oversized = ModelRequest(
        operation_id="oversized-budget-request",
        purpose="repository-discovery",
        system_instructions="x" * 4_000,
        analysis_task="y" * 4_000,
        trusted_code_map_facts={"records": []},
        untrusted_sources=(),
        response_model=_Response,
        max_output_tokens=512,
    )
    clear_recent_records()
    provider = FakeModelProvider(configuration, responder=responder)
    with pytest.raises(ContextWindowExceededError):
        asyncio.run(provider.complete_structured(oversized))
    assert called == 0
    rejected = next(
        item for item in recent_records() if item.event == "budget.rejected"
    )
    assert rejected.data["estimated_total_tokens"] == (
        rejected.data["estimated_input_tokens"]
        + rejected.data["estimated_schema_tokens"]
        + rejected.data["requested_output_tokens"]
        + rejected.data["protocol_overhead_tokens"]
        + rejected.data["safety_margin_tokens"]
    )
    assert rejected.data["request_dispatched"] is False
    assert rejected.data["error_code"] == (
        "model_request_exceeds_configured_context_window"
    )


def test_provider_request_stages_are_distinct_and_safe() -> None:
    clear_recent_records()
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="stage-test",
        context_window=16_384,
        retry_limit=0,
    )
    provider = FakeModelProvider(configuration, scripts=[_valid_response()])
    asyncio.run(provider.complete_structured(_request()))
    events = {item.event for item in recent_records()}
    assert {
        "budget.calculated",
        "provider.request.started",
        "provider.response.received",
        "response.parsed",
        "provider.request.completed",
    } <= events
    encoded = json.dumps([item.to_dict() for item in recent_records()])
    assert "Select bounded context" not in encoded
    assert _valid_response() not in encoded


def test_context_suggestion_reports_candidate_counts_and_failure_stage(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    request = build_discovery_request(task="Select app", mode="fresh")
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="suggest-stage-test",
        context_window=1_024,
        context_safety_margin=64,
        retry_limit=0,
    )

    def responder(model_request: ModelRequest, index: int) -> str:
        del model_request, index
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "schema_version": 1,
                        "action_id": "finalize",
                        "kind": "finalize",
                        "arguments": {
                            "summary": "fixture",
                            "confidence": 0.5,
                        },
                    }
                ],
            }
        )

    provider = FakeModelProvider(configuration, responder=responder)
    clear_recent_records()
    with pytest.raises(DiscoveryProtocolError, match="context window"):
        asyncio.run(suggest_repository_context(snapshot, provider, request))
    discovered = next(
        item
        for item in recent_records()
        if item.event == "context_suggestion.candidates_discovered"
    )
    assert discovered.data["structural_candidates"] == 1
    assert discovered.data["candidate_count_after_deduplication"] == 1
    failed = next(
        item for item in recent_records() if item.event == "context_suggestion.failed"
    )
    assert failed.data["failing_stage"] == "budget_validation"
    assert failed.data["provider_request_dispatched"] is False


def test_logging_cli_flags_preserve_json_stdout_and_precedence(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--log-level",
            "debug",
            "--log-format",
            "json",
            "scan",
            str(tmp_path),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.stdout), dict)
    assert all(
        isinstance(json.loads(line), dict) for line in result.stderr.splitlines()
    )

    project = load_project_configuration(tmp_path, environment={})
    assert resolve_logging_configuration(project, tmp_path, verbosity=1).level == (
        LogLevel.INFO
    )
    assert resolve_logging_configuration(project, tmp_path, verbosity=2).level == (
        LogLevel.TRACE
    )
    assert (
        resolve_logging_configuration(
            project, tmp_path, level="error", verbosity=2
        ).level
        == LogLevel.ERROR
    )


def test_diagnostic_summary_round_trip_and_cli_last(tmp_path: Path) -> None:
    summary = OperationDiagnosticSummary(
        operation_id="operation-1",
        command="context suggest",
        operation_type="repository.context.suggest",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        duration_ms=1_000,
        outcome="failed",
        generation_id=None,
        provider_models=({"provider": "fake", "model": "fixture"},),
        context_windows=({"effective_context_window": 98_304},),
        budget_breakdowns=({"estimated_total_tokens": 100_000},),
        request_count=0,
        estimated_token_total=100_000,
        actual_input_tokens=0,
        actual_output_tokens=0,
        retry_count=0,
        failed_phases=("budget_calculation",),
        fallback_phases=(),
        final_error_code="context_window_exceeded",
        error_chain=None,
        remediation_hints=("increase context_window",),
    )
    assert persist_summary(tmp_path, summary)
    assert load_last_summary(tmp_path)["final_error_code"] == "context_window_exceeded"
    result = CliRunner().invoke(
        app,
        ["diagnostics", "last", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["operation_id"] == "operation-1"

    shown = CliRunner().invoke(
        app,
        ["diagnostics", "show", str(tmp_path), "operation-1"],
    )
    assert shown.exit_code == 0
    assert "context_window_exceeded" in shown.stdout
    assert load_summary(tmp_path, "operation-1")["duration_ms"] == 1_000


def test_diagnostics_last_failed_and_missing_summary_errors(tmp_path: Path) -> None:
    missing = CliRunner().invoke(
        app, ["diagnostics", "last", str(tmp_path), "--failed"]
    )
    assert missing.exit_code == 1
    assert "no operation diagnostic summary" in missing.stderr
    with pytest.raises(DiagnosticStoreError, match="operation ID"):
        load_summary(tmp_path, "../escape")


def test_diagnostics_config_explains_sources_without_credentials(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".contextforge"
    state.mkdir()
    (state / "config.toml").write_text(
        "config_version = 1\n[models]\ncontext_window = 98304\n"
        "credential_env = 'PRIVATE_KEY_NAME'\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["diagnostics", "config", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    value: dict[str, Any] = json.loads(result.stdout)
    assert value["context_window"]["effective_context_window"] == 98_304
    assert value["context_window"]["effective_context_window_source"] == "config.toml"
    assert "PRIVATE_KEY_NAME" not in result.stdout
    assert value["credential_values_exposed"] is False


def test_diagnostics_provider_is_read_only_and_sanitized(tmp_path: Path) -> None:
    state = tmp_path / ".contextforge"
    state.mkdir()
    (state / "config.toml").write_text(
        "config_version = 1\n[models]\nprovider = 'ollama'\n"
        "endpoint = 'http://127.0.0.1:11434/api/chat'\n"
        "context_window = 98304\ncredential_env = 'OLLAMA_KEY'\n",
        encoding="utf-8",
    )
    before = tuple(state.iterdir())
    result = CliRunner().invoke(
        app,
        ["diagnostics", "provider", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value["enabled"] is True
    assert value["probe_performed"] is False
    assert value["effective_context_window"] == 98_304
    assert value["credential_reference_configured"] is True
    assert tuple(state.iterdir()) == before


def test_logging_environment_precedence_and_file_disable(tmp_path: Path) -> None:
    state = tmp_path / ".contextforge"
    state.mkdir()
    (state / "config.toml").write_text(
        "config_version = 1\n[logging]\nlevel = 'info'\n"
        "format = 'pretty'\nfile_enabled = true\nfile = 'shared.log'\n",
        encoding="utf-8",
    )
    project = load_project_configuration(
        tmp_path,
        environment={
            "CONTEXTFORGE_LOG_LEVEL": "debug",
            "CONTEXTFORGE_LOG_FORMAT": "json",
            "CONTEXTFORGE_LOG_FILE": "environment.log",
            "CONTEXTFORGE_LOG_COMPONENTS": "provider,budget",
        },
    )
    resolution = configuration_resolution(project)
    assert project.logging.level == "debug"
    assert project.logging.format == "json"
    assert resolution["sources"]["logging.level"] == "environment"
    configuration = resolve_logging_configuration(
        project,
        tmp_path,
        component_filter=("provider", "budget"),
        no_log_file=True,
        no_color=True,
    )
    assert configuration.file_enabled is False
    assert configuration.component_filter == frozenset({"provider", "budget"})
    assert configuration.no_color is True
