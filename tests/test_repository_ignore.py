from pathlib import Path

import pytest

from contextforge.repositories.ignore import IgnoreRulesError, load_ignore_rules


def test_missing_project_ignore_files_are_normal(tmp_path: Path) -> None:
    rules = load_ignore_rules(tmp_path)

    assert rules.is_ignored("src/app.py") is False
    assert rules.is_ignored(".git", is_directory=True) is True
    assert rules.is_ignored(r"nested\__pycache__", is_directory=True) is True
    assert rules.is_ignored("package.pyc") is True


@pytest.mark.parametrize(
    ("path", "is_directory"),
    [
        ("__pycache__", True),
        ("module.pyc", False),
        ("module.pyo", False),
        ("module.pyd", False),
        (".pytest_cache", True),
        (".mypy_cache", True),
        (".ruff_cache", True),
        (".uv-cache", True),
        (".coverage", False),
        ("htmlcov", True),
        (".tox", True),
        (".nox", True),
        (".venv", True),
        ("venv", True),
        ("env", True),
        ("node_modules", True),
        ("build", True),
        ("dist", True),
        ("package.egg-info", True),
    ],
)
def test_default_rules_cover_targeted_generated_artifacts(
    tmp_path: Path, path: str, is_directory: bool
) -> None:
    rules = load_ignore_rules(tmp_path)

    match = rules.match(path, is_directory=is_directory)

    assert match is not None
    assert match.source == "default"


def test_gitignore_supports_comments_blanks_directories_and_wildcards(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(
        "# generated output\n\ncache/\n*.log\n", encoding="utf-8"
    )

    rules = load_ignore_rules(tmp_path)

    assert rules.is_ignored("cache", is_directory=True) is True
    assert rules.is_ignored("cache/nested/data.json") is True
    assert rules.is_ignored("nested/debug.log") is True
    assert rules.is_ignored("nested/app.py") is False


def test_contextforgeignore_has_highest_precedence(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text(
        "!keep.log\nprivate/**\n", encoding="utf-8"
    )

    rules = load_ignore_rules(tmp_path)

    assert rules.is_ignored("debug.log") is True
    assert rules.is_ignored("keep.log") is False
    assert rules.is_ignored("private/nested/data.txt") is True


def test_project_rules_can_negate_default_exclusions(tmp_path: Path) -> None:
    (tmp_path / ".contextforgeignore").write_text(
        "!.venv/\n!.venv/keep.py\n", encoding="utf-8"
    )

    rules = load_ignore_rules(tmp_path)

    assert rules.is_ignored(".venv", is_directory=True) is False
    assert rules.is_ignored(".venv/keep.py") is False


@pytest.mark.parametrize("metadata_directory", [".git", ".hg", ".svn"])
def test_project_rules_cannot_negate_protected_vcs_metadata(
    tmp_path: Path, metadata_directory: str
) -> None:
    (tmp_path / ".contextforgeignore").write_text(
        f"!{metadata_directory}/\n!{metadata_directory}/config\n", encoding="utf-8"
    )

    rules = load_ignore_rules(tmp_path)

    match = rules.match(metadata_directory, is_directory=True)
    file_match = rules.match(metadata_directory)
    nested_match = rules.match(f"{metadata_directory}/config")
    assert match is not None
    assert match.source == "protected"
    assert file_match is not None
    assert file_match.source == "protected"
    assert nested_match is not None
    assert nested_match.source == "protected"


@pytest.mark.parametrize("metadata_path", [".GIT/config", "nested/.Hg/store", ".SvN"])
def test_protected_vcs_matching_is_case_safe(
    tmp_path: Path, metadata_path: str
) -> None:
    rules = load_ignore_rules(tmp_path)

    match = rules.match(metadata_path)

    assert match is not None
    assert match.source == "protected"


def test_ignore_match_reports_authoritative_source_and_pattern(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text(
        "!keep.log\nprivate.txt\n", encoding="utf-8"
    )

    rules = load_ignore_rules(tmp_path)

    default_match = rules.match("build/output.txt")
    git_match = rules.match("debug.log")
    context_match = rules.match("private.txt")
    assert default_match is not None
    assert (default_match.source, default_match.pattern) == ("default", "build/")
    assert git_match is not None
    assert (git_match.source, git_match.pattern) == ("gitignore", "*.log")
    assert context_match is not None
    assert (context_match.source, context_match.pattern) == (
        "contextforgeignore",
        "private.txt",
    )
    assert rules.match("keep.log") is None


def test_ignore_files_can_be_disabled_independently(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("git-only.txt\n", encoding="utf-8")
    (tmp_path / ".contextforgeignore").write_text(
        "context-only.txt\n", encoding="utf-8"
    )

    rules = load_ignore_rules(
        tmp_path, include_gitignore=False, include_contextforgeignore=False
    )

    assert rules.is_ignored("git-only.txt") is False
    assert rules.is_ignored("context-only.txt") is False
    assert rules.is_ignored("dist", is_directory=True) is True


def test_ignore_rules_reject_invalid_roots(tmp_path: Path) -> None:
    file_root = tmp_path / "file.txt"
    file_root.write_text("text", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        load_ignore_rules(tmp_path / "missing")
    with pytest.raises(NotADirectoryError):
        load_ignore_rules(file_root)


@pytest.mark.parametrize("ignore_name", [".gitignore", ".contextforgeignore"])
def test_invalid_utf8_ignore_file_has_predictable_error(
    tmp_path: Path, ignore_name: str
) -> None:
    (tmp_path / ignore_name).write_bytes(b"*.log\n\xff")

    with pytest.raises(IgnoreRulesError, match="unable to read ignore file"):
        load_ignore_rules(tmp_path)


def test_non_file_ignore_path_has_predictable_error(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").mkdir()

    with pytest.raises(IgnoreRulesError, match="unable to read ignore file"):
        load_ignore_rules(tmp_path)


def test_malformed_ignore_pattern_has_predictable_error(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("trailing-escape\\\n", encoding="utf-8")

    with pytest.raises(IgnoreRulesError, match="invalid ignore pattern"):
        load_ignore_rules(tmp_path)


def test_ignore_match_rejects_absolute_paths(tmp_path: Path) -> None:
    rules = load_ignore_rules(tmp_path)

    with pytest.raises(ValueError, match="relative path"):
        rules.is_ignored("C:/absolute/file.py")
