"""Read-only validation of the historical v1 index envelope."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def load_legacy_envelope[Model: BaseModel](
    payload: dict[str, Any],
    model: type[Model],
) -> Model:
    """Preserve v1 values and digests; never migrate by filling v2 identities."""
    if payload.get("schema_version") != 1:
        raise ValueError("not a legacy index envelope")
    versions = payload.get("schema_versions")
    if versions is not None and versions != {
        "index_schema_version": 1,
        "manifest_schema_version": 1,
        "record_schema_version": 1,
    }:
        raise ValueError("mixed legacy schema versions")
    return model.model_validate(payload)
