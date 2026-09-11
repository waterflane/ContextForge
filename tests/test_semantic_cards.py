import asyncio
import json
import re
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.intelligence import (
    SemanticCardOptions,
    acquire_index_lock,
    build_relationship_graph,
    extract_code_maps,
    load_relationship_graph,
    load_repository_map_v3,
    load_semantic_card,
)
from contextforge.intelligence import cards as cards_module
from contextforge.models import FakeModelProvider, ProviderConfiguration
from contextforge.repositories import scan_repository


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
                    "text": (
                        "Invalid optional claim"
                        if invalid_optional
                        else "Handles repository requests"
                    ),
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


def test_repository_maps_project_grounded_claims_and_enriched_graph(
    tmp_path: Path,
) -> None:
    (tmp_path / "handler.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "from handler import handle\n\n"
        "def main(request: str) -> str:\n    return handle(request)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_handler.py").write_text(
        "from handler import handle\n\n"
        "def test_handle():\n    assert handle('ok') == 'ok'\n",
        encoding="utf-8",
    )
    provider = _provider(lambda request, call: _response())

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    architecture = load_repository_map_v3(
        tmp_path, "architecture", manifest=report.manifest
    )
    features = load_repository_map_v3(tmp_path, "features", manifest=report.manifest)

    relationships = [
        item for entry in architecture.entries for item in entry.relationships
    ]
    assert {item.kind for item in relationships} >= {
        "import",
        "call",
        "source-test",
        "entrypoint-handler",
    }
    assert {item.provenance for item in relationships} >= {
        "verified",
        "best-effort-structural",
    }
    assert any(
        claim.text == "request handling"
        and claim.provenance == "grounded-semantic-card"
        for entry in architecture.entries
        for claim in entry.claims
    )
    assert any(entry.claims for entry in features.entries)


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


def test_unrelated_rename_reuses_uncached_fallback_card(tmp_path: Path) -> None:
    (tmp_path / "failed.py").write_text(
        "def failed() -> None:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "old.py").write_text(
        "def handle() -> None:\n    pass\n", encoding="utf-8"
    )

    def respond(request: object, call: int) -> str:
        del call
        path = request.trusted_code_map_facts["path"]  # type: ignore[attr-defined]
        return _response(synopsis_evidence="unknown" if path == "failed.py" else "file")

    provider = _provider(respond)
    initial = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    initial_calls = provider.call_count
    assert initial_calls == 3
    initial_failed = load_semantic_card(
        tmp_path, "failed.py", manifest=initial.manifest
    )
    assert initial_failed.provenance.method == "deterministic-fallback"

    (tmp_path / "old.py").rename(tmp_path / "new.py")
    updated = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )

    reused_failed = load_semantic_card(tmp_path, "failed.py", manifest=updated.manifest)
    assert provider.call_count == initial_calls
    assert updated.semantic is not None
    assert "failed.py" in updated.semantic.reused_paths
    assert reused_failed.provenance.method == "deterministic-fallback"


def test_previous_card_reuse_ignores_unavailable_or_ineligible_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle() -> None:\n    pass\n", encoding="utf-8"
    )
    provider = _provider(lambda request, call: _response())
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    structural = report.structural.manifest

    with acquire_index_lock(tmp_path, "previous-card-coverage") as lock:
        unavailable = structural.model_copy(
            update={
                "build": structural.build.model_copy(
                    update={"previous_generation_id": "0" * 64}
                )
            }
        )
        assert (
            cards_module._previous_reusable_cards(
                lock,
                unavailable,
                provider,
                SemanticCardOptions(),
            )
            == {}
        )


