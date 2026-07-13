"""Storage boundary for future persistence adapters."""

from typing import Protocol


class StorageBackend(Protocol):
    """Protocol for future storage backends."""

    def connect(self) -> None:
        """Connect to a storage backend."""
        raise NotImplementedError
