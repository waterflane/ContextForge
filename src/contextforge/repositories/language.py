"""Explicit filename and extension based language detection."""

from __future__ import annotations

from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final

LANGUAGES_BY_FILENAME: Final = MappingProxyType(
    {
        ".dockerignore": "Dockerfile",
        ".editorconfig": "EditorConfig",
        ".env": "Dotenv",
        ".env.example": "Dotenv template",
        ".env.sample": "Dotenv template",
        ".gitattributes": "Git attributes",
        ".gitignore": "Git ignore",
        "dockerfile": "Dockerfile",
        "gemfile": "Ruby",
        "makefile": "Makefile",
        "rakefile": "Ruby",
        "readme": "Markdown",
        "license": "License",
    }
)

LANGUAGES_BY_EXTENSION: Final = MappingProxyType(
    {
        ".c": "C",
        ".cc": "C++",
        ".cfg": "Configuration",
        ".cpp": "C++",
        ".cs": "C#",
        ".css": "CSS",
        ".cxx": "C++",
        ".bat": "Batch",
        ".cmd": "Batch",
        ".go": "Go",
        ".h": "C",
        ".hpp": "C++",
        ".htm": "HTML",
        ".html": "HTML",
        ".ini": "Configuration",
        ".java": "Java",
        ".js": "JavaScript",
        ".json": "JSON",
        ".jsx": "JavaScript",
        ".kt": "Kotlin",
        ".kts": "Kotlin",
        ".md": "Markdown",
        ".php": "PHP",
        ".ps1": "PowerShell",
        ".py": "Python",
        ".rb": "Ruby",
        ".rs": "Rust",
        ".scss": "SCSS",
        ".sh": "Shell",
        ".sql": "SQL",
        ".swift": "Swift",
        ".toml": "TOML",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".txt": "Text",
        ".xml": "XML",
        ".yaml": "YAML",
        ".yml": "YAML",
    }
)


def detect_language(path: str | PurePosixPath) -> str | None:
    """Detect a common programming or configuration language for ``path``."""

    filename = str(path).replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    normalized_filename = filename.casefold()
    if language := LANGUAGES_BY_FILENAME.get(normalized_filename):
        return language

    extension = PurePosixPath(normalized_filename).suffix
    return LANGUAGES_BY_EXTENSION.get(extension)