def test_whole_generation_reuse_rejects_incompatible_semantic_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle() -> None:\n    pass\n", encoding="utf-8"
    )
    provider = _provider(lambda request, call: _response())
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    maps = {item.path: item for item in report.structural.code_maps}
    structural = report.structural.manifest
    state = report.manifest.files[0]

    with acquire_index_lock(tmp_path, "whole-card-coverage") as lock:
        skipped = report.manifest.model_copy(
            update={
                "files": (
                    state.model_copy(
                        update={
                            "semantic_status": "skipped",
                            "interpretation_record_location": None,
                            "interpretation_record_sha256": None,
                        }
                    ),
                )
            }
        )
        assert (
            cards_module._reusable_cards(
                lock,
                skipped,
                provider,
                SemanticCardOptions(),
                maps,
                {"app.py"},
            )
            is None
        )
        assert (
            cards_module._reusable_cards(
                lock,
                skipped,
                provider,
                SemanticCardOptions(),
                maps,
                set(),
            )
            == ()
        )

        disabled = report.manifest.model_copy(
            update={
                "files": (
                    state.model_copy(
                        update={
                            "semantic_status": "disabled",
                            "interpretation_record_location": None,
                            "interpretation_record_sha256": None,
                        }
                    ),
                )
            }
        )
        assert (
            cards_module._reusable_cards(
                lock,
                disabled,
                provider,
                SemanticCardOptions(),
                maps,
                set(),
            )
            is None
        )

        def invalid_card(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise ValueError("corrupt card")

        monkeypatch.setattr(cards_module, "load_semantic_card", invalid_card)
        assert (
            cards_module._reusable_cards(
                lock,
                report.manifest,
                provider,
                SemanticCardOptions(),
                maps,
                set(),
            )
            is None
        )

        card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)
        stale = card.model_copy(
            update={
                "provenance": card.provenance.model_copy(
                    update={
                        "analyzer": card.provenance.analyzer.model_copy(
                            update={"analyzer_version": "stale"}
                        )
                    }
                )
            }
        )
        monkeypatch.setattr(
            cards_module, "load_semantic_card", lambda *args, **kwargs: stale
        )
        assert (
            cards_module._reusable_cards(
                lock,
                report.manifest,
                provider,
                SemanticCardOptions(),
                maps,
                set(),
            )
            is None
        )

        deterministic = stale.model_copy(
            update={
                "provenance": stale.provenance.model_copy(
                    update={"method": "deterministic-policy"}
                )
            }
        )
        monkeypatch.setattr(
            cards_module,
            "load_semantic_card",
            lambda *args, **kwargs: deterministic,
        )
        assert (
            cards_module._reusable_cards(
                lock,
                report.manifest,
                provider,
                SemanticCardOptions(),
                maps,
                set(),
            )
            is None
        )

        structural_predecessor = structural.model_copy(
            update={
                "build": structural.build.model_copy(
                    update={"previous_generation_id": structural.generation_id}
                )
            }
        )
        assert (
            cards_module._previous_reusable_cards(
                lock,
                structural_predecessor,
                provider,
                SemanticCardOptions(),
            )
            == {}
        )

        enriched_predecessor = structural.model_copy(
            update={
                "build": structural.build.model_copy(
                    update={"previous_generation_id": report.manifest.generation_id}
                )
            }
        )
        assert (
            cards_module._previous_reusable_cards(
                lock,
                enriched_predecessor,
                provider,
                SemanticCardOptions(scope="none"),
            )
            == {}
        )

        missing_current_file = enriched_predecessor.model_copy(update={"files": ()})
        assert (
            cards_module._previous_reusable_cards(
                lock,
                missing_current_file,
                provider,
                SemanticCardOptions(),
            )
            == {}
        )

        monkeypatch.setattr(
            cards_module,
            "load_semantic_card",
            lambda *args, **kwargs: deterministic,
        )
        assert (
            cards_module._previous_reusable_cards(
                lock,
                enriched_predecessor,
                provider,
                SemanticCardOptions(),
            )
            == {}
        )

        def invalid_card(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise ValueError("corrupt card")

        monkeypatch.setattr(cards_module, "load_semantic_card", invalid_card)
        assert (
            cards_module._previous_reusable_cards(
                lock,
                enriched_predecessor,
                provider,
                SemanticCardOptions(),
            )
            == {}
        )


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


def test_unchanged_enriched_generation_reuses_cards_without_provider_call(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    provider = _provider(lambda request, call: _response())
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )

    updated = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )

    assert provider.call_count == 1
    assert updated.semantic is not None
    assert updated.semantic.reused_paths == ("app.py",)  # type: ignore[union-attr]


