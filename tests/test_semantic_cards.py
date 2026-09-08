import asyncio
import json
from pathlib import Path

from contextforge.application import build_repository_index
from contextforge.intelligence import load_semantic_card
from contextforge.models import FakeModelProvider, ProviderConfiguration


def _provider(responder: object) -> FakeModelProvider:
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="http://127.0.0.1:1",
        model_id="semantic-card-test",
        retry_limit=0,
        max_json_repair_attempts=0,
    )
    return FakeModelProvider(configuration, responder=responder)  # type: ignore[arg-type]


def _response(
    *, synopsis_evidence: str = "file", invalid_optional: bool = False
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "synopsis": {
                "text": "Handles repository requests.",
                "evidence_ids": [synopsis_evidence],
            },
            "concepts": [{"text": "request handling", "evidence_ids": ["symbol:0000"]}],
            "responsibilities": [
                {
                    "text": "Invalid optional claim",
                    "evidence_ids": ["unknown"] if invalid_optional else ["file"],
                }
            ],
            "key_symbols": [
                {"evidence_id": "symbol:0000", "summary": "Public handler"}
            ],
            "side_effects": [],
            "profile_facts": {},
        }
    )


def test_semantic_card_keeps_grounded_items_and_drops_bad_optional_claim(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    provider = _provider(lambda request, call: _response(invalid_optional=True))

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)

    assert card.quality == "partial"
    assert card.synopsis.text == "Handles repository requests."
    assert card.responsibilities == ()
    assert card.key_symbols[0].name == "handle"
    assert "Invalid optional claim" not in card.ranking_text()
    assert provider.call_count == 1


def test_invalid_required_grounding_gets_exactly_one_repair(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def handle() -> None:\n    pass\n", encoding="utf-8"
    )
    provider = _provider(
        lambda request, call: _response(
            synopsis_evidence="unknown" if call == 0 else "file"
        )
    )

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)

    assert provider.call_count == 2
    assert card.quality == "complete"
    assert card.provenance.repair_attempted is True


def test_content_cache_rebinds_unchanged_source_after_rename(tmp_path: Path) -> None:
    source = "def handle() -> None:\n    pass\n"
    (tmp_path / "old.py").write_text(source, encoding="utf-8")
    provider = _provider(lambda request, call: _response())
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    (tmp_path / "old.py").rename(tmp_path / "new.py")

    updated = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )
    card = load_semantic_card(tmp_path, "new.py", manifest=updated.manifest)

    assert provider.call_count == 1
    assert updated.semantic is not None
    assert updated.semantic.cache_hits == 1  # type: ignore[union-attr]
    assert card.path == "new.py"
    assert card.provenance.cache_hit is True
    assert all(item.path == "new.py" for item in card.evidence)


def test_semantic_cards_select_all_four_profiles(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "settings.toml").write_text("port = 8000\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_module.py").write_text(
        "def test_value():\n    pass\n", encoding="utf-8"
    )

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    profiles = {
        path: load_semantic_card(tmp_path, path, manifest=report.manifest).profile
        for path in (
            "README.md",
            "module.py",
            "settings.toml",
            "tests/test_module.py",
        )
    }

    assert profiles == {
        "README.md": "documentation",
        "module.py": "code",
        "settings.toml": "config",
        "tests/test_module.py": "test",
    }
