import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from contextforge.context import (
    MAX_JSON_PACKAGE_BYTES,
    ContextBuildOptions,
    ContextPackage,
    ContextProject,
    ContextRenderError,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    ProjectTree,
    build_context_package,
    calculate_context_statistics,
    render_context_package_json,
    render_context_package_markdown,
)
from contextforge.repositories import scan_repository


def _build(
    root: Path,
    files: Mapping[str, str | bytes],
    *,
    title: str = "Review the project",
    selection: ContextSelection | None = None,
    include_tree: bool = True,
) -> ContextPackage:
    for relative_path, content in files.items():
        path = root.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
    return build_context_package(
        scan_repository(root),
        ContextBuildOptions(
            title=title,
            selection=selection or ContextSelection(),
            include_tree=include_tree,
        ),
    )


def _empty_package(*, include_tree: bool = True) -> ContextPackage:
    project = ContextProject(
        selectable_file_count=0,
        selectable_directory_count=0,
        selectable_source_bytes=0,
    )
    files = ()
    return ContextPackage(
        title="Empty package",
        project=project,
        tree=ProjectTree() if include_tree else None,
        files=files,
        statistics=calculate_context_statistics(project, files),
    )


def test_markdown_is_deterministic_structured_lf_only_and_portable(
    tmp_path: Path,
) -> None:
    package = _build(
        tmp_path,
        {"src/app.py": "print('hello')\r\n", "README.md": "# Read me\n"},
    )

    first = render_context_package_markdown(package)
    second = render_context_package_markdown(package)

    assert first == second
    assert first.startswith("# Review the project\n\nTask: Review the project\n")
    assert first.index("## Project tree") < first.index("## Statistics")
    assert first.index("## Statistics") < first.index("## Files")
    assert first.index("### `README.md`") < first.index("### `src/app.py`")
    assert "\r" not in first
    assert first.endswith("\n") and not first.endswith("\n\n")
    assert str(tmp_path.resolve()) not in first
    assert "Source SHA-256:" in first


def test_markdown_optional_tree_and_empty_package_are_representable() -> None:
    with_tree = render_context_package_markdown(_empty_package())
    without_tree = render_context_package_markdown(_empty_package(include_tree=False))

    assert "## Project tree\n\n```text\n.\n```" in with_tree
    assert "## Project tree" not in without_tree
    assert "## Files\n\n_(none)_\n" in with_tree
    assert "- Selected files: 0" in with_tree


def test_markdown_one_unknown_language_empty_file_has_unlabelled_fence(
    tmp_path: Path,
) -> None:
    package = _build(tmp_path, {"empty.unknown": ""})

    rendered = render_context_package_markdown(package)

    assert "- Language: unknown" in rendered
    assert "- Source bytes: 0" in rendered
    assert "- Source lines: 0" in rendered
    assert "- Included lines: all" in rendered
    assert "\n```\n\n```\n" in rendered
    assert "```unknown" not in rendered


@pytest.mark.parametrize(
    ("content", "fence_length"),
    [
        ("one ` tick\n", 3),
        ("```\n", 4),
        ("````\n", 5),
        ("Example:\n```markdown\n~~~\n```\n", 4),
        ("```python\nprint('unterminated')\n", 4),
    ],
)
def test_markdown_fence_is_longer_than_every_source_backtick_run(
    tmp_path: Path, content: str, fence_length: int
) -> None:
    package = _build(tmp_path, {"sample.unknown": content})

    rendered = render_context_package_markdown(package)
    fence = "`" * fence_length

    assert f"\n{fence}\n{content}{fence}\n" in rendered
    assert package.files[0].blocks[0].text == content


def test_markdown_fence_adds_only_a_framing_newline_for_no_final_lf(
    tmp_path: Path,
) -> None:
    content = "before\n```\nafter"
    package = _build(tmp_path, {"sample.md": content})

    rendered = render_context_package_markdown(package)

    assert "\n````\nbefore\n```\nafter\n````\n" in rendered
    assert package.files[0].blocks[0].text == content


def test_markdown_range_blocks_have_canonical_labels_and_metadata(
    tmp_path: Path,
) -> None:
    selection = ContextSelection(
        line_ranges=(
            LineRangeRequest("app.py", LineRange(4, 4)),
            LineRangeRequest("app.py", LineRange(1, 2)),
        )
    )
    package = _build(tmp_path, {"app.py": "one\ntwo\nthree\nfour"}, selection=selection)

    rendered = render_context_package_markdown(package)

    assert "- Included lines: 1-2, 4-4" in rendered
    assert rendered.index("#### Lines 1-2") < rendered.index("#### Lines 4-4")
    assert "\n```\none\ntwo\n```\n" in rendered
    assert "\n```\nfour\n```\n" in rendered


