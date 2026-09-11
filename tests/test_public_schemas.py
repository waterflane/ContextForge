import json
from pathlib import Path
from typing import Any, cast

import pytest

from contextforge.context import ContextCapsule
from contextforge.intelligence import (
    OrientationMap,
    RepositoryMap,
    RetrievalResult,
    SemanticCard,
)

SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "docs" / "schemas"


def _schema(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8")),
    )


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("semantic-card-v3.schema.json", 3),
        ("orientation-map-v3.schema.json", 3),
        ("repository-map-v3.schema.json", 3),
        ("retrieval-result-v3.schema.json", 3),
        ("context-capsule-v2.schema.json", 2),
    ],
)
def test_public_artifact_schemas_are_closed_and_versioned(
    name: str, version: int
) -> None:
    schema = _schema(name)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {"const": version}
    assert "schema_version" in schema["required"]


def test_capsule_and_retrieval_schemas_expose_representation_contract() -> None:
    capsule = _schema("context-capsule-v2.schema.json")
    retrieval = _schema("retrieval-result-v3.schema.json")

    assert capsule["$defs"]["material"]["properties"]["representation"]["enum"] == [
        "map",
        "summary",
        "slice",
        "full",
    ]
    candidate = retrieval["$defs"]["candidate"]
    assert candidate["additionalProperties"] is False
    assert candidate["properties"]["freshness"] == {"const": "current"}
    assert candidate["properties"]["estimated_cost"] == {"$ref": "#/$defs/costs"}


def test_semantic_card_schema_requires_grounding_and_sparse_symbols() -> None:
    semantic = _schema("semantic-card-v3.schema.json")

    assert semantic["properties"]["concepts"]["minItems"] == 1
    assert semantic["properties"]["key_symbols"]["maxItems"] == 12
    assert semantic["$defs"]["claim"]["properties"]["evidence_ids"]["minItems"] == 1
    assert semantic["$defs"]["diagnostic"]["properties"]["dropped_items"] == {
        "type": "integer",
        "minimum": 0,
    }


def test_repository_map_schema_retains_claim_and_relationship_provenance() -> None:
    repository_map = _schema("repository-map-v3.schema.json")

    assert repository_map["$defs"]["claim"]["properties"]["provenance"]["enum"] == [
        "verified",
        "best-effort-structural",
        "model-inferred",
        "grounded-semantic-card",
    ]
    assert repository_map["$defs"]["relationship"]["properties"]["provenance"][
        "enum"
    ] == ["verified", "best-effort-structural", "model-inferred"]


def test_bridge_21_map_result_advertises_all_pinned_map_schemas() -> None:
    bridge = _schema("contextforge-bridge-v2.1.schema.json")
    result = bridge["$defs"]["mapSuccess"]["properties"]["result"]

    assert result["properties"]["orientation"] == {
        "$ref": "orientation-map-v3.schema.json"
    }
    assert set(result["properties"]["repository_maps"]["properties"]) == {
        "architecture",
        "conventions",
        "features",
    }


def test_public_artifact_schema_fields_match_runtime_models() -> None:
    pairs = (
        (SemanticCard, _schema("semantic-card-v3.schema.json")),
        (OrientationMap, _schema("orientation-map-v3.schema.json")),
        (RepositoryMap, _schema("repository-map-v3.schema.json")),
        (RetrievalResult, _schema("retrieval-result-v3.schema.json")),
        (ContextCapsule, _schema("context-capsule-v2.schema.json")),
    )

    for model, schema in pairs:
        assert set(schema["properties"]) == set(model.model_fields)
