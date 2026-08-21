"""Conservative file constraints extracted from explicit task instructions."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

_NEGATIVE_INSTRUCTION = re.compile(
    r"(?:\b(?:do|must)\s+not\s+(?:select|include)\b|"
    r"\bdo\s+not\s+expand\s+into\b|"
    r"\bdo\s+not\s+spend\s+context\s+on\b|"
    r"\bdon't\s+(?:select|include)\b|"
    r"\b(?:exclude|without|ignore)\b)",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(
    r";|\n|\.(?=\s|$)|\bbut\b|"
    r",(?=\s*(?:find|fix|include|inspect|select|use)\b)|"
    r"\band\s+(?=(?:find|fix|include|inspect|select|use)\b)",
    re.IGNORECASE,
)
_REFERENCE_TOKEN = re.compile(r"[A-Za-z0-9_.?*/\\[\]-]+")
_FILE_HINTS = {
    "extension",
    "extensions",
    "file",
    "files",
    "helper",
    "helpers",
    "path",
    "paths",
    "script",
    "scripts",
}
_TARGET_STOP_WORDS = _FILE_HINTS | {
    "all",
    "and",
    "any",
    "do",
    "from",
    "include",
    "including",
    "context",
    "not",
    "or",
    "select",
    "selecting",
    "selection",
    "spend",
    "the",
    "using",
}
_NON_SELECTION_WITHOUT = {
    "change",
    "changing",
    "edit",
    "editing",
    "modify",
    "modifying",
    "write",
    "writing",
}


@dataclass(frozen=True, slots=True)
class TaskFileConstraints:
    """Fallback-only task text and explicit negative file references."""

    positive_task: str
    excluded_references: tuple[str, ...] = ()
    excluded_terms: frozenset[str] = frozenset()
    excluded_term_groups: tuple[frozenset[str], ...] = ()

    def excludes(self, path: str) -> bool:
        """Return whether a repository path matches an extracted negative target."""

        normalized = path.casefold().replace("\\", "/")
        basename = normalized.rsplit("/", 1)[-1]
        for reference in self.excluded_references:
            if reference.startswith(".") and "/" not in reference:
                if basename.endswith(reference):
                    return True
            elif any(marker in reference for marker in "*?["):
                if fnmatch.fnmatchcase(normalized, reference) or fnmatch.fnmatchcase(
                    basename, reference
                ):
                    return True
            elif "/" in reference:
                if normalized == reference or normalized.startswith(
                    reference.rstrip("/") + "/"
                ):
                    return True
            elif basename == reference:
                return True
        path_terms = _terms(normalized)
        groups = self.excluded_term_groups or (
            (self.excluded_terms,) if self.excluded_terms else ()
        )
        return any(group <= path_terms for group in groups)


def extract_task_file_constraints(task: str) -> TaskFileConstraints:
    """Extract only simple, explicit negative file-selection clauses.

    The extractor deliberately recognizes a small imperative vocabulary and stops
    each clause at sentence punctuation, a newline, or ``but``. Ambiguous uses of
    ``without`` and ``ignore`` require a path-like reference or a file noun.
    """

    excluded_references: set[str] = set()
    excluded_terms: set[str] = set()
    excluded_term_groups: set[frozenset[str]] = set()
    removed_spans: list[tuple[int, int]] = []
    cursor = 0
    while match := _NEGATIVE_INSTRUCTION.search(task, cursor):
        tail_start = match.end()
        end_match = _CLAUSE_END.search(task, tail_start)
        tail_end = len(task) if end_match is None else end_match.start()
        tail = task[tail_start:tail_end]
        references = _path_references(tail)
        tail_terms = _terms(tail)
        for reference in references:
            tail_terms.difference_update(_terms(reference))
        marker = " ".join(match.group(0).casefold().split())
        file_directed = (
            marker.startswith(("do not ", "must not ", "don't "))
            or marker == "exclude"
            or bool(references)
            or bool(tail_terms & _FILE_HINTS)
        )
        if marker == "without" and tail_terms & _NON_SELECTION_WITHOUT:
            file_directed = False
        if file_directed:
            excluded_references.update(references)
            for target in re.split(r"\s*(?:,|\bor\b)\s*", tail):
                target_terms = _terms(target)
                for reference in _path_references(target):
                    target_terms.difference_update(_terms(reference))
                group = frozenset(target_terms - _TARGET_STOP_WORDS)
                if {"server", "startup"} <= group:
                    group = group - {"startup"}
                if {"library", "page", "video"} <= group:
                    group = group - {"library", "page", "separate"}
                if group:
                    excluded_term_groups.add(group)
                    excluded_terms.update(group)
            removed_spans.append((match.start(), tail_end))
        cursor = max(tail_end, match.end())

    positive_task = task
    for start, end in reversed(removed_spans):
        positive_task = positive_task[:start] + " " + positive_task[end:]
    positive_task = " ".join(positive_task.split()).strip(" ,;.")
    return TaskFileConstraints(
        positive_task=positive_task,
        excluded_references=tuple(sorted(excluded_references)),
        excluded_terms=frozenset(excluded_terms),
        excluded_term_groups=tuple(
            sorted(excluded_term_groups, key=lambda item: tuple(sorted(item)))
        ),
    )


def _path_references(value: str) -> tuple[str, ...]:
    references: set[str] = set()
    for raw in _REFERENCE_TOKEN.findall(value):
        reference = raw.strip("'\"`(){}:,").casefold().replace("\\", "/")
        if not reference:
            continue
        if (
            "/" in reference
            or any(marker in reference for marker in "*?[")
            or reference.startswith(".")
            or "." in reference
        ):
            references.add(reference)
    return tuple(sorted(references))


def _terms(value: str) -> set[str]:
    aliases = {
        "callers": "caller",
        "docs": "documentation",
        "files": "file",
        "helpers": "helper",
        "readme": "documentation",
        "scripts": "script",
        "tests": "test",
        "videos": "video",
    }
    return {
        aliases.get(item, item)
        for item in re.findall(r"[a-z0-9]+", value.casefold().replace("_", " "))
        if len(item) >= 2
    }


__all__ = ["TaskFileConstraints", "extract_task_file_constraints"]
