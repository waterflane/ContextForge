import hashlib
import json
from pathlib import Path

import pytest

from contextforge.intelligence import (
    acquire_index_lock,
    build_structural_index,
    load_file_code_map,
    load_manifest,
)
from contextforge.intelligence.legacy import load_legacy_envelope
from contextforge.intelligence.manifest import (
    calculate_generation_id,
    canonical_json_bytes,
)
from contextforge.intelligence.models import (
    ActiveIndexPointer,
    IndexManifest,
    SchemaVersionMetadata,
)
from contextforge.intelligence.store import (
    IndexPublicationError,
    _validate_record_schema,
    index_publication_transaction,
)
from contextforge.repositories import ProjectSnapshot, scan_repository


def _legacy_index(root: Path) -> tuple[ProjectSnapshot, IndexManifest]:
    (root / "sample.ts").write_text("export function run() {}\n", encoding="utf-8")
    snapshot = scan_repository(root)
    with acquire_index_lock(root, "seed") as lock:
        current = build_structural_index(snapshot, lock)
    code_map = current.code_maps[0].model_copy(update={"schema_version": 1})
    content = canonical_json_bytes(code_map.model_dump(mode="json"))
    state = current.manifest.files[0].model_copy(
        update={
            "record_sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    legacy = current.manifest.model_copy(
        update={
            "schema_version": 1,
            "schema_versions": SchemaVersionMetadata(
                index_schema_version=1,
                manifest_schema_version=1,
                record_schema_version=1,
            ),
            "files": (state,),
        }
    )
    legacy = legacy.model_copy(
        update={"generation_id": calculate_generation_id(legacy)}
    )
    generation = current.generation_path.parent / legacy.generation_id
    assert state.record_location is not None
    record = generation / state.record_location
    record.parent.mkdir(parents=True)
    record.write_bytes(content)
    (generation / "manifest.json").write_bytes(
        canonical_json_bytes(legacy.model_dump(mode="json"))
    )
    pointer = ActiveIndexPointer(
        schema_version=1,
        generation_id=legacy.generation_id,
        generation_manifest=f"generations/{legacy.generation_id}/manifest.json",
        source_snapshot_digest=legacy.build.source_snapshot_digest,
    )
    (root / ".contextforge/index/manifest.json").write_bytes(
        canonical_json_bytes(pointer.model_dump(mode="json"))
    )
    return snapshot, legacy


def test_v1_inspection_and_v2_rebuild_do_not_reuse_records(tmp_path: Path) -> None:
    snapshot, legacy = _legacy_index(tmp_path)
    assert load_manifest(tmp_path) == legacy
    assert load_file_code_map(tmp_path, "sample.ts").schema_version == 1
    with acquire_index_lock(tmp_path, "migrate") as lock:
        result = build_structural_index(snapshot, lock)
    assert result.manifest.schema_version == 2
    assert result.extracted_paths == ("sample.ts",)
    assert result.reused_paths == ()
    assert load_file_code_map(tmp_path, "sample.ts").schema_version == 2
    assert (result.generation_path.parent / legacy.generation_id).is_dir()


def test_failed_migration_does_not_change_active_pointer(tmp_path: Path) -> None:
    snapshot, legacy = _legacy_index(tmp_path)
    pointer = tmp_path / ".contextforge/index/manifest.json"
    before = pointer.read_bytes()
    with (
        pytest.raises(RuntimeError, match="fail after structural"),
        acquire_index_lock(tmp_path, "failed-migration") as lock,
        index_publication_transaction(lock),
    ):
        build_structural_index(snapshot, lock)
        assert load_manifest(tmp_path).schema_version == 2
        assert pointer.read_bytes() == before
        raise RuntimeError("fail after structural")
    assert pointer.read_bytes() == before
    assert load_manifest(tmp_path) == legacy


def test_successful_transaction_switches_pointer_only_at_exit(tmp_path: Path) -> None:
    snapshot, _ = _legacy_index(tmp_path)
    pointer = tmp_path / ".contextforge/index/manifest.json"
    with (
        acquire_index_lock(tmp_path, "successful-migration") as lock,
        index_publication_transaction(lock),
    ):
        build_structural_index(snapshot, lock)
        assert json.loads(pointer.read_bytes())["schema_version"] == 1
    assert json.loads(pointer.read_bytes())["schema_version"] == 2


def test_mixed_legacy_versions_and_nested_transactions_are_rejected(
    tmp_path: Path,
) -> None:
    _, legacy = _legacy_index(tmp_path)
    payload = legacy.model_dump(mode="json")
    payload["schema_versions"]["record_schema_version"] = 2
    with pytest.raises(ValueError, match="mixed legacy"):
        load_legacy_envelope(payload, IndexManifest)
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="not a legacy"):
        load_legacy_envelope(payload, IndexManifest)
    with pytest.raises(IndexPublicationError, match="mixed record"):
        _validate_record_schema(
            b'{"record_kind":"verified_file_codemap","schema_version":1}', 2
        )
    with (
        acquire_index_lock(tmp_path, "nested") as lock,
        index_publication_transaction(lock),
        pytest.raises(IndexPublicationError, match="nested"),
        index_publication_transaction(lock),
    ):
        pass
