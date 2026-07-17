import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.intelligence import (
    AnalyzerIdentity,
    IndexBuildState,
    IndexComparisonError,
    IndexedFileState,
    IndexManifest,
    ModelIdentity,
    SchemaVersionMetadata,
    build_index_manifest,
    calculate_generation_id,
    calculate_source_snapshot_digest,
    canonical_json_bytes,
    compare_index_status,
    identify_added_files,
    identify_changed_files,
    identify_deleted_files,
    identify_stale_analysis,
    identify_unchanged_files,
    validate_portable_relative_path,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot, ScanSummary


def _sha(value: bytes | str) -> str:
    encoded = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _analyzer(
    *,
    analyzer_version: str = "1",
    prompt_version: str = "prompt-1",
    provider: str = "local",
    model: str = "model-a",
) -> AnalyzerIdentity:
    return AnalyzerIdentity(
        analyzer_id="repository-analysis",
        analyzer_version=analyzer_version,
        analysis_prompt_version=prompt_version,
        response_schema_version=1,
        model_identity=ModelIdentity(provider_id=provider, model_id=model),
    )


def _file(path: str, content: str, *, language: str | None = "Python") -> ProjectFile:
    encoded = content.encode()
    return ProjectFile(
        path=path,
        size_bytes=len(encoded),
        language=language,
        sha256=_sha(encoded),
        is_text=True,
    )


def _state(
    project_file: ProjectFile,
    *,
    analyzer: AnalyzerIdentity | None = None,
    record_status: str = "complete",
) -> IndexedFileState:
    record = f"facts:{project_file.path}".encode()
    path_key = _sha(project_file.path)
    values: dict[str, object] = {
        "path": project_file.path,
        "source_sha256": project_file.sha256,
        "source_size_bytes": project_file.size_bytes,
        "language": project_file.language,
        "analyzer": analyzer or _analyzer(),
        "record_status": record_status,
    }
    if record_status in {"complete", "unsupported"}:
        values.update(
            record_location=f"files/{path_key}.facts.json",
            record_sha256=_sha(record),
        )
    return IndexedFileState.model_validate(values)


def _manifest(
    files: tuple[ProjectFile, ...],
    *,
    analyzer: AnalyzerIdentity | None = None,
    build_options_digest: str | None = None,
    schema_versions: SchemaVersionMetadata | None = None,
) -> IndexManifest:
    identity = analyzer or _analyzer()
    states = tuple(_state(item, analyzer=identity) for item in files)
    build = IndexBuildState(
        source_snapshot_digest=calculate_source_snapshot_digest(files),
        index_config_digest=_sha("config"),
        build_options_digest=build_options_digest or _sha("options"),
        facts_digest=_sha("facts"),
    )
    return build_index_manifest(
        build=build,
        files=reversed(states),
        structural_analyzers=(identity,),
        schema_versions=schema_versions,
    )


def _snapshot(tmp_path: Path, files: tuple[ProjectFile, ...]) -> ProjectSnapshot:
    return ProjectSnapshot(
        root=tmp_path,
        files=files,
        summary=ScanSummary(
            file_count=len(files),
            ignored_count=0,
            total_size_bytes=sum(item.size_bytes for item in files),
        ),
    )


def test_manifest_serialization_and_generation_id_are_deterministic() -> None:
    files = (_file("z.py", "z"), _file("a.py", "a"))

    first = _manifest(files)
    second = _manifest(tuple(reversed(files)))
    first_json = canonical_json_bytes(first.model_dump(mode="json"))
    second_json = canonical_json_bytes(second.model_dump(mode="json"))

    assert first == second
    assert first_json == second_json
    assert first_json.endswith(b"\n")
    assert b"timestamp" not in first_json
    assert b"root" not in first_json
    assert first.generation_id == calculate_generation_id(first)
    assert tuple(item.path for item in first.files) == ("a.py", "z.py")
    assert first.statistics.file_count == 2
    assert first.statistics.total_source_bytes == 2


def test_manifest_models_are_closed_frozen_and_do_not_accept_credentials() -> None:
    identity = ModelIdentity(provider_id="ollama", model_id="qwen")
    assert identity.model_dump() == {"provider_id": "ollama", "model_id": "qwen"}

    with pytest.raises(ValidationError):
        identity.provider_id = "changed"
    with pytest.raises(ValidationError):
        ModelIdentity(
            provider_id="cloud",
            model_id="model",
            api_key="secret",  # type: ignore[call-arg]
        )

    serialized = _manifest((_file("app.py", "pass"),)).model_dump_json()
    assert "api_key" not in serialized
    assert "bearer" not in serialized
    assert "credential" not in serialized


def test_record_reference_and_manifest_statistics_are_validated() -> None:
    project_file = _file("app.py", "pass")
    complete = _state(project_file)
    manifest = _manifest((project_file,))

    with pytest.raises(ValidationError, match="set together"):
        complete.model_copy(update={"record_sha256": None}, deep=True).__class__(
            **{
                **complete.model_dump(),
                "record_sha256": None,
            }
        )
    with pytest.raises(ValidationError, match="cannot reference"):
        IndexedFileState(**{**complete.model_dump(), "record_status": "failed"})
    with pytest.raises(ValidationError, match="statistics"):
        manifest.__class__(
            **{
                **manifest.model_dump(),
                "statistics": manifest.statistics.model_copy(update={"file_count": 99}),
            }
        )


def test_added_changed_unchanged_and_deleted_detection() -> None:
    original_a = _file("a.py", "a")
    deleted = _file("deleted.py", "gone")
    manifest = _manifest((original_a, deleted))
    changed_a = _file("a.py", "changed")
    added = _file("new.py", "new")
    current = (added, changed_a)

    assert tuple(item.path for item in identify_added_files(manifest, current)) == (
        "new.py",
    )
    assert tuple(item.path for item in identify_changed_files(manifest, current)) == (
        "a.py",
    )
    assert identify_unchanged_files(manifest, current) == ()
    assert tuple(item.path for item in identify_deleted_files(manifest, current)) == (
        "deleted.py",
    )


def test_unchanged_content_is_not_invalidated_by_external_mtime_state() -> None:
    project_file = _file("app.py", "same")
    manifest = _manifest((project_file,))
    reconstructed = project_file.model_copy()

    assert identify_changed_files(manifest, (reconstructed,)) == ()
    assert identify_unchanged_files(manifest, (reconstructed,)) == (reconstructed,)


@pytest.mark.parametrize(
    "expected_analyzer",
    [
        _analyzer(analyzer_version="2"),
        _analyzer(prompt_version="prompt-2"),
        _analyzer(provider="other-provider"),
        _analyzer(model="model-b"),
    ],
)
def test_analyzer_prompt_provider_and_model_changes_invalidate(
    expected_analyzer: AnalyzerIdentity,
) -> None:
    project_file = _file("app.py", "same")
    manifest = _manifest((project_file,))

    stale = identify_stale_analysis(
        manifest,
        (project_file,),
        expected_analyzer=expected_analyzer,
        build_options_digest=_sha("options"),
    )

    assert tuple(item.path for item in stale) == ("app.py",)


def test_source_schema_and_build_option_changes_invalidate() -> None:
    project_file = _file("app.py", "same")
    manifest = _manifest((project_file,))
    changed = _file("app.py", "different")

    source_stale = identify_stale_analysis(
        manifest,
        (changed,),
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("options"),
    )
    option_stale = identify_stale_analysis(
        manifest,
        (project_file,),
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("new-options"),
    )
    schema_stale = identify_stale_analysis(
        manifest,
        (project_file,),
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("options"),
        schema_versions=SchemaVersionMetadata(record_schema_version=2),
    )

    assert source_stale == option_stale == schema_stale == manifest.files


def test_status_for_first_index_and_existing_index_is_canonical(tmp_path: Path) -> None:
    first = _file("a.py", "a")
    second = _file("b.py", "b")
    snapshot = _snapshot(tmp_path, (second, first))

    initial = compare_index_status(
        None,
        snapshot,
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("options"),
        initialized=True,
    )
    existing = compare_index_status(
        _manifest((first, second)),
        snapshot,
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("options"),
        initialized=True,
    )

    assert initial.added_files == ("a.py", "b.py")
    assert initial.active_generation_id is None
    assert existing.unchanged_files == ("a.py", "b.py")
    assert existing.stale_analysis == ()


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "../secret",
        "safe/../secret",
        "/absolute",
        r"\absolute",
        r"C:\absolute",
        "C:/absolute",
        "C:drive-relative",
        r"\\server\share\file",
        "bad\x00path",
    ],
)
def test_portable_path_policy_rejects_every_escape_shape(path: str) -> None:
    with pytest.raises(ValueError, match="portable relative"):
        validate_portable_relative_path(path)


