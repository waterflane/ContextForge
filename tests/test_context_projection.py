"""Compact projection preserves source and omits proof-ledger duplication."""

import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from contextforge.application import build_repository_index
from contextforge.intelligence import retrieve_context_candidates


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def execute_job():\n    return 7\n")
    asyncio.run(
        build_repository_index(
            source, provider=None, provider_configuration=None, semantic_scope="none"
        )
    )
    return source, tmp_path / "artifacts"


@pytest.mark.parametrize("mode", ["full", "slice", "map", "summary"])
def test_answer_projection_preserves_material_and_omits_internal_addresses(
    workspace: tuple[Path, Path], mode: str
) -> None:
    from contextforge.context import (
        ContextBudget,
        compile_context_capsule,
        render_capsule_source_context,
    )
    from contextforge.context.capsule import (
        CapsuleMaterial,
        CapsuleRange,
        RepresentationMode,
    )

    source, _ = workspace
    retrieval = asyncio.run(retrieve_context_candidates(source, "execute_job"))
    compiled = compile_context_capsule(
        source,
        "execute_job",
        retrieval,
        budget=ContextBudget(context_window_tokens=8192),
    )
    content = 'def execute_job():\n    return "<tag>& structural-in-source"\n'
    if mode == "slice":
        content = "worker.py:3-4\n" + content + "\nworker.py:8-8\nDEFAULT = 7\n"
    material = CapsuleMaterial(
        path="worker.py",
        source_sha256="a" * 64,
        representation=RepresentationMode(mode),
        ranges=(
            CapsuleRange(start_line=3, end_line=4),
            CapsuleRange(start_line=8, end_line=8),
        )
        if mode == "slice"
        else (),
        content=content,
        evidence_ids=tuple(f"structural-{i:032x}" for i in range(800)),
        relevance=1,
        provenance=("verified-structure",),
        token_count=40,
    )
    capsule = compiled.capsule.model_copy(
        update={
            "working_set": (),
            "task_context": (material,),
            "git_context": "diff <old> & <new>",
            "interpretations": ("Observed intent <description>.",),
        }
    )
    compiled = compiled.model_copy(update={"capsule": capsule})
    labels: list[dict[str, Any]] = [
        {
            "path": "worker.py",
            "name": "worker.execute_job",
            "range": {"start_line": 3 if mode == "slice" else 1},
            "implementation_end_line": 4 if mode == "slice" else 2,
            "purpose": "Return <tag> & text.",
        },
        {
            "path": "worker.py",
            "name": "worker.outside_material",
            "range": {"start_line": 99},
            "implementation_end_line": 100,
        },
    ]
    prompt = render_capsule_source_context(compiled, labels)
    root = ET.fromstring(prompt)
    physical = mode in {"full", "slice"}
    body = root.find("file/code" if physical else "file/orientation")
    git = root.find("git")
    interpretation = root.find("interpretation")
    file = root.find("file")
    assert body is not None and git is not None
    assert interpretation is not None and file is not None
    if physical:
        reconstructed = "\n".join(
            line.split(": ", 1)[1] for line in (body.text or "").splitlines()
        )
        expected = content.splitlines()
        if mode == "slice":
            expected = [expected[1], expected[2], expected[5]]
        assert reconstructed == "\n".join(expected)
    else:
        assert body.text == content
    assert git.text == capsule.git_context
    assert interpretation.text == capsule.interpretations[0]
    assert file.attrib["lines"] == (
        "3-4,8-8" if mode == "slice" else "1-2" if mode == "full" else ""
    )
    names = [item.attrib["name"] for item in root.findall("file/function")]
    assert names == (["worker.execute_job"] if physical else [])
    if physical:
        purpose = root.find("file/function/purpose")
        assert purpose is not None and purpose.text == labels[0]["purpose"]
    assert material.source_sha256 not in prompt
    assert all(identity not in prompt for identity in material.evidence_ids)
    assert "structural-in-source" in (body.text or "")


@pytest.mark.parametrize("damage", [None, "header", "missing_line"])
def test_source_slice_framing_distinguishes_literal_headers(
    workspace: tuple[Path, Path], damage: str | None
) -> None:
    from contextforge.context import (
        ContextBudget,
        compile_context_capsule,
        render_capsule_source_context,
    )
    from contextforge.context.capsule import CapsuleRange, RepresentationMode

    source, _ = workspace
    compiled = compile_context_capsule(
        source,
        "execute_job",
        asyncio.run(retrieve_context_candidates(source, "execute_job")),
        budget=ContextBudget(context_window_tokens=8192),
    )
    material = compiled.capsule.task_context[0].model_copy(
        update={
            "representation": RepresentationMode.SLICE,
            "ranges": (CapsuleRange(start_line=1, end_line=3),),
            "content": (
                "worker.py:1-3\ndef execute_job():\nworker.py:9-9\n    return 7\n"
            ),
        }
    )
    if damage == "header":
        material = material.model_copy(
            update={"content": "worker.py:2-4\ndef execute_job():\n    return 7\n"}
        )
    elif damage == "missing_line":
        material = material.model_copy(
            update={"content": "worker.py:1-3\ndef execute_job():\n    return 7\n"}
        )
    compiled = compiled.model_copy(
        update={
            "capsule": compiled.capsule.model_copy(update={"task_context": (material,)})
        }
    )
    if damage:
        with pytest.raises(ValueError, match="source slice framing"):
            render_capsule_source_context(compiled)
    else:
        body = ET.fromstring(render_capsule_source_context(compiled)).find("file/code")
        assert body is not None
        assert body.text == "1: def execute_job():\n2: worker.py:9-9\n3:     return 7"


def test_compact_cli_prompt_retains_auditable_capsule(
    workspace: tuple[Path, Path],
) -> None:
    import json

    from typer.testing import CliRunner

    from contextforge.cli.main import app

    source, _ = workspace
    output = source.parent / "capsule.json"
    prompt = source.parent / "source.xml"
    result = CliRunner().invoke(
        app,
        [
            "context",
            "create",
            str(source),
            "--task",
            "Explain `execute_job`.",
            "--planning",
            "off",
            "--compact-prompt",
            "--format",
            "json",
            "--output",
            str(output),
            "--prompt-output",
            str(prompt),
            "--context-tokens",
            "8192",
        ],
    )
    assert result.exit_code == 0, result.output
    capsule = json.loads(output.read_text())
    assert capsule["schema_version"] == 2
    assert capsule["task_context"][0]["source_sha256"]
    assert capsule["task_context"][0]["evidence_ids"]
    projected = ET.fromstring(prompt.read_text())
    assert projected.tag == "context"
    body = projected.find("file/code")
    assert body is not None and "def execute_job" in (body.text or "")
    assert capsule["task_context"][0]["source_sha256"] not in prompt.read_text()


@pytest.mark.parametrize(
    "arguments", [[], ["--task", "Explain this", "--legacy-handoff"]]
)
def test_compact_cli_prompt_rejects_unsupported_flows(
    workspace: tuple[Path, Path], arguments: list[str]
) -> None:
    from typer.testing import CliRunner

    from contextforge.cli.main import app

    source, _ = workspace
    result = CliRunner().invoke(
        app, ["context", "create", str(source), "--compact-prompt", *arguments]
    )
    assert result.exit_code == 2
    assert "--compact-prompt requires" in result.output
