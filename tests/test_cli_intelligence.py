import json
from pathlib import Path
from typing import Any, cast

import click
import pytest
from typer.testing import CliRunner, Result

import contextforge.cli.context_commands as context_cli
import contextforge.cli.intelligence_commands as index_cli
from contextforge.cli.main import app
from contextforge.intelligence import IndexManifestNotFoundError, load_manifest
from contextforge.models import FakeModelProvider, ProviderConfiguration

runner = CliRunner()
TERMINAL_WIDTH = 140


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _invoke(*arguments: str, input: str | None = None) -> Result:
    return runner.invoke(
        app,
        list(arguments),
        input=input,
        terminal_width=TERMINAL_WIDTH,
    )


def _plain(value: str) -> str:
    return click.unstyle(value)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--help",),
        ("index", "--help"),
        ("index", "build", "--help"),
        ("index", "update", "--help"),
        ("index", "status", "--help"),
        ("index", "clean", "--help"),
        ("context", "suggest", "--help"),
        ("context", "create", "--help"),
        ("context", "review", "--help"),
        ("mcp", "serve", "--help"),
    ],
)
def test_repository_intelligence_help_commands(arguments: tuple[str, ...]) -> None:
    result = _invoke(*arguments)

    assert result.exit_code == 0
    assert "Traceback" not in result.output


def test_index_build_update_reuse_status_and_clean_preserve_config(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    _write(tmp_path, "tests/test_app.py", "from app import run\n")

    built = _invoke("index", "build", str(tmp_path), "--provider", "fake")

    assert built.exit_code == 0, built.output
    assert "Status: complete" in _plain(built.stdout)
    first = load_manifest(tmp_path)
    assert all(
        item.semantic_status
        == ("skipped" if item.path.startswith(".contextforge/") else "complete")
        for item in first.files
    )

    unchanged = _invoke("index", "update", str(tmp_path), "--provider", "fake")
    assert unchanged.exit_code == 0, unchanged.output
    assert "CodeMaps extracted: 0" in _plain(unchanged.stdout)
    assert "Semantic analyses reused:" in _plain(unchanged.stdout)
    assert load_manifest(tmp_path) == first

    _write(tmp_path, "app.py", "def run():\n    return 2\n")
    _write(tmp_path, "new.py", "VALUE = 1\n")
    (tmp_path / "tests/test_app.py").unlink()
    updated = _invoke("index", "update", str(tmp_path), "--provider", "fake")

    assert updated.exit_code == 0, updated.output
    assert "CodeMaps extracted: 2" in _plain(updated.stdout)
    current = load_manifest(tmp_path)
    assert tuple(item.path for item in current.files) == (
        "app.py",
        "new.py",
    )

    status = _invoke("index", "status", str(tmp_path), "--format", "json")
    assert status.exit_code == 0
    assert status.stderr == ""
    payload = json.loads(status.stdout)
    assert payload["indexed_files"] == 2
    assert payload["stale_files"] == [
        "app.py",
        "new.py",
    ]
    assert payload["provider_id"] == "fake"
    assert payload["global_maps"] == {
        "architecture": "current",
        "features": "current",
        "overview": "current",
    }
    config = tmp_path / ".contextforge/config.toml"
    before = config.read_bytes()

    refused = _invoke("index", "clean", str(tmp_path), input="n\n")
    assert refused.exit_code == 1
    assert load_manifest(tmp_path) == current

    cleaned = _invoke("index", "clean", str(tmp_path), "--force")
    assert cleaned.exit_code == 0
    assert config.read_bytes() == before
    with pytest.raises(IndexManifestNotFoundError):
        load_manifest(tmp_path)


def test_index_update_requires_existing_index_and_status_handles_missing(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "pass\n")

    missing = _invoke("index", "update", str(tmp_path), "--provider", "none")
    status = _invoke("index", "status", str(tmp_path), "--format", "json")

    assert missing.exit_code == 1
    assert "index build" in _plain(missing.stderr)
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["active_generation_id"] is None
    assert payload["added_files"] == ["app.py"]


def test_index_cli_requires_explicit_confirmation_to_recover_unknown_lock(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "pass\n")
    assert _invoke("index", "build", str(tmp_path), "--provider", "none").exit_code == 0
    lock = tmp_path / ".contextforge/index/lock.json"
    lock.write_text("{", encoding="utf-8")

    refused = _invoke("index", "update", str(tmp_path), "--provider", "none")
    recovered = _invoke(
        "index",
        "update",
        str(tmp_path),
        "--provider",
        "none",
        "--confirm-unknown-lock",
    )

    assert refused.exit_code == 1
    assert refused.stdout == ""
    assert "explicit confirmation is required" in _plain(refused.stderr)
    assert recovered.exit_code == 0, recovered.output
    assert not lock.exists()


def test_index_force_reanalysis_and_max_files_are_reported(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "A = 1\n")
    _write(tmp_path, "b.py", "B = 1\n")
    assert _invoke("index", "build", str(tmp_path), "--provider", "fake").exit_code == 0

    forced = _invoke(
        "index",
        "update",
        str(tmp_path),
        "--provider",
        "fake",
        "--force-reanalyze",
        "--max-files",
        "1",
    )

    assert forced.exit_code == 0
    assert "Semantic analyses completed: 1" in _plain(forced.stdout)
    manifest = load_manifest(tmp_path)
    assert sum(item.semantic_status == "skipped" for item in manifest.files) == 1


def test_index_provider_failure_preserves_previous_active_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    assert _invoke("index", "build", str(tmp_path), "--provider", "fake").exit_code == 0
    previous = load_manifest(tmp_path)
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="broken",
        retry_limit=0,
    )

    monkeypatch.setattr(
        index_cli,
        "create_model_provider",
        lambda _configuration: FakeModelProvider(configuration, scripts=("not-json",)),
    )
    failed = _invoke(
        "index",
        "update",
        str(tmp_path),
        "--provider",
        "fake",
        "--force-reanalyze",
        "--fail-on-error",
    )

    assert failed.exit_code == 1
    assert failed.stdout == ""
    assert "semantic analysis failed" in _plain(failed.stderr).lower()
    assert load_manifest(tmp_path) == previous


