"""Git-style repository ignore-rule loading and matching.

Rules are combined in ascending precedence: built-in defaults, ``.gitignore``,
then ``.contextforgeignore``. Because Git-style matching uses the last matching
rule, later files may negate earlier exclusions. This lets a repository opt back
into a default or Git-ignored path explicitly while keeping ContextForge-specific
rules authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from contextforge.repositories.files import normalize_relative_path

DEFAULT_IGNORE_PATTERNS = (
    ".git/",
    ".hg/",
    ".svn/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    "build/",
    "dist/",
    "*.egg-info/",
    "*.py[cod]",
)


class IgnoreRulesError(ValueError):
    """Raised when an ignore file cannot be read or parsed."""


@dataclass(frozen=True, slots=True)
class IgnoreRules:
    """Compiled Git-style ignore rules for one repository root."""

    root: Path
    _spec: GitIgnoreSpec

    def is_ignored(self, path: str | Path, *, is_directory: bool = False) -> bool:
        """Return whether a repository-relative path matches the active rules."""

        candidate = normalize_relative_path(path)
        if is_directory:
            candidate = f"{candidate}/"
        return self._spec.match_file(candidate)


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

    patterns = list(DEFAULT_IGNORE_PATTERNS)
    if include_gitignore:
        patterns.extend(_read_ignore_file(resolved_root / ".gitignore"))
    if include_contextforgeignore:
        patterns.extend(_read_ignore_file(resolved_root / ".contextforgeignore"))

    try:
        spec = GitIgnoreSpec.from_lines(patterns)
    except Exception as exc:
        raise IgnoreRulesError(f"invalid ignore pattern for root: {root}") from exc
    return IgnoreRules(root=resolved_root, _spec=spec)


def _read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise IgnoreRulesError(f"unable to read ignore file: {path.name}") from exc
