from pathlib import Path

import pytest
from pydantic import ValidationError

import contextforge
from contextforge.context import build_context_package
from contextforge.prompts import PromptPackage
from contextforge.repositories.analysis import RepositoryAnalyzer
from contextforge.storage.backend import StorageBackend


def test_foundation_package_models_are_frozen(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Repository\n", encoding="utf-8")
    context_package = build_context_package(tmp_path)
    prompt_package = PromptPackage(title="Review", body="Review the repository.")

    assert tuple(item.path for item in context_package.items) == ("README.md",)
    assert prompt_package.body == "Review the repository."
    with pytest.raises(ValidationError):
        context_package.title = "Changed"
    with pytest.raises(ValidationError):
        prompt_package.body = "Changed"


def test_foundation_protocol_default_methods_are_explicitly_unimplemented() -> None:
    with pytest.raises(NotImplementedError):
        RepositoryAnalyzer.analyze(object(), Path("."))  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        StorageBackend.connect(object())  # type: ignore[arg-type]


def test_index_v3_public_api_is_lazy_and_discoverable() -> None:
    assert contextforge.ContextBudget.__name__ == "ContextBudget"
    assert contextforge.SemanticCard.__name__ == "SemanticCard"
    assert callable(contextforge.retrieve_context_candidates)
    assert callable(contextforge.compile_context_capsule)
    with pytest.raises(AttributeError, match="no attribute"):
        contextforge.__getattr__("missing_public_api")
