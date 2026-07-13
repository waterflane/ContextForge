from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.context import ContextPackage
from contextforge.prompts import PromptPackage
from contextforge.repositories.analysis import RepositoryAnalyzer
from contextforge.storage.backend import StorageBackend


def test_foundation_package_models_are_frozen() -> None:
    context_package = ContextPackage(title="Repository", items=("README.md",))
    prompt_package = PromptPackage(title="Review", body="Review the repository.")

    assert context_package.items == ("README.md",)
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
