import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from contextforge.context import (
    MAX_JSON_PACKAGE_BYTES,
    ContextBuildOptions,
    ContextInspection,
    ContextPackage,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    PackageReadError,
    PackageValidationError,
    UnsupportedSchemaVersionError,
    build_context_package,
    inspect_context_package,
    inspect_context_package_json,
    load_context_package_json,
    render_context_inspection,
    render_context_package_json,
    validate_context_package,
)
from contextforge.repositories import scan_repository


def _build(
    root: Path,
    files: Mapping[str, str],
    *,
    selection: ContextSelection | None = None,
) -> ContextPackage:
    for relative_path, content in files.items():
        path = root.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    return build_context_package(
        scan_repository(root),
        ContextBuildOptions(
            title="Inspect Привет",
            selection=selection or ContextSelection(),
        ),
    )


def _payload(package: ContextPackage) -> dict[str, object]:
    return cast(dict[str, object], json.loads(render_context_package_json(package)))


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_json_text_and_bytes_round_trip_to_the_canonical_package(
    tmp_path: Path,
) -> None:
    package = _build(
        tmp_path,
        {"src/app.py": "print('Привет')\n", "README.md": "# 文档\n"},
    )
    rendered = render_context_package_json(package)

    assert load_context_package_json(rendered) == package
    assert load_context_package_json(rendered.encode()) == package
    assert render_context_package_json(load_context_package_json(rendered)) == rendered


def test_single_leading_utf8_bom_is_accepted_for_bytes_and_text(
    tmp_path: Path,
) -> None:
    package = _build(tmp_path, {"file.txt": "text"})
    rendered = render_context_package_json(package)

    assert load_context_package_json(b"\xef\xbb\xbf" + rendered.encode()) == package
    assert load_context_package_json("\ufeff" + rendered) == package


@pytest.mark.parametrize("data", [b"\xff", b'{"title":"\xff"}'])
def test_invalid_utf8_is_a_read_error(data: bytes) -> None:
    with pytest.raises(PackageReadError, match="valid UTF-8"):
        load_context_package_json(data)


@pytest.mark.parametrize(
    "data",
    ["", "{", "[]", "{} {}", "null", '{"schema_version": NaN}'],
)
def test_malformed_or_wrong_root_json_is_rejected_clearly(data: str) -> None:
    with pytest.raises(PackageValidationError):
        load_context_package_json(data)


def test_duplicate_json_keys_are_rejected_at_any_object_level(
    tmp_path: Path,
) -> None:
    package = _build(tmp_path, {"file.txt": "text"})
    rendered = render_context_package_json(package)
    duplicate_root = rendered.replace(
        '"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,', 1
    )
    duplicate_nested = rendered.replace(
        '"selected_file_count": 1,',
        '"selected_file_count": 1,\n    "selected_file_count": 1,',
        1,
    )

    with pytest.raises(PackageValidationError, match="duplicate JSON object key"):
        load_context_package_json(duplicate_root)
    with pytest.raises(PackageValidationError, match="duplicate JSON object key"):
        load_context_package_json(duplicate_nested)


def test_unknown_integer_schema_version_has_a_specific_error(tmp_path: Path) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    payload["schema_version"] = 2

    with pytest.raises(UnsupportedSchemaVersionError) as error:
        load_context_package_json(_json(payload))

    assert error.value.schema_version == 2


@pytest.mark.parametrize("version", ["1", 1.0, True, None])
def test_schema_version_is_never_coerced(tmp_path: Path, version: object) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    payload["schema_version"] = version

    with pytest.raises(PackageValidationError, match="schema_version"):
        load_context_package_json(_json(payload))


def test_root_requires_only_stable_canonical_field_names(tmp_path: Path) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    missing = copy.deepcopy(payload)
    del missing["title"]
    alias = copy.deepcopy(payload)
    alias["task_description"] = alias.pop("title")
    extra = copy.deepcopy(payload)
    extra["machine"] = "developer-laptop"

    with pytest.raises(PackageValidationError, match="missing required field: title"):
        load_context_package_json(_json(missing))
    with pytest.raises(PackageValidationError, match="missing required field: title"):
        load_context_package_json(_json(alias))
    with pytest.raises(PackageValidationError, match="unknown field: machine"):
        load_context_package_json(_json(extra))


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        r"C:\repo\file.txt",
        "C:/repo/file.txt",
        r"C:file.txt",
        r"\\server\share\file.txt",
        "safe/../file.txt",
    ],
)
def test_unsafe_absolute_drive_relative_and_traversal_paths_are_rejected(
    tmp_path: Path, path: str
) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    payload["files"][0]["path"] = path  # type: ignore[index]

    with pytest.raises(PackageValidationError, match="portable relative path"):
        load_context_package_json(_json(payload))