def test_invalid_grounded_text_falls_back_after_grounding_validation(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    provider = _provider(
        lambda request, call: json.dumps(
            {
                "schema_version": 1,
                "synopsis": {"text": "   ", "evidence_ids": ["file"]},
                "concepts": [{"text": "concept", "evidence_ids": ["file"]}],
                "responsibilities": [],
                "key_symbols": [],
                "side_effects": [],
                "profile_facts": {},
            }
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

    assert report.semantic is not None
    assert report.semantic.failed_paths == ("app.py",)  # type: ignore[union-attr]
    assert card.provenance.method == "deterministic-fallback"


def test_corrupt_semantic_cache_is_ignored_and_rebuilt(tmp_path: Path) -> None:
    source = "def handle(request: str) -> str:\n    return request\n"
    (tmp_path / "old.py").write_text(source, encoding="utf-8")
    provider = _provider(lambda request, call: _response())
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    cache_file = next(
        (tmp_path / ".contextforge" / "index" / "cache" / "semantic").rglob("*.json")
    )
    cache_file.write_text("not-json", encoding="utf-8")
    (tmp_path / "old.py").rename(tmp_path / "new.py")

    updated = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )

    assert provider.call_count == 2
    assert updated.semantic is not None
    assert updated.semantic.cache_hits == 0  # type: ignore[union-attr]


def test_loading_unknown_semantic_card_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )

    try:
        load_semantic_card(tmp_path, "missing.py", manifest=report.manifest)
    except ValueError as exc:
        assert "absent" in str(exc)
    else:
        raise AssertionError("missing semantic card was accepted")


def test_profile_facts_survive_independent_optional_filtering(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    provider = _provider(
        lambda request, call: json.dumps(
            {
                "schema_version": 1,
                "synopsis": {"text": "Handles requests.", "evidence_ids": ["file"]},
                "concepts": [
                    {"text": "request handling", "evidence_ids": ["symbol:0000"]}
                ],
                "responsibilities": [],
                "key_symbols": [
                    {"evidence_id": "unknown", "summary": "Invented symbol"}
                ],
                "side_effects": [],
                "profile_facts": {
                    "apis": [
                        {"text": "Public handler API", "evidence_ids": ["symbol:0000"]},
                        {"text": "Ungrounded API", "evidence_ids": ["unknown"]},
                    ]
                },
            }
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

    assert card.quality == "partial"
    assert [item.text for item in card.profile_facts["apis"]] == ["Public handler API"]
    assert card.key_symbols == ()


def test_lexically_unanchored_optional_claim_is_not_ranking_text(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    payload = json.loads(_response())
    payload["side_effects"] = [
        {"text": "quantum banana teleportation", "evidence_ids": ["file"]}
    ]
    provider = _provider(lambda request, call: json.dumps(payload))

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)

    assert card.quality == "partial"
    assert card.side_effects == ()
    assert "quantum" not in card.ranking_text()


def test_semantic_card_chunks_large_utf8_source_and_scopes_evidence(
    tmp_path: Path,
) -> None:
    source = "\n".join(
        f"def function_{index}(value: str) -> str:\n    return value + 'λ'"
        for index in range(160)
    )
    (tmp_path / "large.py").write_text(source + "\n", encoding="utf-8")
    observed: list[tuple[int, int]] = []

    def respond(request: object, call: int) -> str:
        del call
        trusted = request.trusted_code_map_facts  # type: ignore[attr-defined]
        chunk_range = trusted["chunk_range"]
        evidence = trusted["evidence"]
        assert evidence
        assert trusted["allowed_evidence_ids"] == [
            item["evidence_id"] for item in evidence
        ]
        assert "container ID is not an evidence ID" in request.system_instructions  # type: ignore[attr-defined]
        assert all(
            chunk_range["start_line"] <= item["source_range"]["start_line"]
            and item["source_range"]["end_line"] <= chunk_range["end_line"]
            for item in evidence
            if item["source_range"] is not None
        )
        chunk_index = int(request.metadata["chunk_index"])  # type: ignore[attr-defined]
        chunk_count = int(request.metadata["chunk_count"])  # type: ignore[attr-defined]
        observed.append((chunk_index, chunk_count))
        text = request.untrusted_sources[0].text  # type: ignore[attr-defined]
        match = re.search(r"function_\d+", text)
        anchor = match.group(0) if match else "value"
        root = next(
            item["evidence_id"]
            for item in evidence
            if item["evidence_id"] in {"file", f"chunk:{chunk_index - 1:04d}"}
        )
        return json.dumps(
            {
                "schema_version": 1,
                "synopsis": {"text": f"{anchor} chunk", "evidence_ids": [root]},
                "concepts": [{"text": anchor, "evidence_ids": [root]}],
                "responsibilities": [],
                "key_symbols": [],
                "side_effects": [],
                "profile_facts": {},
                "inferred_relationships": [],
            }
        )

    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="http://127.0.0.1:1",
        model_id="semantic-card-chunk-test",
        context_window=4_096,
        context_safety_margin=64,
        retry_limit=0,
        max_json_repair_attempts=1,
    )
    provider = FakeModelProvider(configuration, responder=respond)
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=configuration,
            semantic_max_output_tokens=256,
            semantic_max_chunks_per_file=4,
        )
    )
    card = load_semantic_card(tmp_path, "large.py", manifest=report.manifest)

    assert 2 <= provider.call_count <= 4
    assert observed == [
        (index, observed[0][1]) for index in range(1, len(observed) + 1)
    ]
    assert all(count <= 4 for _, count in observed)
    assert card.provenance.method == "model"
    assert card.quality in {"complete", "partial"}


def test_global_request_and_full_request_token_ceilings_include_repair(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    invalid = _provider(lambda request, call: _response(synopsis_evidence="unknown"))
    request_limited = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=invalid,
            provider_configuration=invalid.configuration,
            semantic_max_requests=1,
        )
    )

    assert invalid.call_count == 1
    assert request_limited.semantic is not None
    assert request_limited.semantic.request_count == 1  # type: ignore[union-attr]
    assert request_limited.semantic.repair_count == 0  # type: ignore[union-attr]

    token_root = tmp_path / "token-case"
    token_root.mkdir()
    (token_root / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    token_limited = _provider(lambda request, call: _response())
    token_report = asyncio.run(
        build_repository_index(
            token_root,
            provider=token_limited,
            provider_configuration=token_limited.configuration,
            semantic_max_input_tokens=1,
        )
    )

    assert token_limited.call_count == 0
    assert token_report.semantic is not None
    assert token_report.semantic.request_count == 0  # type: ignore[union-attr]


def test_semantic_scheduler_is_the_only_card_repair_authority(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def handle(request: str) -> str:\n    return request\n", encoding="utf-8"
    )
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="http://127.0.0.1:1",
        model_id="semantic-card-repair-owner",
        retry_limit=0,
        max_json_repair_attempts=3,
    )
    provider = FakeModelProvider(
        configuration,
        responder=lambda request, call: "not-json" if call == 0 else _response(),
    )

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=configuration,
        )
    )

    assert provider.call_count == 2
    assert report.semantic is not None
    assert report.semantic.request_count == 2  # type: ignore[union-attr]
    assert report.semantic.repair_count == 1  # type: ignore[union-attr]