def test_markdown_escapes_title_and_uses_safe_path_code_span(tmp_path: Path) -> None:
    package = _build(
        tmp_path,
        {"docs/a``b.md": "content"},
        title="# Review *carefully*",
    )

    rendered = render_context_package_markdown(package)

    assert rendered.startswith(r"# \# Review \*carefully\*" + "\n")
    assert "### ```docs/a``b.md```" in rendered


def test_project_tree_fence_is_safe_for_backticks_in_paths(tmp_path: Path) -> None:
    package = _build(tmp_path, {"docs/```example.md": "content"})

    rendered = render_context_package_markdown(package)

    assert "````text\n.\n`-- docs/\n    `-- ```example.md\n````" in rendered


def test_markdown_path_code_span_padding_and_controls_are_safe(tmp_path: Path) -> None:
    package = _build(tmp_path, {"`leading.txt": "content"}, title="Review\tthis")
    context_file = package.files[0].model_copy(update={"path": "line\nname.txt"})
    forged = package.model_copy(update={"files": (context_file,)})

    normal = render_context_package_markdown(package)
    controlled = render_context_package_markdown(forged)

    assert "### `` `leading.txt ``" in normal
    assert r"# Review\\tthis" in normal
    assert r"### `line\nname.txt`" in controlled


def test_json_is_deterministic_sorted_unicode_and_round_trip_ready(
    tmp_path: Path,
) -> None:
    package = _build(
        tmp_path,
        {"данные/пример.py": "print('Привет, 世界')\n", "empty": ""},
    )

    first = render_context_package_json(package)
    second = render_context_package_json(package)

    assert first == second
    assert "Привет, 世界" in first
    assert "данные/пример.py" in first
    assert "\\u041f" not in first
    assert "\r" not in first
    assert first.endswith("\n") and not first.endswith("\n\n")
    assert list(json.loads(first)) == [
        "files",
        "project",
        "schema_version",
        "statistics",
        "title",
        "tree",
    ]
    assert str(tmp_path.resolve()) not in first


def test_json_empty_package_has_exact_stable_schema_golden() -> None:
    rendered = render_context_package_json(_empty_package(include_tree=False))

    assert (
        rendered
        == """{
  "files": [],
  "project": {
    "languages": {},
    "selectable_directory_count": 0,
    "selectable_file_count": 0,
    "selectable_source_bytes": 0
  },
  "schema_version": 1,
  "statistics": {
    "included_character_count": 0,
    "included_content_bytes": 0,
    "included_line_count": 0,
    "languages": {},
    "ranged_file_count": 0,
    "selected_file_count": 0,
    "selected_source_bytes": 0,
    "tree_directory_count": 0,
    "tree_file_count": 0
  },
  "title": "Empty package",
  "tree": null
}
"""
    )


def test_json_adds_no_literal_ansi_codes(tmp_path: Path) -> None:
    package = _build(tmp_path, {"ansi.txt": "before\x1b[31mafter"})

    rendered = render_context_package_json(package)

    assert "\x1b" not in rendered
    assert r"\u001b[31m" in rendered


def test_json_render_size_limit_accepts_equality_and_rejects_one_over(
    tmp_path: Path,
) -> None:
    package = _build(tmp_path, {"file.txt": "text"})
    rendered = render_context_package_json(package)
    size = len(rendered.encode())

    assert render_context_package_json(package, max_size_bytes=size) == rendered
    with pytest.raises(ContextRenderError, match="limit"):
        render_context_package_json(package, max_size_bytes=size - 1)


def test_default_json_render_limit_accepts_ten_megabytes_and_rejects_one_over() -> None:
    package = _empty_package(include_tree=False)
    one_character = package.model_copy(update={"title": "x"})
    fixed_size = len(render_context_package_json(one_character).encode()) - 1
    exact = package.model_copy(
        update={"title": "x" * (MAX_JSON_PACKAGE_BYTES - fixed_size)}
    )
    over = exact.model_copy(update={"title": exact.title + "x"})

    rendered = render_context_package_json(exact)

    assert len(rendered.encode()) == MAX_JSON_PACKAGE_BYTES
    with pytest.raises(ContextRenderError, match="limit"):
        render_context_package_json(over)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_json_render_limit_requires_a_positive_integer(limit: object) -> None:
    with pytest.raises(ContextRenderError, match="positive integer"):
        render_context_package_json(
            _empty_package(),
            max_size_bytes=limit,  # type: ignore[arg-type]
        )


def test_renderers_reject_non_packages() -> None:
    with pytest.raises(ContextRenderError, match="ContextPackage"):
        render_context_package_markdown(object())  # type: ignore[arg-type]
    with pytest.raises(ContextRenderError, match="ContextPackage"):
        render_context_package_json(object())  # type: ignore[arg-type]


def test_renderers_reject_invalid_unicode_in_forged_package() -> None:
    forged = _empty_package().model_copy(update={"title": "bad\ud800"})

    with pytest.raises(ContextRenderError, match="invalid Unicode"):
        render_context_package_markdown(forged)
    with pytest.raises(ContextRenderError, match="UTF-8 JSON"):
        render_context_package_json(forged)
