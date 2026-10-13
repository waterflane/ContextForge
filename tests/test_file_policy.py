from __future__ import annotations

import pytest

from contextforge.intelligence.file_policy import FILE_POLICY_REGISTRY


@pytest.mark.parametrize(
    "path",
    (
        "test/unit/service.py",
        "tests/unit/service.py",
        "__tests__/service.ts",
        "spec/service.cs",
        "specs/service.java",
        "src/test/kotlin/example/Service.kt",
        "src/service.test.js",
        "src/service.spec.tsx",
        "src/ServiceTest.kt",
        "src/ServiceTests.cs",
        "src/test_service.py",
        "src/service_test.py",
    ),
)
def test_registry_classifies_supported_polyglot_test_paths(path: str) -> None:
    assert FILE_POLICY_REGISTRY.is_test(path)


def test_registry_does_not_classify_ordinary_source_as_test() -> None:
    assert not FILE_POLICY_REGISTRY.is_test("src/service.ts")
