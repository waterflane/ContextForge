import hashlib
from pathlib import Path

import pytest

from contextforge.repositories.files import (
    deterministic_relative_path,
    is_binary_file,
    is_text_file,
    normalize_relative_path,
    sha256_file,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/contextforge/app.py", "src/contextforge/app.py"),
        (r"src\contextforge\app.py", "src/contextforge/app.py"),
        ("./src//contextforge/../app.py", "src/app.py"),
        ("данные/пример.py", "данные/пример.py"),
    ],
)
def test_normalize_relative_path(path: str, expected: str) -> None:
    assert normalize_relative_path(path) == expected


@pytest.mark.parametrize("path", ["", ".", "..", "../secret", "/rooted", "C:/rooted"])
def test_normalize_relative_path_rejects_non_relative_paths(path: str) -> None:
    with pytest.raises(ValueError):
        normalize_relative_path(path)


def test_deterministic_relative_path_for_nested_non_ascii_file(tmp_path: Path) -> None:
    nested_file = tmp_path / "исходники" / "пример.py"
    nested_file.parent.mkdir()
    nested_file.write_text("print('привет')", encoding="utf-8")

    assert deterministic_relative_path(tmp_path, nested_file) == "исходники/пример.py"


def test_deterministic_relative_path_validates_root_and_containment(
    tmp_path: Path,
) -> None:
    root_file = tmp_path / "root.txt"
    root_file.write_text("root", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(FileNotFoundError):
        deterministic_relative_path(tmp_path / "missing", outside_file)
    with pytest.raises(NotADirectoryError):
        deterministic_relative_path(root_file, outside_file)
    with pytest.raises(ValueError, match="outside repository root"):
        deterministic_relative_path(repository, outside_file)


@pytest.mark.parametrize(
    ("filename", "content", "expected_binary"),
    [
        ("empty.txt", b"", False),
        ("utf8.txt", "Hello, мир!\n".encode(), False),
        ("null.bin", b"text\x00more", True),
        ("invalid-utf8.bin", b"\xff\xfe", True),
        ("controls.bin", bytes(range(1, 8)), True),
    ],
)
def test_binary_detection_uses_content_sample(
    tmp_path: Path, filename: str, content: bytes, expected_binary: bool
) -> None:
    path = tmp_path / filename
    path.write_bytes(content)

    assert is_binary_file(path) is expected_binary
    assert is_text_file(path) is (not expected_binary)


def test_binary_detection_reads_only_initial_sample(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * 2_000_000 + b"\x00")

    assert is_binary_file(path, sample_size=1_024) is False


def test_binary_detection_rejects_invalid_sample_size(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("text", encoding="utf-8")

    with pytest.raises(ValueError, match="sample_size"):
        is_binary_file(path, sample_size=0)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", hashlib.sha256(b"").hexdigest()),
        (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
        ("данные".encode(), hashlib.sha256("данные".encode()).hexdigest()),
    ],
)
def test_sha256_file_matches_known_digest(
    tmp_path: Path, content: bytes, expected: str
) -> None:
    path = tmp_path / "content"
    path.write_bytes(content)

    assert sha256_file(path, chunk_size=2) == expected


def test_sha256_streams_large_file(tmp_path: Path) -> None:
    content = ("large UTF-8 text — " * 100_000).encode()
    path = tmp_path / "large.txt"
    path.write_bytes(content)

    assert sha256_file(path, chunk_size=1_024) == hashlib.sha256(content).hexdigest()


def test_sha256_rejects_invalid_chunk_size(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        sha256_file(tmp_path / "unused", chunk_size=0)
