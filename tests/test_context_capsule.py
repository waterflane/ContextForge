import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.application import build_repository_index
from contextforge.context import (
    CapsuleMaterial,
    CapsuleRange,
    CompiledContextCapsule,
    ConservativeTokenEstimator,
    ContextBudget,
    ContextBudgetError,
    ContextCapsule,
    ContextFreshnessError,
    RepresentationMode,
    compile_context_capsule,
)
from contextforge.intelligence import (
    GroundedClaim,
    SourceRange,
    load_file_code_map,
    load_semantic_card,
    retrieve_context_candidates,
)


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _build(root: Path):
    return asyncio.run(
        build_repository_index(
            root,
            provider=None,
            provider_configuration=None,
        )
    )


def _retrieve(root: Path, report: object, task: str):
    return asyncio.run(
        retrieve_context_candidates(
            root,
            task,
            manifest=report.manifest,  # type: ignore[attr-defined]
        )
    )


def _budget(tokens: int) -> ContextBudget:
    return ContextBudget(context_window_tokens=tokens)


def test_compiler_renders_stable_full_capsule_for_small_source(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def greet(name: str) -> str:\n    return f'<hello>{name}</hello>'\n",
    )
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "change greet")

    first = compile_context_capsule(
        tmp_path, "change <greet>", retrieval, budget=_budget(4_000)
    )
    second = compile_context_capsule(
        tmp_path, "change <greet>", retrieval, budget=_budget(4_000)
    )

    assert first == second
    assert first.capsule.schema_version == 2
    assert first.capsule.task_context[0].representation == RepresentationMode.FULL
    assert "&lt;greet&gt;" in first.prompt
    assert "&lt;hello&gt;" in first.prompt
    assert first.prompt.startswith('<contextforge schema_version="2">')
    assert first.prompt.endswith("</contextforge>\n")
    assert first.token_count <= first.capsule.allocations["task_evidence"] + 4_000


def test_tight_budget_keeps_indivisible_map_instead_of_partial_source(
    tmp_path: Path,
) -> None:
    source = "\n".join(f"plain line {index}" for index in range(120)) + "\n"
    _write(tmp_path, "large.txt", source)
    report = _build(tmp_path)
    retrieval = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "large.txt",
            manifest=report.structural.manifest,
        )
    )

    compiled = compile_context_capsule(
        tmp_path,
        "inspect large.txt",
        retrieval,
        budget=_budget(550),
        manifest=report.structural.manifest,
    )

    material = compiled.capsule.task_context[0]
    assert material.representation == RepresentationMode.MAP
    assert "plain line 119" not in material.content
    assert compiled.token_count <= 550


def test_grounded_summary_upgrades_when_source_modes_do_not_fit(tmp_path: Path) -> None:
    source = "\n".join(f"configuration line {index}" for index in range(260)) + "\n"
    _write(tmp_path, "settings.txt", source)
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "documentation configuration")

    compiled = compile_context_capsule(
        tmp_path, "documentation configuration", retrieval, budget=_budget(800)
    )

    material = next(
        item for item in compiled.capsule.task_context if item.path == "settings.txt"
    )
    assert material.representation == RepresentationMode.SUMMARY
    assert material.provenance == (
        "verified-structure",
        "grounded-semantic-card",
    )
    assert "synopsis:" in material.content


def test_working_ranges_expand_context_and_merge_nearby_blocks(tmp_path: Path) -> None:
    source = "".join(f"line {index}\n" for index in range(1, 221))
    _write(tmp_path, "notes.txt", source)
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "notes")
    requested = {
        "notes.txt": (
            SourceRange(start_line=10, start_column=0, end_line=10, end_column=1),
            SourceRange(start_line=18, start_column=0, end_line=18, end_column=1),
        )
    }

    compiled = compile_context_capsule(
        tmp_path,
        "inspect notes",
        retrieval,
        budget=_budget(1_200),
        working_files=("notes.txt",),
        working_lines=requested,
    )

    material = compiled.capsule.working_set[0]
    assert material.representation == RepresentationMode.SLICE
    assert material.ranges == (CapsuleRange(start_line=5, end_line=23),)
    assert material.content.startswith("notes.txt:5-23\nline 5\n")
    assert material.content.endswith("line 23\n")
    assert "line 24" not in material.content


