import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

import contextforge.intelligence.store as store_module
from contextforge.intelligence import (
    DEFAULT_CONFIG,
    AnalyzerIdentity,
    IndexBuildState,
    IndexedFileState,
    IndexLockActiveError,
    IndexLockOwnershipError,
    IndexLockRecoveryRequiredError,
    IndexManifest,
    IndexManifestNotFoundError,
    IndexManifestReadError,
    IndexPathError,
    IndexPublicationError,
    IndexRecordWriteError,
    ModelIdentity,
    UnsupportedIndexSchemaError,
    acquire_index_lock,
    begin_index_build,
    build_index_manifest,
    calculate_generation_id,
    calculate_source_snapshot_digest,
    clean_generated_index,
    cleanup_stale_temporary_files,
    initialize_index,
    inspect_index_status,
    load_index_record,
    load_manifest,
    write_index_record,
    write_manifest,
)
from contextforge.repositories import ProjectFile, scan_repository


def _sha(value: bytes | str) -> str:
    encoded = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


class _WindowsReplaceError(PermissionError):
    winerror: int

    def __init__(self, winerror: int, message: str) -> None:
        super().__init__(message)
        self.winerror = winerror


def test_windows_directory_publication_retries_only_bounded_sharing_errors() -> None:
    errors = [
        _WindowsReplaceError(5, "access denied"),
        _WindowsReplaceError(32, "sharing violation"),
    ]
    attempts: list[tuple[Path, Path]] = []
    delays: list[float] = []

    def replace(source: Path, destination: Path) -> None:
        attempts.append((source, destination))
        if errors:
            raise errors.pop(0)

    store_module._replace_directory_for_publication(
        Path("stage"),
        Path("generation"),
        replace=replace,
        sleeper=delays.append,
        retry_delays=(0.01, 0.05),
        platform="nt",
    )

    assert len(attempts) == 3
    assert delays == [0.01, 0.05]


@pytest.mark.parametrize(
    ("platform", "winerror"),
    [("nt", 2), ("posix", 5)],
)
def test_directory_publication_does_not_retry_unrelated_errors(
    platform: str, winerror: int
) -> None:
    error = _WindowsReplaceError(winerror, "not retryable")
    attempts = 0
    delays: list[float] = []

    def replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        del source, destination
        attempts += 1
        raise error

    with pytest.raises(PermissionError) as captured:
        store_module._replace_directory_for_publication(
            Path("stage"),
            Path("generation"),
            replace=replace,
            sleeper=delays.append,
            platform=platform,
        )

    assert captured.value is error
    assert attempts == 1
    assert delays == []


def test_exhausted_directory_publication_preserves_first_error() -> None:
    first = _WindowsReplaceError(5, "first access denial")
    errors = [
        first,
        _WindowsReplaceError(32, "sharing violation"),
        _WindowsReplaceError(33, "lock violation"),
    ]
    delays: list[float] = []

    def replace(source: Path, destination: Path) -> None:
        del source, destination
        raise errors.pop(0)

    with pytest.raises(PermissionError) as captured:
        store_module._replace_directory_for_publication(
            Path("stage"),
            Path("generation"),
            replace=replace,
            sleeper=delays.append,
            retry_delays=(0.01, 0.05),
            platform="nt",
        )

    assert captured.value is first
    assert delays == [0.01, 0.05]


def _analyzer() -> AnalyzerIdentity:
    return AnalyzerIdentity(
        analyzer_id="python-ast",
        analyzer_version="1",
        analysis_prompt_version="none",
        response_schema_version=1,
        model_identity=ModelIdentity(provider_id="local", model_id="deterministic"),
    )


def _file(path: str, content: str) -> ProjectFile:
    encoded = content.encode()
    return ProjectFile(
        path=path,
        size_bytes=len(encoded),
        language="Python",
        sha256=_sha(encoded),
        is_text=True,
    )


