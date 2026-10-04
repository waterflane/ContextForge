import asyncio
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.intelligence import retrieve_context_candidates
from contextforge.intelligence.retrieval import parse_query_intent


@pytest.mark.parametrize("word", ["request", "build", "mode"])
def test_prose_symbol_matches_do_not_override_topical_source(
    tmp_path: Path, word: str
) -> None:
    (tmp_path / "incidental.py").write_text(
        f"def {word}():\n    return 0\n", encoding="utf-8"
    )
    (tmp_path / "topical.py").write_text(
        "def amber_cobalt_stage():\n    return 1\n", encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    task = f"Explain the amber cobalt {word} behavior"
    result = asyncio.run(
        retrieve_context_candidates(tmp_path, task, manifest=report.manifest)
    )
    assert (
        result.query_intent is not None and result.query_intent.explicit_anchors == ()
    )
    assert result.candidates[0].path == "topical.py"
    incidental = next(c for c in result.candidates if c.path == "incidental.py")
    assert incidental.exact_group == "exact_symbol"
    assert incidental.match_origin == "lexical-discovery"
    explicit = asyncio.run(
        retrieve_context_candidates(
            tmp_path, f"Explain `{word}` implementation", manifest=report.manifest
        )
    )
    assert explicit.candidates[0].path == "incidental.py"
    assert explicit.candidates[0].match_origin == "explicit-anchor"
    assert explicit.requirements is not None
    assert "caller" not in {r.kind for r in explicit.requirements.roles}


@pytest.mark.parametrize(
    "task,anchors",
    [
        ("Find callers of run", ("run",)),
        ("Explain Widget.run implementation", ("Widget.run",)),
        ("Review execute_job implementation", ("execute_job",)),
        ("Review src/widget.py", ("src/widget.py",)),
        ("run", ("run",)),
        ("Trace the request pipeline", ()),
    ],
)
def test_explicit_anchor_syntax_has_one_canonical_intent(
    task: str, anchors: tuple[str, ...]
) -> None:
    assert parse_query_intent(task).explicit_anchors == anchors


def test_role_nouns_can_remain_topic_facets() -> None:
    intent = parse_query_intent("Explain model data schema configuration behavior")
    assert {"model", "data", "schema", "configuration"} <= set(intent.facet_terms)
    assert "implementation" in {r.kind for r in intent.roles}
    contextual = parse_query_intent("Explain amber with configuration evidence")
    assert "configuration" not in contextual.facet_terms
    assert "configuration" in {r.kind for r in contextual.roles}


def test_bare_explicit_role_named_symbol_is_not_role_syntax() -> None:
    assert parse_query_intent("callers").roles[0].kind == "unknown"