def test_large_full_file_requires_explicit_pin(tmp_path: Path) -> None:
    source = "".join(f"line {index}\n" for index in range(1, 221))
    _write(tmp_path, "large.txt", source)
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "large")

    automatic = compile_context_capsule(
        tmp_path,
        "inspect large",
        retrieval,
        budget=_budget(10_000),
        working_files=("large.txt",),
    )
    pinned = compile_context_capsule(
        tmp_path,
        "inspect large",
        retrieval,
        budget=_budget(10_000),
        working_files=("large.txt",),
        pinned_full_files=("large.txt",),
    )

    assert automatic.capsule.working_set[0].representation != RepresentationMode.FULL
    assert pinned.capsule.working_set[0].representation == RepresentationMode.FULL
    assert pinned.capsule.working_set[0].content == source


def test_working_full_falls_back_to_map_and_is_not_selected_twice(
    tmp_path: Path,
) -> None:
    source = "".join(f"line {index}\n" for index in range(1, 221))
    _write(tmp_path, "large.txt", source)
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "large")

    compiled = compile_context_capsule(
        tmp_path,
        "inspect large",
        retrieval,
        budget=_budget(600),
        working_files=("large.txt",),
        pinned_full_files=("large.txt",),
    )

    assert compiled.capsule.working_set[0].representation != RepresentationMode.FULL
    assert compiled.capsule.task_context == ()
    assert [item.path for item in compiled.capsule.working_set].count("large.txt") == 1


def test_source_change_after_retrieval_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "run")
    _write(tmp_path, "app.py", "def run():\n    return 2\n")

    with pytest.raises(ContextFreshnessError, match="source changed"):
        compile_context_capsule(
            tmp_path, "change run", retrieval, budget=_budget(2_000)
        )