def test_semantic_evidence_exposes_structural_fact_ids_without_config_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "helper.py").write_text(
        "def serve() -> str:\n    return 'ok'\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "import os\nfrom helper import serve\n"
        "API_URL = os.getenv('SERVICE_SECRET_KEY')\n\n"
        "def run() -> str:\n    return serve()\n",
        encoding="utf-8",
    )
    code_map = next(
        item
        for item in extract_code_maps(scan_repository(tmp_path))
        if item.path == "app.py"
    )

    evidence = cards_module._evidence_table(code_map)
    fact_ids = {item.fact_id for item in evidence if item.fact_id is not None}
    serialized = json.dumps([item.model_dump(mode="json") for item in evidence])

    assert any(value.startswith("relationship:") for value in fact_ids)
    assert any(value.startswith("config-key-sha256:") for value in fact_ids)
    assert "SERVICE_SECRET_KEY" not in serialized


def test_oversized_semantic_cache_is_ignored(tmp_path: Path) -> None:
    source = "def handle(request: str) -> str:\n    return request\n"
    (tmp_path / "old.py").write_text(source, encoding="utf-8")
    provider = _provider(lambda request, call: _response())
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    cache_file = next(
        (tmp_path / ".contextforge" / "index" / "cache" / "semantic").rglob("*.json")
    )
    cache_file.write_bytes(b"x" * 1_000_001)
    (tmp_path / "old.py").rename(tmp_path / "new.py")

    updated = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )

    assert provider.call_count == 2
    assert updated.semantic is not None
    assert updated.semantic.cache_hits == 0  # type: ignore[union-attr]


