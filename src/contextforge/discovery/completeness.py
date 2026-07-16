"""Advisory missing-context and low-confidence review for discovery selections."""

from __future__ import annotations

from collections import defaultdict

from .models import CompletenessWarning, DiscoveryCandidate
from .tools import DiscoveryKnowledge, GitDiffResult


def review_completeness(
    knowledge: DiscoveryKnowledge,
    selected: tuple[DiscoveryCandidate, ...],
    *,
    git_diff: GitDiffResult | None = None,
    source_was_read: bool,
) -> tuple[CompletenessWarning, ...]:
    """Return bounded advisory checks without claiming graph completeness."""

    selected_paths = {item.path for item in selected if item.path is not None}
    warnings: list[CompletenessWarning] = []
    imports: dict[str, set[str]] = defaultdict(set)
    importers: dict[str, set[str]] = defaultdict(set)
    callers: dict[str, set[str]] = defaultdict(set)
    tests: dict[str, set[str]] = defaultdict(set)

    for source_path, code_map in knowledge.code_maps.items():
        for item in code_map.imports:
            if item.target_file_path is not None:
                imports[source_path].add(item.target_file_path)
                importers[item.target_file_path].add(source_path)
        for symbol in code_map.symbols:
            for call in symbol.direct_calls:
                if call.target_file_path is not None:
                    callers[call.target_file_path].add(source_path)
        for relationship in code_map.relationships:
            target = relationship.target.file_path
            if target is None:
                continue
            if relationship.kind in {"tests", "tested_by"}:
                tests[source_path].add(target)
                tests[target].add(source_path)

    if knowledge.overview is not None:
        for test_relationship in knowledge.overview.test_relationships:
            tests[test_relationship.source_file].add(test_relationship.test_file)
            tests[test_relationship.test_file].add(test_relationship.source_file)

    for path in sorted(selected_paths):
        _warn_related(
            warnings,
            "direct-import-omitted",
            path,
            imports[path] - selected_paths,
            "Selected code directly imports files that are not selected.",
        )
        _warn_related(
            warnings,
            "direct-importer-omitted",
            path,
            importers[path] - selected_paths,
            "Direct importers of selected code are not selected.",
        )
        _warn_related(
            warnings,
            "caller-omitted",
            path,
            callers[path] - selected_paths,
            "Statically resolved callers of selected code are not selected.",
        )
        related_tests = {
            candidate for candidate in tests[path] if _looks_like_test(candidate)
        }
        _warn_related(
            warnings,
            "related-test-omitted",
            path,
            related_tests - selected_paths,
            "Related tests exist but are not selected.",
        )

        selected_map = knowledge.code_maps.get(path)
        if selected_map is None:
            warnings.append(
                CompletenessWarning(
                    code="structural-record-unavailable",
                    path=path,
                    message="No current CodeMap was available for completeness review.",
                    confidence=0.2,
                )
            )
            continue
        if selected_map.parse_status != "parsed":
            warnings.append(
                CompletenessWarning(
                    code="incomplete-parse-data",
                    path=path,
                    message=(
                        "Structural parse data is incomplete; broaden review with "
                        "tree, "
                        "text search, direct reads, configuration, and tests."
                    ),
                    confidence=0.2,
                )
            )
        unresolved = sum(
            call.resolution == "unresolved"
            for symbol in selected_map.symbols
            for call in symbol.direct_calls
        )
        if unresolved:
            warnings.append(
                CompletenessWarning(
                    code="dynamic-or-unresolved-calls",
                    path=path,
                    message=(
                        f"{unresolved} observed calls are unresolved; dynamic "
                        "dispatch, "
                        "reflection, or rebinding may hide callers or dependencies."
                    ),
                    confidence=0.35,
                )
            )
        configuration_keys = sorted(
            {
                key
                for symbol in selected_map.symbols
                for key in symbol.configuration_keys
            }
        )
        if configuration_keys:
            consumers = {
                other_path
                for other_path, other_map in knowledge.code_maps.items()
                if other_path != path
                and any(
                    set(configuration_keys) & set(symbol.configuration_keys)
                    for symbol in other_map.symbols
                )
            }
            _warn_related(
                warnings,
                "configuration-consumer-omitted",
                path,
                consumers - selected_paths,
                "Other files consume the same statically observed configuration keys.",
            )
        analysis = knowledge.semantic_analyses.get(path)
        if analysis is not None and analysis.uncertainty:
            confidence = min(item.confidence.value for item in analysis.uncertainty)
            warnings.append(
                CompletenessWarning(
                    code="semantic-uncertainty",
                    path=path,
                    message=(
                        "Indexed semantic analysis reports uncertainty; verify source "
                        "and broaden beyond semantic candidates."
                    ),
                    confidence=confidence,
                )
            )

    architecture = knowledge.architecture
    if architecture is not None:
        for entry in architecture.entry_points:
            if (
                entry.handler_file in selected_paths
                and entry.file not in selected_paths
            ):
                _warn_related(
                    warnings,
                    "public-entry-point-omitted",
                    entry.handler_file,
                    {entry.file},
                    "A mapped public entry point for selected code is not selected.",
                )

    if git_diff is not None:
        omitted = set(git_diff.touched_paths) - selected_paths
        _warn_related(
            warnings,
            "diff-file-omitted",
            None,
            omitted,
            "Files touched by the relevant Git diff are not selected.",
        )
        public_changes = any(
            path in selected_paths and path.endswith((".py", ".pyi"))
            for path in git_diff.touched_paths
        )
        has_docs = any(
            path.lower().endswith((".md", ".rst")) or path.lower().startswith("docs/")
            for path in selected_paths
        )
        if public_changes and not has_docs:
            warnings.append(
                CompletenessWarning(
                    code="documentation-not-selected",
                    message=(
                        "The diff touches selected code but no documentation is "
                        "selected; "
                        "review docs when public behavior changes."
                    ),
                    confidence=0.5,
                )
            )

    if knowledge.stale_index_paths:
        warnings.append(
            CompletenessWarning(
                code="stale-index-coverage",
                message=(
                    "The pinned index has stale or missing paths; current source tools "
                    "remain available for every allowed snapshot file."
                ),
                related_paths=knowledge.stale_index_paths,
                confidence=0.3,
            )
        )
    if knowledge.overview is not None and knowledge.overview.diagnostics:
        warnings.append(
            CompletenessWarning(
                code="structural-coverage-limitations",
                message=(
                    "Repository structural diagnostics report incomplete parsing or "
                    "unsupported files; completeness is advisory only."
                ),
                confidence=0.35,
            )
        )
    if knowledge.mode.value == "indexed" and not source_was_read:
        warnings.append(
            CompletenessWarning(
                code="indexed-source-not-read",
                message=(
                    "Indexed discovery must verify selected current source before "
                    "finalization."
                ),
                confidence=0.1,
            )
        )

    return _canonical_warnings(warnings)


def _warn_related(
    warnings: list[CompletenessWarning],
    code: str,
    path: str | None,
    related: set[str],
    message: str,
) -> None:
    if related:
        warnings.append(
            CompletenessWarning(
                code=code,
                path=path,
                related_paths=tuple(sorted(related))[:50],
                message=message,
                confidence=0.6,
            )
        )


def _looks_like_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (
        path.lower().startswith(("test/", "tests/"))
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))
    )


def _canonical_warnings(
    warnings: list[CompletenessWarning],
) -> tuple[CompletenessWarning, ...]:
    unique: dict[tuple[str, str | None, tuple[str, ...]], CompletenessWarning] = {}
    for warning in warnings:
        unique[(warning.code, warning.path, warning.related_paths)] = warning
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[0], item[1] or "", item[2]),
        )
    )


__all__ = ["review_completeness"]
