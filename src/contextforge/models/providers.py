"""Model-provider boundary for future integrations."""

from typing import Protocol


class ModelProvider(Protocol):
    """Protocol for future model providers."""

    @property
    def name(self) -> str:
        """Provider display name."""
        raise NotImplementedError
