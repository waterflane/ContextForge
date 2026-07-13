from pathlib import Path

import pytest

from contextforge.repositories.ignore import IgnoreRulesError, load_ignore_rules


def test_missing_project_ignore_files_are_normal(tmp_path: Path) -> None:
    rules = load_ignore_rules(tmp_path)

    assert rules.is_ignored("src/app.py") is False
    assert rules.is_ignored(".git", is_directory=True) is True
    assert rules.is_ignored(r"nested\__pycache__", is_directory=True) is True
    assert rules.is_ignored("package.pyc") is True


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
