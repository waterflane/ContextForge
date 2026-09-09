"""ContextForge package."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from contextforge._metadata import __version__
from contextforge.logging import (
    DIAGNOSTIC_SCHEMA_VERSION,
    DiagnosticRecord,
    LogFormat,
    LogLevel,
    recent_records,
)
from contextforge.progress import (
    NO_OP_PROGRESS_OBSERVER,
    PROGRESS_SCHEMA_VERSION,
    NoOpProgressObserver,
    ProgressActivity,
    ProgressEvent,
    ProgressObserver,
    ProgressReporter,
    ProgressStatus,
)

if TYPE_CHECKING:
    from contextforge.context import (
        CompiledContextCapsule,
        ConservativeTokenEstimator,
        ContextBudget,
        ContextCapsule,
        RepresentationMode,
        TokenEstimator,
        compile_context_capsule,
    )
    from contextforge.intelligence import (
        CandidateCard,
        RetrievalResult,
        SemanticCard,
        load_orientation_map,
        load_relationship_graph,
        retrieve_context_candidates,
    )

_LAZY_EXPORTS = {
    "CandidateCard": "contextforge.intelligence",
    "CompiledContextCapsule": "contextforge.context",
    "ConservativeTokenEstimator": "contextforge.context",
    "ContextBudget": "contextforge.context",
    "ContextCapsule": "contextforge.context",
    "RepresentationMode": "contextforge.context",
    "RetrievalResult": "contextforge.intelligence",
    "SemanticCard": "contextforge.intelligence",
    "TokenEstimator": "contextforge.context",
    "compile_context_capsule": "contextforge.context",
    "load_orientation_map": "contextforge.intelligence",
    "load_relationship_graph": "contextforge.intelligence",
    "retrieve_context_candidates": "contextforge.intelligence",
}


def __getattr__(name: str) -> Any:
    """Load the v3 public API without coupling foundational subpackages."""

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "NO_OP_PROGRESS_OBSERVER",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "DiagnosticRecord",
    "LogFormat",
    "LogLevel",
    "PROGRESS_SCHEMA_VERSION",
    "NoOpProgressObserver",
    "ProgressActivity",
    "ProgressEvent",
    "ProgressObserver",
    "ProgressReporter",
    "ProgressStatus",
    "CandidateCard",
    "CompiledContextCapsule",
    "ConservativeTokenEstimator",
    "ContextBudget",
    "ContextCapsule",
    "RepresentationMode",
    "RetrievalResult",
    "SemanticCard",
    "TokenEstimator",
    "compile_context_capsule",
    "load_orientation_map",
    "load_relationship_graph",
    "recent_records",
    "retrieve_context_candidates",
    "__version__",
]