def test_git_section_is_omitted_whole_when_its_allocation_is_too_small(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "VALUE = 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "app")

    compiled = compile_context_capsule(
        tmp_path,
        "inspect app",
        retrieval,
        budget=_budget(1_000),
        git_diff="diff --git a/app.py b/app.py\n" + "+changed\n" * 200,
    )

    assert compiled.capsule.git_context == ""
    assert compiled.capsule.interpretations == (
        "Git diff omitted because its complete section exceeded budget.",
    )
    assert "+changed" not in compiled.prompt


def test_budget_deductions_and_exact_estimator_are_hard_limits(tmp_path: Path) -> None:
    class CharacterEstimator:
        estimator_id = "characters-v1"

        def count(self, text: str) -> int:
            return len(text)

    _write(tmp_path, "app.py", "VALUE = 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "app")
    budget = ContextBudget(
        context_window_tokens=2_000,
        history_tokens=200,
        response_tokens=300,
        safety_margin_tokens=100,
    )

    compiled = compile_context_capsule(
        tmp_path,
        "inspect app",
        retrieval,
        budget=budget,
        estimator=CharacterEstimator(),
    )

    assert budget.available_tokens == 1_400
    assert compiled.estimator_id == "characters-v1"
    assert compiled.token_count == len(compiled.prompt)
    assert compiled.token_count <= budget.available_tokens


def test_model_representation_rationale_stays_in_interpretation_section(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "run")
    candidate = retrieval.candidates[0].model_copy(
        update={"suggested_representation": "slice"}
    )
    reranked = retrieval.model_copy(update={"candidates": (candidate,)})

    compiled = compile_context_capsule(
        tmp_path, "change run", reranked, budget=_budget(2_000)
    )

    assert "(interpretation)" in compiled.capsule.interpretations[0]
    assert "<interpretations>" in compiled.prompt
    assert "suggestion" not in compiled.capsule.repository_map


def test_over_budget_model_rationale_is_removed_as_one_section(tmp_path: Path) -> None:
    class InterpretationPenaltyEstimator:
        estimator_id = "interpretation-penalty-v1"

        def count(self, text: str) -> int:
            penalty = 1_000 if "<interpretation>" in text else 0
            return (len(text.encode("utf-8")) + 2) // 3 + penalty

    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "run")
    candidate = retrieval.candidates[0].model_copy(
        update={"suggested_representation": "slice"}
    )

    compiled = compile_context_capsule(
        tmp_path,
        "change run",
        retrieval.model_copy(update={"candidates": (candidate,)}),
        budget=_budget(1_200),
        estimator=InterpretationPenaltyEstimator(),
    )

    assert compiled.capsule.interpretations == ()
    assert "<interpretation>" not in compiled.prompt
    assert compiled.token_count <= 1_200


def test_capsule_models_reject_invalid_ranges_duplicates_and_empty_budget() -> None:
    with pytest.raises(ValidationError, match="no available tokens"):
        ContextBudget(context_window_tokens=100, history_tokens=100)
    with pytest.raises(ValueError, match="negative"):
        _budget(100).initial_allocations(-1)
    with pytest.raises(ValidationError, match="must not precede"):
        CapsuleRange(start_line=2, end_line=1)
    material = CapsuleMaterial(
        path="app.py",
        source_sha256="0" * 64,
        representation=RepresentationMode.MAP,
        content="app.py",
        relevance=1.0,
        provenance=("verified-structure",),
        token_count=2,
    )
    payload = {
        "task": "task",
        "snapshot": {
            "generation_id": "0" * 64,
            "source_snapshot_digest": "0" * 64,
            "generation_kind": "structural",
            "index_schema_version": 3,
        },
        "repository_map": "",
        "working_set": [material.model_dump(mode="json")],
        "task_context": [material.model_dump(mode="json")],
        "allocations": {
            "diff_metadata": 0,
            "orientation": 0,
            "task_evidence": 0,
            "working_set": 0,
        },
        "estimator_id": "test",
        "token_count": 0,
    }
    with pytest.raises(ValidationError, match="identities must be unique"):
        ContextCapsule.model_validate(payload)
    with pytest.raises(ValidationError, match="only slice material"):
        CapsuleMaterial.model_validate(
            {
                **material.model_dump(mode="json"),
                "ranges": [{"start_line": 1, "end_line": 1}],
            }
        )


def test_too_small_budget_rejects_indivisible_envelope(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "VALUE = 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "app")

    with pytest.raises(ContextBudgetError, match="capsule envelope"):
        compile_context_capsule(tmp_path, "inspect app", retrieval, budget=_budget(1))


def test_default_estimator_counts_utf8_bytes_conservatively() -> None:
    estimator = ConservativeTokenEstimator()
    assert estimator.estimator_id == "utf8-bytes-ceil-div-3-v1"
    assert estimator.count("abc") == 1
    assert estimator.count("аб") == 2


def test_compiler_rejects_unpinned_inputs_and_invalid_selection(tmp_path: Path) -> None:
    class EmptyEstimator:
        estimator_id = ""

        def count(self, text: str) -> int:
            return len(text)

    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "run")

    with pytest.raises(TypeError, match="RetrievalResult"):
        compile_context_capsule(  # type: ignore[arg-type]
            tmp_path, "run", object(), budget=_budget(2_000)
        )
    old_manifest = report.manifest.model_copy(update={"schema_version": 2})
    with pytest.raises(Exception, match="Index v3"):
        compile_context_capsule(
            tmp_path,
            "run",
            retrieval,
            budget=_budget(2_000),
            manifest=old_manifest,
        )
    stale_retrieval = retrieval.model_copy(update={"generation_id": "f" * 64})
    with pytest.raises(ContextFreshnessError, match="not pinned"):
        compile_context_capsule(tmp_path, "run", stale_retrieval, budget=_budget(2_000))
    with pytest.raises(ValueError, match="estimator_id"):
        compile_context_capsule(
            tmp_path,
            "run",
            retrieval,
            budget=_budget(2_000),
            estimator=EmptyEstimator(),
        )
    ranges = {
        "app.py": (SourceRange(start_line=1, start_column=0, end_line=1, end_column=1),)
    }
    with pytest.raises(ValueError, match="matching working file"):
        compile_context_capsule(
            tmp_path,
            "run",
            retrieval,
            budget=_budget(2_000),
            working_lines=ranges,
        )
    with pytest.raises(ValueError, match="belong to the generation"):
        compile_context_capsule(
            tmp_path,
            "run",
            retrieval,
            budget=_budget(2_000),
            working_files=("missing.py",),
        )
    with pytest.raises(ValueError, match="unique and canonical"):
        compile_context_capsule(
            tmp_path,
            "run",
            retrieval,
            budget=_budget(2_000),
            working_files=("app.py", "app.py"),
        )


def test_candidate_hash_must_match_pinned_codemap(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def run():\n    return 1\n")
    report = _build(tmp_path)
    retrieval = _retrieve(tmp_path, report, "run")
    stale_candidate = retrieval.candidates[0].model_copy(
        update={"source_sha256": "f" * 64}
    )
    stale = retrieval.model_copy(update={"candidates": (stale_candidate,)})

    with pytest.raises(ContextFreshnessError, match="candidate source identity"):
        compile_context_capsule(tmp_path, "run", stale, budget=_budget(2_000))


def test_orientation_hierarchy_and_git_context_object_helpers(tmp_path: Path) -> None:
    from contextforge.context import capsule as capsule_module
    from contextforge.intelligence import load_orientation_map

    class Diff:
        text = "complete diff"

    _write(tmp_path, "src/a.py", "A = 1\n")
    _write(tmp_path, "src/b.py", "B = 2\n")
    report = _build(tmp_path)
    orientation = load_orientation_map(tmp_path, manifest=report.manifest)
    estimator = ConservativeTokenEstimator()

    hierarchy = capsule_module._render_orientation(orientation, 30, estimator)
    assert "module src" in hierarchy
    assert capsule_module._render_orientation(orientation, 0, estimator) == ""
    assert capsule_module._git_text(Diff()) == "complete diff"
    with pytest.raises(TypeError, match="GitDiffContext-like"):
        capsule_module._git_text(object())


def test_summary_profile_facts_and_slice_declaration_expansion(tmp_path: Path) -> None:
    from contextforge.context import capsule as capsule_module

    _write(
        tmp_path,
        "app.py",
        "header = 1\n\ndef run(value: int) -> int:\n    changed = value + 1\n"
        "    return changed\n\nfooter = 2\n",
    )
    report = _build(tmp_path)
    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)
    evidence_id = next(iter(card.evidence)).evidence_id
    enriched = card.model_copy(
        update={
            "profile_facts": {
                "apis": (
                    GroundedClaim(text="run is callable", evidence_ids=(evidence_id,)),
                )
            }
        }
    )
    assert "apis: run is callable" in capsule_module._summary_content(enriched)

    code_map = load_file_code_map(tmp_path, "app.py", manifest=report.manifest)
    ranges = capsule_module._slice_ranges(
        (
            SourceRange(
                start_line=4,
                start_column=0,
                end_line=4,
                end_column=1,
            ),
        ),
        code_map,
        7,
    )
    assert ranges == (CapsuleRange(start_line=1, end_line=7),)


def test_capsule_metadata_and_range_order_validation() -> None:
    material = CapsuleMaterial(
        path="app.py",
        source_sha256="0" * 64,
        representation=RepresentationMode.SLICE,
        content="slice",
        ranges=(
            CapsuleRange(start_line=1, end_line=2),
            CapsuleRange(start_line=4, end_line=5),
        ),
        relevance=1.0,
        provenance=("verified-source-ranges",),
        token_count=2,
    )
    payload = material.model_dump(mode="json")
    payload["ranges"] = [
        {"start_line": 2, "end_line": 3},
        {"start_line": 3, "end_line": 4},
    ]
    with pytest.raises(ValidationError, match="sorted and disjoint"):
        CapsuleMaterial.model_validate(payload)

    capsule_payload = {
        "task": "task",
        "snapshot": {
            "generation_id": "0" * 64,
            "source_snapshot_digest": "0" * 64,
            "generation_kind": "structural",
            "index_schema_version": 3,
        },
        "repository_map": "",
        "allocations": {"z": 0, "a": 0},
        "estimator_id": "test",
        "token_count": 0,
    }
    with pytest.raises(ValidationError, match="allocations must be canonical"):
        ContextCapsule.model_validate(capsule_payload)
    capsule_payload["allocations"] = {"a": 0, "z": 0}
    capsule = ContextCapsule.model_validate(capsule_payload)
    with pytest.raises(ValidationError, match="metadata is inconsistent"):
        CompiledContextCapsule(
            capsule=capsule,
            prompt="prompt",
            token_count=1,
            estimator_id="other",
        )
    with pytest.raises(ValidationError, match="bounded non-empty"):
        ContextCapsule.model_validate({**capsule_payload, "task": "\x00"})
