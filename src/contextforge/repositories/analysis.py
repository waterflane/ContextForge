"""Repository analysis boundary for future adapters."""

from pathlib import Path
from typing import Protocol


class RepositoryAnalyzer(Protocol):
    """Protocol for future repository analyzers."""

    def analyze(self, root: Path) -> None:
        """Analyze a repository root."""
        raise NotImplementedError