def test_python_file_under_docs_uses_documentation_profile(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )

    card = load_semantic_card(tmp_path, "docs/example.py", manifest=report.manifest)
    assert card.profile == "documentation"


def test_semantic_card_identity_and_internal_guards_are_strict(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    state = report.manifest.files[0]
    stale_state = state.model_copy(update={"source_sha256": "f" * 64})
    stale = report.manifest.model_copy(update={"files": (stale_state,)})
    with pytest.raises(ValueError, match="identity"):
        load_semantic_card(tmp_path, "app.py", manifest=stale)

    cards_module._raise_if_cancelled(None)
    cancellation = asyncio.Event()
    cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        cards_module._raise_if_cancelled(cancellation)
    with pytest.raises(ValueError, match="published file facts"):
        cards_module._require_facts_sha(
            state.model_copy(update={"record_sha256": None})
        )


def test_changed_low_score_file_wins_priority_in_large_repository(
    tmp_path: Path,
) -> None:
    for index in range(69):
        (tmp_path / f"public_{index:02d}.py").write_text(
            f"def public_{index}():\n    return {index}\n", encoding="utf-8"
        )
    low = tmp_path / "low.py"
    low.write_text("def _low():\n    return 1\n", encoding="utf-8")
    asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    low.write_text("def _low():\n    return 2\n", encoding="utf-8")
    requested: list[str] = []

    def respond(request: object, call: int) -> str:
        del call
        source = request.untrusted_sources[0]  # type: ignore[attr-defined]
        requested.append(source.path)
        payload = json.loads(_response())
        payload["synopsis"] = {
            "text": "Low function",
            "evidence_ids": ["symbol:0000"],
        }
        payload["concepts"] = [{"text": "low", "evidence_ids": ["symbol:0000"]}]
        return json.dumps(payload)

    provider = _provider(respond)
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
            max_files=1,
        )
    )

    assert requested == ["low.py"]
    assert report.semantic is not None
    assert report.semantic.analyzed_paths == ("low.py",)  # type: ignore[union-attr]


def test_priority_includes_central_tier_and_its_related_test(tmp_path: Path) -> None:
    (tmp_path / "hub.py").write_text("def _hub():\n    return 1\n", encoding="utf-8")
    for index in range(4):
        (tmp_path / f"leaf_{index}.py").write_text(
            "from hub import _hub\n\ndef _use():\n    return _hub()\n",
            encoding="utf-8",
        )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_hub.py").write_text(
        "from hub import _hub\n\ndef _test_hub():\n    assert _hub() == 1\n",
        encoding="utf-8",
    )
    snapshot = scan_repository(tmp_path)
    code_maps = extract_code_maps(snapshot)
    graph = build_relationship_graph(code_maps, "3" * 64)

    selected = cards_module._priority_paths(
        code_maps,
        SemanticCardOptions(max_model_files=2),
        graph=graph,
        changed_paths=(),
    )

    assert selected == {"hub.py", "tests/test_hub.py"}


