"""Git-style repository ignore-rule loading and matching.

Protected VCS metadata is matched independently and can never be negated.
Ordinary rules are combined in ascending precedence: built-in defaults,
repository and nested ``.gitignore`` files, then ``.contextforgeignore``.
Nested Git rules are scoped to the directory containing their control file.
Within that ordinary rule stack, Git-style last-match semantics allow later
files to negate earlier exclusions.
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
    ".contextforge/index/",
    ".contextforge/contexts/",
    ".contextforge/runs/",
)

DEFAULT_IGNORE_PATTERNS = (
    ".venv/",
    "venv/",
    "env/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".uv-cache/",
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
class _RuleScope:
    """One compiled rule file applied below a repository-relative directory."""

    base: str
    spec: GitIgnoreSpec
    matches: tuple[IgnoreMatch, ...]


@dataclass(frozen=True, slots=True)
class IgnoreRules:
    """Compiled inherited Git-style ignore rules for one traversal scope."""

    root: Path
    _protected_matches: tuple[IgnoreMatch, ...]
    _default_scope: _RuleScope
    _gitignore_scopes: tuple[_RuleScope, ...]
    _contextforge_scope: _RuleScope
    _include_gitignore: bool

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
        return _match_ordinary(
            (
                self._default_scope,
                *self._gitignore_scopes,
                self._contextforge_scope,
            ),
            candidate,
        )

    def for_directory(self, directory: Path) -> IgnoreRules:
        """Return inherited rules extended by ``directory/.gitignore``.

        The root control file is loaded by :func:`load_ignore_rules`. A nested
        control file is read only after traversal has established that its
        containing directory is reachable, matching Git's ignored-parent rule.
        """

        if not self._include_gitignore or directory == self.root:
            return self
        try:
            relative = directory.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "ignore-rule directory is outside repository root"
            ) from exc
        base = normalize_relative_path(relative)
        rules = tuple(
            IgnoreMatch("gitignore", pattern)
            for pattern in _read_ignore_file(directory / ".gitignore")
        )
        if not rules:
            return self
        try:
            scope = _compile_scope(base, rules)
        except Exception as exc:
            raise IgnoreRulesError(
                f"invalid ignore pattern for directory: {base}/.gitignore"
            ) from exc
        return IgnoreRules(
            root=self.root,
            _protected_matches=self._protected_matches,
            _default_scope=self._default_scope,
            _gitignore_scopes=(*self._gitignore_scopes, scope),
            _contextforge_scope=self._contextforge_scope,
            _include_gitignore=self._include_gitignore,
        )


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

    default_rules = [
        IgnoreMatch("default", pattern) for pattern in DEFAULT_IGNORE_PATTERNS
    ]
    gitignore_rules: list[IgnoreMatch] = []
    if include_gitignore:
        gitignore_rules.extend(
            IgnoreMatch("gitignore", pattern)
            for pattern in _read_ignore_file(resolved_root / ".gitignore")
        )
    contextforge_rules: list[IgnoreMatch] = []
    if include_contextforgeignore:
        contextforge_rules.extend(
            IgnoreMatch("contextforgeignore", pattern)
            for pattern in _read_ignore_file(resolved_root / ".contextforgeignore")
        )

    protected_rules = tuple(
        IgnoreMatch("protected", pattern) for pattern in PROTECTED_IGNORE_PATTERNS
    )

    try:
        default_scope = _compile_scope("", default_rules)
        gitignore_scope = _compile_scope("", gitignore_rules)
        contextforge_scope = _compile_scope("", contextforge_rules)
    except Exception as exc:
        raise IgnoreRulesError(f"invalid ignore pattern for root: {root}") from exc
    return IgnoreRules(
        root=resolved_root,
        _protected_matches=protected_rules,
        _default_scope=default_scope,
        _gitignore_scopes=(gitignore_scope,),
        _contextforge_scope=contextforge_scope,
        _include_gitignore=include_gitignore,
    )


def _compile_scope(
    base: str,
    rules: list[IgnoreMatch] | tuple[IgnoreMatch, ...],
) -> _RuleScope:
    nonempty_rules = tuple(rule for rule in rules if rule.pattern)
    spec = GitIgnoreSpec.from_lines(rule.pattern for rule in nonempty_rules)
    return _RuleScope(base=base, spec=spec, matches=nonempty_rules)


def _match_ordinary(
    scopes: tuple[_RuleScope, ...], candidate: str
) -> IgnoreMatch | None:
    match: IgnoreMatch | None = None
    ignored = False
    for scope in scopes:
        scoped_candidate = _scoped_candidate(scope.base, candidate)
        if scoped_candidate is None:
            continue
        result = scope.spec.check_file(scoped_candidate)
        if result.include is None or result.index is None:
            continue
        ignored = result.include
        match = scope.matches[result.index]
    return match if ignored else None


def _scoped_candidate(base: str, candidate: str) -> str | None:
    if not base:
        return candidate
    prefix = f"{base}/"
    if not candidate.startswith(prefix):
        return None
    return candidate[len(prefix) :]


def _match_protected(
    rules: tuple[IgnoreMatch, ...], candidate: str
) -> IgnoreMatch | None:
    normalized_candidate = candidate.rstrip("/").casefold()
    path_parts = normalized_candidate.split("/")
    for rule in rules:
        protected_name = rule.pattern.rstrip("/").casefold()
        if "/" in protected_name:
            if (
                normalized_candidate == protected_name
                or normalized_candidate.startswith(f"{protected_name}/")
            ):
                return rule
        elif protected_name in path_parts:
            return rule
    return None


def _read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise IgnoreRulesError(f"unable to read ignore file: {path.name}") from exc
