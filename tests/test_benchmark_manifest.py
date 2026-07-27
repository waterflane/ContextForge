import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from contextforge.benchmarks import (
    BenchmarkManifest,
    BenchmarkMode,
    load_benchmark_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "discovery_benchmark_minimal.json"


def _manifest() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def _validate(payload: dict[str, Any]) -> BenchmarkManifest:
    return BenchmarkManifest.model_validate_json(json.dumps(payload))


def test_minimal_manifest_fixture_is_valid_and_deterministic() -> None:
    manifest = load_benchmark_manifest(FIXTURE)

    assert manifest.schema_version == 1
    assert manifest.suite_name == "minimal-discovery"
    assert manifest.tasks[0].modes == (BenchmarkMode.FRESH,)
    assert manifest.tasks[0].max_provider_http_calls is None
    assert manifest == BenchmarkManifest.model_validate_json(FIXTURE.read_bytes())


def test_manifest_exposes_a_versioned_machine_readable_schema() -> None:
    schema = BenchmarkManifest.model_json_schema()

    assert schema["properties"]["schema_version"]["const"] == 1
    assert "schema_version" in schema["required"]
    assert schema["properties"]["tasks"]["minItems"] == 1


@pytest.mark.parametrize(
    ("field", "value", "location"),
    [
        ("repository_path", "/tmp/repository", ("tasks", 0, "repository_path")),
        ("include_paths", ["src\\app.py"], ("tasks", 0, "include_paths", 0)),
        ("forbidden_files", ["src/../app.py"], ("tasks", 0, "forbidden_files", 0)),
        (
            "include_paths",
            [".contextforge/index/active.json"],
            ("tasks", 0, "include_paths"),
        ),
    ],
)
def test_invalid_paths_report_exact_manifest_locations(
    field: str, value: object, location: tuple[object, ...]
) -> None:
    payload = _manifest()
    task = payload["tasks"][0]
    task[field] = value

    with pytest.raises(ValidationError) as error:
        _validate(payload)

    assert error.value.errors()[0]["loc"] == location


def test_contextforge_state_requires_explicit_benchmark_configuration() -> None:
    payload = _manifest()
    task = payload["tasks"][0]
    task["allow_contextforge_state"] = True
    task["include_paths"] = [".contextforge/index/active.json"]

    manifest = _validate(payload)

    assert manifest.tasks[0].include_paths == (".contextforge/index/active.json",)


def test_manifest_supports_required_groups_warnings_and_per_mode_overrides() -> None:
    payload = _manifest()
    task = payload["tasks"][0]
    task["modes"] = ["fresh", "indexed", "hybrid"]
    task["required_files_any"] = [["README.md", "docs/index.md"]]
    task["forbidden_files"] = ["secrets.txt"]
    task["allowed_warnings"] = ["documentation-not-selected"]
    task["required_warnings"] = ["stale-index-coverage"]
    task["mode_overrides"] = {
        "indexed": {
            "repeat_count": 2,
            "max_files_read": 0,
            "max_model_generations": 1,
            "max_provider_http_calls": 3,
        }
    }

    manifest = _validate(payload)
    override = manifest.tasks[0].mode_overrides.indexed

    assert override is not None
    assert override.repeat_count == 2
    assert override.max_files_read == 0
    assert override.max_provider_http_calls == 3


def test_nested_override_errors_keep_their_exact_location() -> None:
    payload = _manifest()
    task = payload["tasks"][0]
    task["mode_overrides"] = {"fresh": {"required_files_any": [["../escape.py"]]}}

    with pytest.raises(ValidationError) as error:
        _validate(payload)

    assert error.value.errors()[0]["loc"] == (
        "tasks",
        0,
        "mode_overrides",
        "fresh",
        "required_files_any",
        0,
        0,
    )


def test_manifest_rejects_unknown_fields_before_task_execution() -> None:
    payload = _manifest()
    payload["tasks"][0]["provider"] = "fake"

    with pytest.raises(ValidationError) as error:
        _validate(payload)

    assert error.value.errors()[0]["loc"] == ("tasks", 0, "provider")