def test_snapshot_digest_rejects_duplicate_or_non_project_files() -> None:
    project_file = _file("app.py", "same")

    with pytest.raises(IndexComparisonError, match="unique"):
        calculate_source_snapshot_digest((project_file, project_file))
    with pytest.raises(IndexComparisonError, match="ProjectFile"):
        calculate_source_snapshot_digest((object(),))  # type: ignore[arg-type]


def test_invalid_build_options_digest_is_rejected() -> None:
    project_file = _file("app.py", "same")

    with pytest.raises(IndexComparisonError, match="SHA-256"):
        identify_stale_analysis(
            _manifest((project_file,)),
            (project_file,),
            expected_analyzer=_analyzer(),
            build_options_digest="invalid",
        )


def test_openai_compatible_base_url_identity_change_invalidates_model_records() -> None:
    project_file = _file("app.py", "pass")
    first = _analyzer().model_copy(
        update={
            "analyzer_version": "1+base." + _sha("http://localhost:1234/v1"),
            "model_identity": ModelIdentity(
                provider_id="openai-compatible",
                model_id="exact/model",
            ),
        }
    )
    changed = first.model_copy(
        update={
            "analyzer_version": "1+base." + _sha("http://localhost:9999/v1"),
        }
    )
    manifest = _manifest((project_file,), analyzer=first)

    assert (
        identify_stale_analysis(
            manifest,
            (project_file,),
            expected_analyzer=changed,
            build_options_digest=_sha("options"),
        )
        == manifest.files
    )