def test_index_cancellation_maps_to_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancelled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(index_cli, "build_repository_index", cancelled)

    result = _invoke("index", "build", str(tmp_path), "--provider", "none")

    assert result.exit_code == 130


def test_progress_never_suppresses_stderr_and_preserves_json_stdout(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "VALUE = 1\n")
    built = _invoke(
        "index",
        "build",
        str(tmp_path),
        "--provider",
        "none",
        "--progress",
        "never",
    )
    suggested = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "Review VALUE",
        "--provider",
        "fake",
        "--format",
        "json",
        "--progress",
        "never",
    )

    assert built.exit_code == suggested.exit_code == 0
    assert built.stderr == ""
    assert suggested.stderr == ""
    assert json.loads(suggested.stdout)["mode"] == "hybrid"


@pytest.mark.parametrize("mode", ["fresh", "hybrid"])
def test_context_suggest_table_and_json_are_read_only(
    tmp_path: Path, mode: str
) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    before = (tmp_path / "app.py").read_bytes()

    table = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "Review run",
        "--discovery",
        mode,
        "--provider",
        "fake",
        "--explain",
    )
    structured = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "Review run",
        "--discovery",
        mode,
        "--provider",
        "fake",
        "--format",
        "json",
    )

    assert table.exit_code == structured.exit_code == 0
    assert "Discovery mode:" in _plain(table.stdout)
    assert "reason:" in _plain(table.stdout)
    assert "Suggesting context:" in _plain(structured.stderr)
    assert "\x1b" not in structured.stderr
    payload = json.loads(structured.stdout)
    assert payload["mode"] == mode
    assert payload["selected"][0]["path"] == "app.py"
    assert payload["budget_usage"]["context_bytes"] > 0
    assert (tmp_path / "app.py").read_bytes() == before
    assert not (tmp_path / ".contextforge").exists()


def test_context_suggest_indexed_missing_then_indexed_success(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "pass\n")
    missing = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "Review",
        "--discovery",
        "indexed",
        "--provider",
        "fake",
    )
    assert missing.exit_code == 1
    assert "active index" in _plain(missing.stderr)

    assert _invoke("index", "build", str(tmp_path), "--provider", "fake").exit_code == 0
    indexed = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "Review",
        "--discovery",
        "indexed",
        "--provider",
        "fake",
        "--format",
        "json",
    )
    assert indexed.exit_code == 0, indexed.output
    assert json.loads(indexed.stdout)["mode"] == "indexed"


