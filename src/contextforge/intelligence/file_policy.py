"""Declarative file classification shared by semantics and context selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from contextforge.intelligence.codemap import FileCodeMap, SymbolKind

SemanticProfileName = Literal["code", "documentation", "config", "test"]
CandidateRole = Literal["source", "documentation", "config", "test"]


@dataclass(frozen=True, slots=True)
class FilePolicyRule:
    """One ordered path rule with semantic and compiler classifications."""

    profile: SemanticProfileName
    role: CandidateRole
    suffixes: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    path_parts: frozenset[str] = frozenset()
    name_prefixes: tuple[str, ...] = ()
    name_suffixes: tuple[str, ...] = ()
    name_fragments: tuple[str, ...] = ()

    def matches(self, path: PurePosixPath) -> bool:
        parts = {part.casefold() for part in path.parts}
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        return bool(
            suffix in self.suffixes
            or name in self.names
            or parts.intersection(self.path_parts)
            or name.startswith(self.name_prefixes)
            or name.endswith(self.name_suffixes)
            or any(fragment in name for fragment in self.name_fragments)
        )


@dataclass(frozen=True, slots=True)
class TestNamingRule:
    """Declarative conversion from a test filename to a source filename."""

    marker: str
    source_suffix: str
    position: Literal["prefix", "suffix", "infix"]

    def source_name(self, name: str) -> str | None:
        folded = name.casefold()
        if self.position == "prefix" and folded.startswith(self.marker):
            return name[len(self.marker) :]
        if self.position == "suffix" and folded.endswith(self.marker):
            return name[: -len(self.marker)] + self.source_suffix
        if self.position == "infix" and self.marker in folded:
            index = folded.rfind(self.marker)
            return name[:index] + self.source_suffix + name[index + len(self.marker) :]
        return None


class FilePolicyRegistry:
    """Apply ordered declarative path rules and structural deterministic policy."""

    def __init__(
        self,
        rules: tuple[FilePolicyRule, ...],
        test_naming_rules: tuple[TestNamingRule, ...] = (),
    ) -> None:
        self._rules = rules
        self._test_naming_rules = test_naming_rules

    def profile(self, path: str) -> SemanticProfileName:
        pure = PurePosixPath(path)
        return next(
            (rule.profile for rule in self._rules if rule.matches(pure)), "code"
        )

    def candidate_role(self, path: str) -> CandidateRole:
        pure = PurePosixPath(path)
        return next((rule.role for rule in self._rules if rule.matches(pure)), "source")

    def is_test(self, path: str) -> bool:
        """Return the shared test classification used by graph construction."""

        return self.candidate_role(path) == "test"

    def conventional_source_names(self, test_path: str) -> tuple[str, ...]:
        """Return plausible source basenames without looking outside a snapshot."""

        pure = PurePosixPath(test_path)
        if not self.is_test(test_path):
            return ()
        names = {pure.name}
        for rule in self._test_naming_rules:
            candidate = rule.source_name(pure.name)
            if candidate:
                names.add(candidate)
        return tuple(sorted(names, key=lambda value: (value.casefold(), value)))

    def is_structural_barrel(self, code_map: FileCodeMap) -> bool:
        """Recognize passive index/init files whose imports re-export sources."""

        path = PurePosixPath(code_map.path.casefold())
        if path.name not in {"__init__.py", "index.ts", "index.js"}:
            return False
        return self._is_behavioral_barrel(code_map)

    def requires_deterministic_card(self, code_map: FileCodeMap) -> bool:
        path = PurePosixPath(code_map.path.casefold())
        if path.name in {"__init__.py", "index.ts", "index.js"}:
            return self._is_behavioral_barrel(code_map)
        deterministic_part = any(
            part in {"generated", "dist", "vendor"} for part in path.parts
        )
        deterministic_name = path.name.endswith((".lock", ".min.js", ".map")) or (
            path.name in {".gitignore", ".gitattributes", "license", "license.md"}
        )
        simple_metadata = len(code_map.symbols) == 0 and code_map.line_count <= 8
        return bool(
            code_map.line_count == 0
            or deterministic_part
            or deterministic_name
            or simple_metadata
        )

    @staticmethod
    def _is_behavioral_barrel(code_map: FileCodeMap) -> bool:
        if code_map.module_has_executable_code:
            return False
        if any(
            symbol.kind
            in {
                SymbolKind.CLASS,
                SymbolKind.FUNCTION,
                SymbolKind.ASYNC_FUNCTION,
                SymbolKind.METHOD,
                SymbolKind.CONSTRUCTOR,
            }
            or symbol.direct_calls
            for symbol in code_map.symbols
        ):
            return False
        return all(
            symbol.name == "__all__"
            and symbol.kind in {SymbolKind.CONSTANT, SymbolKind.VARIABLE}
            for symbol in code_map.symbols
        )


FILE_POLICY_REGISTRY = FilePolicyRegistry(
    (
        FilePolicyRule(
            profile="test",
            role="test",
            path_parts=frozenset({"test", "tests", "__tests__", "spec", "specs"}),
            name_prefixes=("test_", "spec_"),
            name_suffixes=("_test.py", "test.kt", "tests.cs"),
            name_fragments=(".test.", ".spec."),
        ),
        FilePolicyRule(
            profile="documentation",
            role="documentation",
            suffixes=frozenset({".md", ".mdx", ".rst", ".adoc", ".txt"}),
            path_parts=frozenset({"docs"}),
        ),
        FilePolicyRule(
            profile="config",
            role="config",
            suffixes=frozenset(
                {".toml", ".yaml", ".yml", ".ini", ".cfg", ".json", ".env"}
            ),
            names=frozenset({".env", "dockerfile"}),
        ),
    ),
    test_naming_rules=(
        TestNamingRule(marker="test_", source_suffix="", position="prefix"),
        TestNamingRule(marker="spec_", source_suffix="", position="prefix"),
        TestNamingRule(marker="_test.py", source_suffix=".py", position="suffix"),
        TestNamingRule(marker="test.kt", source_suffix=".kt", position="suffix"),
        TestNamingRule(marker="tests.cs", source_suffix=".cs", position="suffix"),
        TestNamingRule(marker=".test.", source_suffix=".", position="infix"),
        TestNamingRule(marker=".spec.", source_suffix=".", position="infix"),
    ),
)


__all__ = [
    "CandidateRole",
    "FILE_POLICY_REGISTRY",
    "FilePolicyRegistry",
    "FilePolicyRule",
    "SemanticProfileName",
    "TestNamingRule",
]
