"""Logging setup for ContextForge."""

import logging

from contextforge.config import LogLevel


def configure_logging(level: LogLevel = "INFO") -> None:
    """Configure standard library logging for local entry points."""

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