def test_duplicate_and_invalid_file_order_are_rejected(tmp_path: Path) -> None:
    payload = _payload(_build(tmp_path, {"a.txt": "a", "b.txt": "b"}))
    duplicate = copy.deepcopy(payload)
    duplicate_files = cast(list[object], duplicate["files"])
    duplicate_files.append(copy.deepcopy(duplicate_files[0]))
    unsorted = copy.deepcopy(payload)
    cast(list[object], unsorted["files"]).reverse()

    with pytest.raises(PackageValidationError, match="unique paths"):
        load_context_package_json(_json(duplicate))
    with pytest.raises(PackageValidationError, match="canonical order"):
        load_context_package_json(_json(unsorted))


def test_invalid_tree_order_is_rejected(tmp_path: Path) -> None:
    payload = _payload(_build(tmp_path, {"a.txt": "a", "src/b.txt": "b"}))
    tree = cast(dict[str, object], payload["tree"])
    cast(list[object], tree["entries"]).reverse()

    with pytest.raises(PackageValidationError, match="canonical pre-order"):
        load_context_package_json(_json(payload))


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "g" * 64, 123])
def test_invalid_source_sha256_is_rejected_without_coercion(
    tmp_path: Path, digest: object
) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    payload["files"][0]["source_sha256"] = digest  # type: ignore[index]

    with pytest.raises(PackageValidationError, match="source_sha256"):
        load_context_package_json(_json(payload))


def test_block_hash_and_statistics_tampering_are_rejected(tmp_path: Path) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    bad_hash = copy.deepcopy(payload)
    bad_hash["files"][0]["blocks"][0]["sha256"] = "a" * 64  # type: ignore[index]
    bad_statistics = copy.deepcopy(payload)
    bad_statistics["statistics"]["included_content_bytes"] = 999  # type: ignore[index]

    with pytest.raises(PackageValidationError, match="sha256"):
        load_context_package_json(_json(bad_hash))
    with pytest.raises(PackageValidationError, match="statistics"):
        load_context_package_json(_json(bad_statistics))


def test_strict_schema_rejects_float_counts_and_unknown_nested_fields(
    tmp_path: Path,
) -> None:
    payload = _payload(_build(tmp_path, {"file.txt": "text"}))
    float_count = copy.deepcopy(payload)
    float_count["tree"]["file_count"] = 1.0  # type: ignore[index]
    unknown = copy.deepcopy(payload)
    unknown["files"][0]["local_path"] = str(tmp_path / "file.txt")  # type: ignore[index]

    with pytest.raises(PackageValidationError, match="tree.file_count"):
        load_context_package_json(_json(float_count))
    with pytest.raises(PackageValidationError, match="local_path"):
        load_context_package_json(_json(unknown))


def test_ranged_and_empty_files_inspect_successfully(tmp_path: Path) -> None:
    selection = ContextSelection(
        line_ranges=(LineRangeRequest("a.txt", LineRange(1, 1)),)
    )
    package = _build(
        tmp_path, {"a.txt": "one\ntwo\n", "empty": ""}, selection=selection
    )

    loaded, inspection = inspect_context_package_json(
        render_context_package_json(package)
    )

    assert loaded == package
    assert inspection.selected_file_count == 2
    assert inspection.ranged_file_count == 1
    assert inspection.included_line_count == 1