def _record_content(project_file: ProjectFile) -> bytes:
    return json.dumps(
        {"path": project_file.path, "sha256": project_file.sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _record_location(project_file: ProjectFile) -> str:
    return f"files/{_sha(project_file.path)}.facts.json"


def _manifest(
    files: tuple[ProjectFile, ...], *, previous: str | None = None
) -> IndexManifest:
    analyzer = _analyzer()
    states = tuple(
        IndexedFileState(
            path=item.path,
            source_sha256=item.sha256,
            source_size_bytes=item.size_bytes,
            language=item.language,
            analyzer=analyzer,
            record_location=_record_location(item),
            record_sha256=_sha(_record_content(item)),
            record_status="complete",
        )
        for item in files
    )
    build = IndexBuildState(
        source_snapshot_digest=calculate_source_snapshot_digest(files),
        index_config_digest=_sha("config"),
        build_options_digest=_sha("options"),
        facts_digest=_sha("facts"),
        previous_generation_id=previous,
    )
    return build_index_manifest(
        build=build,
        files=states,
        structural_analyzers=(analyzer,),
    )


def _stage_records(
    lock: store_module.IndexWriteLock, files: tuple[ProjectFile, ...]
) -> None:
    for item in files:
        digest = write_index_record(
            lock,
            _record_location(item),
            _record_content(item),
        )
        assert digest == _sha(_record_content(item))


def _publish(
    root: Path, run_id: str, files: tuple[ProjectFile, ...]
) -> tuple[IndexManifest, Path]:
    manifest = _manifest(files)
    with acquire_index_lock(root, run_id) as lock:
        _stage_records(lock, files)
        generation = write_manifest(lock, manifest)
    return manifest, generation


def test_first_initialization_creates_separated_layout_and_preserves_config(
    tmp_path: Path,
) -> None:
    layout = initialize_index(tmp_path)

    assert layout.config.read_text(encoding="utf-8") == DEFAULT_CONFIG
    assert layout.staging.is_dir()
    assert layout.generations.is_dir()
    assert layout.contexts.is_dir()
    assert layout.runs.is_dir()
    assert not layout.active_manifest.exists()

    layout.config.write_text("config_version = 1\n# user edit\n", encoding="utf-8")
    second = initialize_index(tmp_path)

    assert second == layout
    assert "# user edit" in layout.config.read_text(encoding="utf-8")
    with pytest.raises(IndexManifestNotFoundError):
        load_manifest(tmp_path)


def test_existing_valid_index_round_trips_through_atomic_pointer(
    tmp_path: Path,
) -> None:
    files = (_file("src/app.py", "pass"),)
    manifest, generation = _publish(tmp_path, "build-1", files)

    loaded = load_manifest(tmp_path)
    pointer = json.loads(
        (tmp_path / ".contextforge/index/manifest.json").read_text(encoding="utf-8")
    )

    assert loaded == manifest
    assert generation.name == manifest.generation_id
    assert pointer["generation_id"] == manifest.generation_id
    assert pointer["generation_manifest"] == (
        f"generations/{manifest.generation_id}/manifest.json"
    )
    assert not (tmp_path / ".contextforge/index/staging/build-1").exists()
    assert load_index_record(tmp_path, loaded.files[0], manifest=loaded) == (
        _record_content(files[0])
    )


def test_publication_prunes_unreferenced_interrupted_records(
    tmp_path: Path,
) -> None:
    stale = _file("deleted.py", "secret from interrupted build")
    current = _file("current.py", "current")
    manifest = _manifest((current,))

    with acquire_index_lock(tmp_path, "resumed") as lock:
        write_index_record(lock, _record_location(stale), _record_content(stale))
        _stage_records(lock, (current,))
        stage = begin_index_build(lock)
        leftover = stage / "files" / "crash.contextforge-tmp"
        leftover.write_bytes(b"partial secret")
        generation = write_manifest(lock, manifest)

    assert not generation.joinpath(*_record_location(stale).split("/")).exists()
    assert not (generation / "files" / leftover.name).exists()
    assert generation.joinpath(*_record_location(current).split("/")).is_file()


def test_publication_validates_interpretations_even_for_forged_partial_state(
    tmp_path: Path,
) -> None:
    project_file = _file("app.py", "pass")
    interpretation_location = f"files/{_sha('app.py')}.interpretation.json"
    forged = IndexedFileState.model_construct(
        path=project_file.path,
        source_sha256=project_file.sha256,
        source_size_bytes=project_file.size_bytes,
        language=project_file.language,
        analyzer=_analyzer(),
        record_location=None,
        record_sha256=None,
        record_status="failed",
        interpretation_record_location=interpretation_location,
        interpretation_record_sha256=_sha("missing interpretation"),
        semantic_status="complete",
    )
    build = IndexBuildState(
        source_snapshot_digest=calculate_source_snapshot_digest((project_file,)),
        index_config_digest=_sha("config"),
        build_options_digest=_sha("options"),
        facts_digest=_sha("facts"),
    )
    valid_failed = IndexedFileState(
        path=project_file.path,
        source_sha256=project_file.sha256,
        source_size_bytes=project_file.size_bytes,
        language=project_file.language,
        analyzer=_analyzer(),
        record_status="failed",
        semantic_status="failed",
    )
    base = build_index_manifest(build=build, files=(valid_failed,))
    draft = base.model_copy(update={"generation_id": "0" * 64, "files": (forged,)})
    manifest = draft.model_copy(
        update={"generation_id": calculate_generation_id(draft)}
    )

    with (
        acquire_index_lock(tmp_path, "forged") as lock,
        pytest.raises(IndexPathError),
    ):
        write_manifest(lock, manifest)


def test_record_reader_rejects_unpinned_state_and_digest_tampering(
    tmp_path: Path,
) -> None:
    project_file = _file("app.py", "pass")
    manifest, generation = _publish(tmp_path, "build", (project_file,))
    external = manifest.files[0].model_copy(update={"source_size_bytes": 99})

    with pytest.raises(IndexManifestReadError, match="pinned"):
        load_index_record(tmp_path, external, manifest=manifest)

    location = manifest.files[0].record_location
    assert location is not None
    record = generation.joinpath(*location.split("/"))
    record.write_bytes(b"tampered")
    with pytest.raises(IndexManifestReadError, match="digest"):
        load_index_record(tmp_path, manifest.files[0], manifest=manifest)


@pytest.mark.parametrize(
    "malformed",
    [b"{", b"[]", b'{"schema_version":1,"schema_version":1}'],
)
def test_malformed_active_manifest_is_a_typed_error(
    tmp_path: Path, malformed: bytes
) -> None:
    layout = initialize_index(tmp_path)
    layout.active_manifest.write_bytes(malformed)

    with pytest.raises(IndexManifestReadError):
        load_manifest(tmp_path)


def test_malformed_generation_manifest_is_a_typed_error(tmp_path: Path) -> None:
    manifest, generation = _publish(tmp_path, "build-1", (_file("app.py", "pass"),))
    (generation / "manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(IndexManifestReadError):
        load_manifest(tmp_path)

    assert manifest.generation_id == generation.name


def test_unsupported_pointer_schema_version_is_not_guessed(tmp_path: Path) -> None:
    layout = initialize_index(tmp_path)
    layout.active_manifest.write_text('{"schema_version":2}\n', encoding="utf-8")

    with pytest.raises(UnsupportedIndexSchemaError) as error:
        load_manifest(tmp_path)

    assert error.value.schema_version == 2


def test_inspect_status_before_and_after_publication(tmp_path: Path) -> None:
    project_file = _file("app.py", "pass")
    initialize_index(tmp_path)

    before = inspect_index_status(
        tmp_path,
        (project_file,),
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("options"),
    )
    _publish(tmp_path, "build-1", (project_file,))
    after = inspect_index_status(
        tmp_path,
        (project_file,),
        expected_analyzer=_analyzer(),
        build_options_digest=_sha("options"),
    )

    assert before.initialized is True
    assert before.added_files == ("app.py",)
    assert after.unchanged_files == ("app.py",)
    assert after.active_generation_id is not None


def test_record_replacement_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    with acquire_index_lock(tmp_path, "build") as lock:
        location = "files/record.json"
        write_index_record(lock, location, b'{"value":"old"}\n')
        write_index_record(lock, location, b'{"value":"new"}\n')
        destination = begin_index_build(lock) / location

        assert destination.read_bytes() == b'{"value":"new"}\n'
        assert list(destination.parent.glob("*.contextforge-tmp")) == []
        assert list(destination.parent.glob(".*.contextforge-tmp")) == []


def test_failed_temporary_write_preserves_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acquire_index_lock(tmp_path, "build") as lock:
        location = "files/record.json"
        write_index_record(lock, location, b"old")
        destination = begin_index_build(lock) / location

        def fail_temporary(*args: object, **kwargs: object) -> None:
            raise PermissionError("temporary denied")

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail_temporary)
        with pytest.raises(IndexRecordWriteError, match="publish"):
            write_index_record(lock, location, b"new")

        assert destination.read_bytes() == b"old"


def test_interrupted_pointer_publish_keeps_previous_generation_and_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_file = _file("app.py", "first")
    first, _ = _publish(tmp_path, "first", (first_file,))
    second_file = _file("app.py", "second")
    second = _manifest((second_file,), previous=first.generation_id)

    with acquire_index_lock(tmp_path, "second") as lock:
        _stage_records(lock, (second_file,))
        original_replace = os.replace

        def fail_pointer(source: Path, destination: Path) -> None:
            if Path(destination) == lock.layout.active_manifest:
                raise PermissionError("pointer publish interrupted")
            original_replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_pointer)
        with pytest.raises(IndexPublicationError):
            write_manifest(lock, second)

        assert load_manifest(tmp_path) == first
        assert (lock.layout.generations / second.generation_id).is_dir()

        monkeypatch.setattr(os, "replace", original_replace)
        write_manifest(lock, second)

    assert load_manifest(tmp_path) == second


def test_stale_temporary_cleanup_is_bounded_and_skips_unrelated_files(
    tmp_path: Path,
) -> None:
    with acquire_index_lock(tmp_path, "cleanup") as lock:
        stage = begin_index_build(lock)
        stale = stage / "files/.record.123.contextforge-tmp"
        stale.write_bytes(b"partial")
        unrelated = stage / "files/keep.tmp"
        unrelated.write_bytes(b"keep")

        removed = cleanup_stale_temporary_files(lock)

        assert removed == ("staging/cleanup/files/.record.123.contextforge-tmp",)
        assert not stale.exists()
        assert unrelated.read_bytes() == b"keep"


def test_active_lock_is_rejected_even_when_recovery_is_requested(
    tmp_path: Path,
) -> None:
    first = acquire_index_lock(tmp_path, "first")
    try:
        with pytest.raises(IndexLockActiveError, match="active"):
            acquire_index_lock(
                tmp_path,
                "second",
                recover_stale=True,
                confirm_unknown=True,
                process_is_running=lambda _: True,
            )
    finally:
        first.release()


def test_stale_lock_requires_explicit_recovery_without_time_assertions(
    tmp_path: Path,
) -> None:
    layout = initialize_index(tmp_path)
    metadata = {
        "schema_version": 1,
        "run_id": "crashed",
        "pid": 999_999,
        "host_fingerprint": store_module._host_fingerprint(),
        "started_at": "2026-01-01T00:00:00+00:00",
        "owner_nonce": "dead-owner",
    }
    layout.lock.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(IndexLockRecoveryRequiredError, match="stale recovery"):
        acquire_index_lock(
            tmp_path,
            "new",
            process_is_running=lambda _: False,
        )

    recovered = acquire_index_lock(
        tmp_path,
        "new",
        recover_stale=True,
        process_is_running=lambda _: False,
    )
    recovered.release()
    assert not layout.lock.exists()


def test_unknown_lock_is_not_silently_deleted(tmp_path: Path) -> None:
    layout = initialize_index(tmp_path)
    layout.lock.write_text("not-json", encoding="utf-8")

    with pytest.raises(IndexLockRecoveryRequiredError, match="confirmation"):
        acquire_index_lock(tmp_path, "new")
    assert layout.lock.read_text(encoding="utf-8") == "not-json"

    recovered = acquire_index_lock(tmp_path, "new", confirm_unknown=True)
    recovered.release()


def test_released_or_replaced_lock_cannot_mutate_index(tmp_path: Path) -> None:
    lock = acquire_index_lock(tmp_path, "build")
    lock.release()

    with pytest.raises(IndexLockOwnershipError):
        begin_index_build(lock)


@pytest.mark.parametrize(
    "location",
    [
        "../escape.json",
        "safe/../escape.json",
        "/absolute.json",
        r"C:\absolute.json",
        "C:/absolute.json",
        "C:drive-relative.json",
        r"\\server\share\record.json",
        "manifest.json",
        "unapproved/data.json",
    ],
)
def test_record_paths_cannot_escape_generated_storage(
    tmp_path: Path, location: str
) -> None:
    with (
        acquire_index_lock(tmp_path, "build") as lock,
        pytest.raises(IndexPathError),
    ):
        write_index_record(lock, location, b"data")


def test_symlinked_generated_storage_is_rejected(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    contextforge = tmp_path / ".contextforge"
    contextforge.mkdir()
    try:
        (contextforge / "index").symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(IndexPathError, match="linked"):
        initialize_index(tmp_path)
    assert list(external.iterdir()) == []


def test_link_inside_staging_is_not_followed_for_record_writes(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    with acquire_index_lock(tmp_path, "build") as lock:
        files = begin_index_build(lock) / "files"
        link = files / "linked"
        try:
            link.symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symbolic links are unavailable: {exc}")

        with pytest.raises(IndexPathError, match="linked"):
            write_index_record(lock, "files/linked/escape.json", b"escape")

    assert list(external.iterdir()) == []


def test_junction_storage_path_is_portably_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = initialize_index(tmp_path)
    original = Path.is_junction

    def report_junction(path: Path) -> bool:
        return path == layout.index or original(path)

    monkeypatch.setattr(Path, "is_junction", report_junction)

    with pytest.raises(IndexPathError, match="linked"):
        initialize_index(tmp_path)


def test_contextforge_directory_is_entirely_scanner_protected(
    tmp_path: Path,
) -> None:
    layout = initialize_index(tmp_path)
    (layout.index / "generated.json").write_text("{}", encoding="utf-8")
    (layout.contexts / "saved.context.json").write_text("{}", encoding="utf-8")
    (layout.runs / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("pass\n", encoding="utf-8")

    snapshot = scan_repository(tmp_path)

    paths = tuple(item.path for item in snapshot.files)
    assert paths == ("source.py",)
    protected = tuple(
        item.path for item in snapshot.ignored_files if item.source == "protected"
    )
    assert protected == (".contextforge",)
    assert snapshot.summary.protected_count == 1


def test_clean_generated_index_preserves_user_and_other_generated_categories(
    tmp_path: Path,
) -> None:
    manifest, _ = _publish(tmp_path, "build", (_file("app.py", "pass"),))
    layout = initialize_index(tmp_path)
    layout.contexts.joinpath("saved.json").write_text("{}", encoding="utf-8")
    layout.runs.joinpath("run.json").write_text("{}", encoding="utf-8")
    config_before = layout.config.read_bytes()

    clean_generated_index(tmp_path)

    assert not layout.active_manifest.exists()
    assert list(layout.generations.iterdir()) == []
    assert list(layout.staging.iterdir()) == []
    assert layout.config.read_bytes() == config_before
    assert layout.contexts.joinpath("saved.json").exists()
    assert layout.runs.joinpath("run.json").exists()
    with pytest.raises(IndexManifestNotFoundError):
        load_manifest(tmp_path)
    assert manifest.generation_id


def test_clean_generated_index_does_not_follow_nested_storage_links(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with acquire_index_lock(tmp_path, "build") as lock:
        stage = begin_index_build(lock)
        try:
            (stage / "external-link").symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symbolic links are unavailable: {exc}")

    clean_generated_index(tmp_path)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_publish_rejects_missing_or_mismatched_record(tmp_path: Path) -> None:
    project_file = _file("app.py", "pass")
    manifest = _manifest((project_file,))

    with acquire_index_lock(tmp_path, "build") as lock:
        begin_index_build(lock)
        with pytest.raises((IndexPathError, IndexManifestReadError)):
            write_manifest(lock, manifest)

        write_index_record(lock, _record_location(project_file), b"wrong")
        with pytest.raises(IndexPublicationError, match="digest"):
            write_manifest(lock, manifest)


def test_conflicting_immutable_generation_is_rejected(tmp_path: Path) -> None:
    project_file = _file("app.py", "pass")
    manifest, generation = _publish(tmp_path, "first", (project_file,))
    (generation / "manifest.json").write_text("{}\n", encoding="utf-8")

    with (
        acquire_index_lock(tmp_path, "second") as lock,
        pytest.raises(IndexPublicationError, match="conflicts"),
    ):
        write_manifest(lock, manifest)


@pytest.mark.parametrize("run_id", ["../escape", ".", "..", "bad/id"])
def test_invalid_run_id_is_rejected_before_creating_a_lock(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(store_module.IndexLockError, match="run_id"):
        acquire_index_lock(tmp_path, run_id)


def test_record_content_size_and_type_are_bounded(tmp_path: Path) -> None:
    with acquire_index_lock(tmp_path, "build") as lock:
        with pytest.raises(IndexRecordWriteError, match="valid UTF-8"):
            write_index_record(lock, "files/bad.json", "bad\ud800")
        with pytest.raises(IndexRecordWriteError, match="byte limit"):
            write_index_record(
                lock,
                "files/large.json",
                b"x" * (store_module.MAX_RECORD_BYTES + 1),
            )


def test_pointer_and_manifest_never_serialize_credentials(tmp_path: Path) -> None:
    manifest, generation = _publish(tmp_path, "build", (_file("app.py", "pass"),))
    pointer_bytes = (tmp_path / ".contextforge/index/manifest.json").read_bytes()
    manifest_bytes = (generation / "manifest.json").read_bytes()

    for forbidden in (b"api_key", b"bearer", b"credential", b"secret"):
        assert forbidden not in pointer_bytes
        assert forbidden not in manifest_bytes
    assert manifest.generation_id.encode() in pointer_bytes
