import pytest

from contextforge.repositories.language import detect_language


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/contextforge/app.py", "Python"),
        (r"src\web\app.TSX", "TypeScript"),
        ("config/settings.yaml", "YAML"),
        ("config/settings.yml", "YAML"),
        ("pyproject.toml", "TOML"),
        ("README.md", "Markdown"),
        ("Dockerfile", "Dockerfile"),
        ("nested/MAKEFILE", "Makefile"),
        (".env", "Dotenv"),
        ("unknown.xyz", None),
        ("LICENSE", None),
        ("nested/файл", None),
    ],
)
def test_detect_language(path: str, expected: str | None) -> None:
    assert detect_language(path) == expected