def test_suggest_invalid_mode_overwrite_refusal_and_force(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "pass\n")
    invalid = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "x",
        "--discovery",
        "unknown",
    )
    assert invalid.exit_code == 2

    output = tmp_path / "suggestion.json"
    first = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "x",
        "--provider",
        "fake",
        "--format",
        "json",
        "--output",
        str(output),
    )
    refused = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "x",
        "--provider",
        "fake",
        "--format",
        "json",
        "--output",
        str(output),
    )
    forced = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "x",
        "--provider",
        "fake",
        "--format",
        "json",
        "--output",
        str(output),
        "--force",
    )
    assert first.exit_code == forced.exit_code == 0
    assert refused.exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "hybrid"


def test_context_create_automatic_json_prompt_and_portable_review(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    handoff_path = tmp_path / "handoff.json"
    prompt_path = tmp_path / "prompt.md"

    created = _invoke(
        "context",
        "create",
        str(tmp_path),
        "--task",
        "Implement behavior",
        "--discovery",
        "fresh",
        "--provider",
        "fake",
        "--refine-task",
        "--git-diff",
        "working",
        "--format",
        "json",
        "--output",
        str(handoff_path),
        "--prompt-output",
        str(prompt_path),
    )

    assert created.exit_code == 0, created.output
    assert "Creating automatic context: 0%" in _plain(created.stderr)
    assert "Creating automatic context: 100%" in _plain(created.stderr)
    assert "\x1b" not in created.stderr
    payload = cast(dict[str, Any], json.loads(handoff_path.read_text(encoding="utf-8")))
    assert payload["original_task"] == "Implement behavior"
    assert payload["review"]["discovery"]["mode"] == "fresh"
    assert payload["context_package"]["schema_version"] == 1
    assert "## Original task" in prompt_path.read_text(encoding="utf-8")

    (tmp_path / "app.py").unlink()
    reviewed = _invoke("context", "review", str(handoff_path))
    assert reviewed.exit_code == 0
    for expected in (
        "Original task: Implement behavior",
        "Selected items:",
        "reason:",
        "pinned:",
        "Budget:",
        "Git context:",
        "Package schema: 1",
        "Model provenance:",
    ):
        assert expected in _plain(reviewed.stdout)


def test_context_create_manual_regression_and_automatic_option_validation(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "pass\n")
    manual = _invoke(
        "context",
        "create",
        str(tmp_path),
        "--include",
        "app.py",
        "--format",
        "json",
    )
    assert manual.exit_code == 0
    assert json.loads(manual.stdout)["files"][0]["path"] == "app.py"

    missing_task = _invoke(
        "context",
        "create",
        str(tmp_path),
        "--discovery",
        "fresh",
        "--provider",
        "fake",
    )
    manual_only = _invoke(
        "context",
        "create",
        str(tmp_path),
        "--task",
        "x",
        "--discovery",
        "fresh",
        "--provider",
        "fake",
        "--glob",
        "*.py",
    )
    assert missing_task.exit_code == manual_only.exit_code == 2


def test_context_review_malformed_missing_and_create_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "bad.json"
    malformed.write_text("{", encoding="utf-8")

    assert _invoke("context", "review", str(tmp_path / "missing.json")).exit_code == 2
    invalid = _invoke("context", "review", str(malformed))
    assert invalid.exit_code == 1
    assert "malformed or invalid" in _plain(invalid.stderr)

    async def cancelled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    _write(tmp_path, "app.py", "pass\n")
    monkeypatch.setattr(context_cli, "create_automatic_handoff", cancelled)
    result = _invoke(
        "context",
        "create",
        str(tmp_path),
        "--task",
        "x",
        "--discovery",
        "fresh",
        "--provider",
        "fake",
    )
    assert result.exit_code == 130


def test_config_does_not_persist_credentials_or_corrupt_json_stdout(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "pass\n")
    config = tmp_path / "models.toml"
    config.write_text(
        """config_version = 1
[models]
provider = "fake"
endpoint = "fake://offline"
model = "fixture"
timeout_seconds = 2.0
max_response_bytes = 1000000
concurrency_limit = 1
retry_limit = 0
local_only = true
external_data_policy = "deny"
store_raw_prompts = false
store_raw_responses = false
credential_env = "CONTEXTFORGE_TEST_TOKEN"
[retention]
runs = 1
index_generations = 2
""",
        encoding="utf-8",
    )
    result = _invoke(
        "context",
        "suggest",
        str(tmp_path),
        "--task",
        "x",
        "--config",
        str(config),
        "--format",
        "json",
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "provider" in _plain(result.stderr)
    assert "secret" not in result.output.lower()
