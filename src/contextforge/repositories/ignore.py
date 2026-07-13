"""Git-style repository ignore-rule loading and matching.

Protected VCS metadata is matched independently and can never be negated.
Ordinary rules are combined in ascending precedence: built-in defaults,
``.gitignore``, then ``.contextforgeignore``. Within that ordinary rule stack,
Git-style last-match semantics allow later files to negate earlier exclusions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pathspec import GitIgnoreSpec

from contextforge.repositories.files import normalize_relative_path

IgnoreSource = Literal["protected", "default", "gitignore", "contextforgeignore"]

PROTECTED_IGNORE_PATTERNS = (
    ".git/",
    ".hg/",
    ".svn/",
)

DEFAULT_IGNORE_PATTERNS = (
    ".venv/",
    "venv/",
    "env/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    "node_modules/",
    "build/",
    "dist/",
    "htmlcov/",
    "*.egg-info/",
    "*.py[cod]",
    ".coverage",
)


class IgnoreRulesError(ValueError):
    """Raised when an ignore file cannot be read or parsed."""


@dataclass(frozen=True, slots=True)
class IgnoreMatch:
    """The authoritative rule that excludes a repository-relative path."""

    source: IgnoreSource
    pattern: str


@dataclass(frozen=True, slots=True)
class IgnoreRules:
    """Compiled Git-style ignore rules for one repository root."""

    root: Path
    _protected_matches: tuple[IgnoreMatch, ...]
    _ordinary_spec: GitIgnoreSpec
    _ordinary_matches: tuple[IgnoreMatch, ...]

    def is_ignored(self, path: str | Path, *, is_directory: bool = False) -> bool:
        """Return whether a repository-relative path matches the active rules."""

        return self.match(path, is_directory=is_directory) is not None

    def match(
        self, path: str | Path, *, is_directory: bool = False
    ) -> IgnoreMatch | None:
        """Return the rule excluding a path, including its source and pattern."""

        candidate = normalize_relative_path(path)
        if is_directory:
            candidate = f"{candidate}/"

        protected = _match_protected(self._protected_matches, candidate)
        if protected is not None:
            return protected
        return _match_rule(self._ordinary_spec, self._ordinary_matches, candidate)


def load_ignore_rules(
    root: Path,
    *,
    include_gitignore: bool = True,
    include_contextforgeignore: bool = True,
) -> IgnoreRules:
    """Load default and optional project ignore files from ``root``.

    Missing ignore files are normal. Invalid roots, unreadable files, invalid
    UTF-8, and patterns rejected by ``pathspec`` produce explicit exceptions.
    """

    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"repository root is not a directory: {root}")

    ordinary_rules = [
        IgnoreMatch("default", pattern) for pattern in DEFAULT_IGNORE_PATTERNS
    ]
    if include_gitignore:
        ordinary_rules.extend(
            IgnoreMatch("gitignore", pattern)
            for pattern in _read_ignore_file(resolved_root / ".gitignore")
        )
    if include_contextforgeignore:
        ordinary_rules.extend(
            IgnoreMatch("contextforgeignore", pattern)
            for pattern in _read_ignore_file(resolved_root / ".contextforgeignore")
        )

    protected_rules = tuple(
        IgnoreMatch("protected", pattern) for pattern in PROTECTED_IGNORE_PATTERNS
    )

    try:
        ordinary_spec, ordinary_matches = _compile_rules(ordinary_rules)
    except Exception as exc:
        raise IgnoreRulesError(f"invalid ignore pattern for root: {root}") from exc
    return IgnoreRules(
        root=resolved_root,
        _protected_matches=protected_rules,
        _ordinary_spec=ordinary_spec,
        _ordinary_matches=ordinary_matches,
    )


def _compile_rules(
    rules: list[IgnoreMatch] | tuple[IgnoreMatch, ...],
) -> tuple[GitIgnoreSpec, tuple[IgnoreMatch, ...]]:
    nonempty_rules = tuple(rule for rule in rules if rule.pattern)
    spec = GitIgnoreSpec.from_lines(rule.pattern for rule in nonempty_rules)
    return spec, nonempty_rules


def _match_rule(
    spec: GitIgnoreSpec, rules: tuple[IgnoreMatch, ...], candidate: str
) -> IgnoreMatch | None:
    result = spec.check_file(candidate)
    if not result.include or result.index is None:
        return None
    return rules[result.index]


def _match_protected(
    rules: tuple[IgnoreMatch, ...], candidate: str
) -> IgnoreMatch | None:
    path_parts = candidate.rstrip("/").split("/")
    for rule in rules:
        protected_name = rule.pattern.rstrip("/").casefold()
        if any(part.casefold() == protected_name for part in path_parts):
            return rule
    return None


def _read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise IgnoreRulesError(f"unable to read ignore file: {path.name}") from exc