def test_barrel_policy_distinguishes_reexports_from_executable_initializer(
    tmp_path: Path,
) -> None:
    package = tmp_path / "barrel"
    package.mkdir()
    (package / "target.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .target import thing\n__all__ = ['thing']\n", encoding="utf-8"
    )
    executable = tmp_path / "executable"
    executable.mkdir()
    (executable / "target.py").write_text(
        "def thing():\n    return 1\n", encoding="utf-8"
    )
    (executable / "__init__.py").write_text(
        "from .target import thing\nthing()\n", encoding="utf-8"
    )
    maps = {item.path: item for item in extract_code_maps(scan_repository(tmp_path))}

    assert cards_module._requires_deterministic_card(maps["barrel/__init__.py"])
    assert not cards_module._requires_deterministic_card(maps["executable/__init__.py"])


def test_inferred_relationships_validate_and_rebind_both_renames(
    tmp_path: Path,
) -> None:
    source_text = "def source():\n    return 'semantic link'\n"
    target_text = "def target():\n    return 'target'\n"
    (tmp_path / "source.py").write_text(source_text, encoding="utf-8")
    (tmp_path / "target.py").write_text(target_text, encoding="utf-8")
    candidate_counts: list[int] = []

    def respond(request: object, call: int) -> str:
        del call
        trusted = request.trusted_code_map_facts  # type: ignore[attr-defined]
        candidates = trusted["relationship_candidates"]
        candidate_counts.append(len(candidates))
        relationships: list[dict[str, object]] = []
        if trusted["path"] == "source.py":
            target = next(item for item in candidates if item["symbol"] == "target")
            relationships = [
                {
                    "target_candidate_id": target["candidate_id"],
                    "evidence_ids": ["symbol:0000"],
                },
                {
                    "target_candidate_id": target["candidate_id"],
                    "evidence_ids": ["symbol:0000"],
                },
                {
                    "target_candidate_id": "target:" + "f" * 64,
                    "evidence_ids": ["symbol:0000"],
                },
            ]
        payload = json.loads(_response())
        stem = str(trusted["path"]).removesuffix(".py")
        payload["synopsis"] = {
            "text": f"{stem} function",
            "evidence_ids": ["symbol:0000"],
        }
        payload["concepts"] = [{"text": stem, "evidence_ids": ["symbol:0000"]}]
        payload["inferred_relationships"] = relationships
        return json.dumps(payload)

    provider = _provider(respond)
    initial = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )
    source_card = load_semantic_card(tmp_path, "source.py", manifest=initial.manifest)
    structural_graph = load_relationship_graph(
        tmp_path, manifest=initial.structural.manifest
    )
    enriched_graph = load_relationship_graph(tmp_path, manifest=initial.manifest)

    assert candidate_counts and max(candidate_counts) <= 24
    assert source_card.quality == "partial"
    assert len(source_card.inferred_relationships) == 1
    assert source_card.inferred_relationships[0].target_path == "target.py"
    assert any(edge.provenance == "model-inferred" for edge in enriched_graph.edges)
    assert enriched_graph.file_metrics == structural_graph.file_metrics

    (tmp_path / "source.py").rename(tmp_path / "renamed_source.py")
    source_renamed = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )
    rebound_source = load_semantic_card(
        tmp_path, "renamed_source.py", manifest=source_renamed.manifest
    )
    assert provider.call_count == 2
    assert rebound_source.path == "renamed_source.py"
    assert rebound_source.inferred_relationships[0].target_path == "target.py"

    (tmp_path / "target.py").rename(tmp_path / "renamed_target.py")
    target_renamed = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )
    rebound_target = load_semantic_card(
        tmp_path, "renamed_source.py", manifest=target_renamed.manifest
    )
    assert provider.call_count == 2
    assert rebound_target.inferred_relationships[0].target_path == "renamed_target.py"

    (tmp_path / "renamed_target.py").write_text(
        "def target():\n    return 'changed target'\n", encoding="utf-8"
    )
    stale_target = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            update_only=True,
        )
    )
    stale_source = load_semantic_card(
        tmp_path, "renamed_source.py", manifest=stale_target.manifest
    )
    stale_graph = load_relationship_graph(tmp_path, manifest=stale_target.manifest)

    assert provider.call_count == 3
    assert stale_source.synopsis.text == "source function"
    assert stale_source.inferred_relationships == ()
    assert stale_source.quality == "partial"
    assert not any(edge.provenance == "model-inferred" for edge in stale_graph.edges)
