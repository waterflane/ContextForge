"""Optional schema-bound task refinement with no repository source exposure."""

from __future__ import annotations

import asyncio

from contextforge.context import ContextPackage
from contextforge.models import ModelProvider, ModelProviderError, ModelRequest

from .models import (
    TASK_REFINEMENT_PROMPT_VERSION,
    TaskRefinement,
    TaskRefinementResponse,
    calculate_context_package_identity,
)

TASK_REFINEMENT_SYSTEM_INSTRUCTIONS = """Clarify one user-authored repository task.
The original task is authoritative and must remain preserved separately. A refinement
may clarify intent, propose acceptance criteria, list open questions, and identify
likely affected areas. It must not remove or weaken user constraints, silently broaden
scope, replace the original task, invent completed requirements, claim repository
facts not supplied here, or request secrets. Return only the closed response schema.
No repository source, filesystem, Git, shell, network, credential, or execution tool is
available to this operation."""


class TaskRefinementError(RuntimeError):
    """Raised when a provider violates the task-refinement response contract."""


async def refine_task(
    original_task: str,
    context_package: ContextPackage,
    provider: ModelProvider,
    *,
    cancellation: asyncio.Event | None = None,
) -> TaskRefinement:
    """Optionally clarify a task using only portable package metadata, never source."""

    if not original_task.strip() or "\x00" in original_task:
        raise ValueError("original task must be bounded non-empty text")
    if not isinstance(context_package, ContextPackage):
        raise TypeError("task refinement requires a ContextPackage")
    identity = calculate_context_package_identity(context_package)
    request = ModelRequest(
        operation_id=f"task-refinement-{identity[:32]}",
        purpose="task-refinement",
        system_instructions=TASK_REFINEMENT_SYSTEM_INSTRUCTIONS,
        analysis_task=(
            "Preserve the following original user task exactly in the caller-owned "
            "artifact while proposing only optional clarification:\n"
            "<ORIGINAL_USER_TASK>\n"
            f"{original_task}\n"
            "</ORIGINAL_USER_TASK>"
        ),
        trusted_code_map_facts={
            "prompt_version": TASK_REFINEMENT_PROMPT_VERSION,
            "source_package_identity": identity,
            "selected_paths": [item.path for item in context_package.files],
            "selected_file_count": context_package.statistics.selected_file_count,
            "languages": context_package.statistics.languages,
        },
        untrusted_sources=(),
        response_model=TaskRefinementResponse,
        max_output_tokens=4_096,
        max_response_bytes=256 * 1024,
        temperature=0.0,
        metadata={"prompt_version": TASK_REFINEMENT_PROMPT_VERSION},
    )
    try:
        response = await provider.complete_structured(
            request, cancellation=cancellation
        )
    except ModelProviderError as exc:
        raise TaskRefinementError(
            "provider did not return a valid task-refinement response"
        ) from exc
    if not isinstance(response.value, TaskRefinementResponse):
        raise TaskRefinementError("provider returned the wrong task-refinement model")
    return TaskRefinement.from_response(
        response.value,
        provider=response.provider_id,
        model=response.model_id,
        source_package_identity=identity,
    )


__all__ = [
    "TASK_REFINEMENT_SYSTEM_INSTRUCTIONS",
    "TaskRefinementError",
    "refine_task",
]