def test_inspection_summary_and_display_are_deterministic(tmp_path: Path) -> None:
    package = _build(tmp_path, {"README.md": "readme\n", "src/app.py": "app\n"})

    first = inspect_context_package(package)
    second = inspect_context_package(package)
    rendered = render_context_inspection(first)

    assert first == second
    assert first.languages == {"Markdown": 1, "Python": 1}
    assert rendered == (
        "Schema version: 1\n"
        "Title: Inspect Привет\n"
        "Task: Inspect Привет\n"
        "Selectable files: 2\n"
        "Selectable directories: 1\n"
        "Selected files: 2\n"
        "Ranged files: 0\n"
        "Included content bytes: 11\n"
        "Included characters: 11\n"
        "Included lines: 2\n"
        "Languages: Markdown: 1, Python: 1\n"
        "Selected paths:\n"
        "  README.md (all lines)\n"
        "  src/app.py (all lines)\n"
    )


def test_inspection_never_accesses_paths_named_inside_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _build(tmp_path, {"src/app.py": "app"})
    rendered = render_context_package_json(package).encode()

    def reject_filesystem(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"unexpected filesystem access: {args!r} {kwargs!r}")

    for method in ("open", "resolve", "exists", "stat", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, method, reject_filesystem)

    loaded = load_context_package_json(rendered)
    assert loaded.files[0].path == "src/app.py"


def test_input_size_limit_accepts_equality_and_rejects_one_over(
    tmp_path: Path,
) -> None:
    rendered = render_context_package_json(_build(tmp_path, {"file.txt": "text"}))
    size = len(rendered.encode())

    assert load_context_package_json(rendered, max_size_bytes=size)
    with pytest.raises(PackageReadError, match="limit"):
        load_context_package_json(rendered, max_size_bytes=size - 1)
    with pytest.raises(PackageReadError, match="limit"):
        load_context_package_json(rendered.encode(), max_size_bytes=size - 1)


def test_default_input_limit_accepts_ten_megabytes_and_rejects_one_over(
    tmp_path: Path,
) -> None:
    rendered = render_context_package_json(_build(tmp_path, {"file.txt": "text"}))
    encoded = rendered.encode()
    exact = b" " * (MAX_JSON_PACKAGE_BYTES - len(encoded)) + encoded
    over = b" " + exact

    assert load_context_package_json(exact)
    with pytest.raises(PackageReadError, match="limit"):
        load_context_package_json(over)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_input_size_limit_requires_a_positive_integer(limit: object) -> None:
    with pytest.raises(PackageReadError, match="positive integer"):
        load_context_package_json("{}", max_size_bytes=limit)  # type: ignore[arg-type]


def test_loader_and_inspection_reject_wrong_runtime_types() -> None:
    with pytest.raises(PackageReadError, match="bytes or text"):
        load_context_package_json(bytearray(b"{}"))  # type: ignore[arg-type]
    with pytest.raises(PackageValidationError, match="ContextPackage"):
        validate_context_package(object())  # type: ignore[arg-type]
    with pytest.raises(PackageValidationError, match="ContextInspection"):
        render_context_inspection(object())  # type: ignore[arg-type]
    with pytest.raises(PackageReadError, match="invalid Unicode"):
        load_context_package_json("bad\ud800")


def test_inspection_revalidates_forged_package_instances(tmp_path: Path) -> None:
    package = _build(tmp_path, {"file.txt": "text"})
    forged_statistics = package.statistics.model_copy(
        update={"included_line_count": 99}
    )
    forged = package.model_copy(update={"statistics": forged_statistics})

    with pytest.raises(PackageValidationError, match="statistics"):
        inspect_context_package(forged)

    unserializable = ContextPackage.model_construct(project=object())
    with pytest.raises(PackageValidationError, match="validated as JSON"):
        validate_context_package(unserializable)


def test_inspection_model_is_frozen_and_forbids_unknown_fields() -> None:
    inspection = ContextInspection(
        schema_version=1,
        title="Package",
        selectable_file_count=0,
        selectable_directory_count=0,
        selected_file_count=0,
        ranged_file_count=0,
        included_content_bytes=0,
        included_character_count=0,
        included_line_count=0,
        languages={},
    )

    with pytest.raises(ValidationError):
        inspection.title = "changed"
    with pytest.raises(ValidationError):
        ContextInspection(
            schema_version=1,
            title="Package",
            selectable_file_count=0,
            selectable_directory_count=0,
            selected_file_count=0,
            ranged_file_count=0,
            included_content_bytes=0,
            included_character_count=0,
            included_line_count=0,
            languages={},
            unknown=True,  # type: ignore[call-arg]
        )
