import hashlib
from pathlib import Path
from typing import Any

import pytest

from contextforge.repositories.files import (
    FileTooLargeError,
    deterministic_relative_path,
    inspect_file,
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


@pytest.mark.parametrize(
    "path", ["", ".", "..", "../secret", "/rooted", "C:/rooted", "C:relative"]
)
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


def test_single_pass_inspection_returns_consistent_text_metadata(
    tmp_path: Path,
) -> None:
    content = "Hello, мир!\n".encode()
    path = tmp_path / "text.txt"
    path.write_bytes(content)

    inspection = inspect_file(
        path,
        max_size_bytes=len(content),
        sample_size=2,
        chunk_size=3,
    )

    assert inspection.size_bytes == len(content)
    assert inspection.is_binary is False
    assert inspection.sha256 == hashlib.sha256(content).hexdigest()


def test_single_pass_inspection_enforces_size_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"12345")

    with pytest.raises(FileTooLargeError) as error:
        inspect_file(path, max_size_bytes=4)

    assert error.value.size_bytes == 5
    assert error.value.max_size_bytes == 4
    assert str(error.value) == "file size 5 exceeds limit 4"


def test_single_pass_inspection_stops_after_binary_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "binary.dat"
    content = b"\x00" + b"x" * 10_000
    path.write_bytes(content)
    original_open = Path.open
    bytes_read = 0

    class CountingReader:
        def __init__(self) -> None:
            self.file = original_open(path, "rb")

        def __enter__(self) -> "CountingReader":
            return self

        def __exit__(self, *args: object) -> None:
            self.file.close()

        def fileno(self) -> int:
            return self.file.fileno()

        def read(self, size: int) -> bytes:
            nonlocal bytes_read
            chunk = self.file.read(size)
            bytes_read += len(chunk)
            return chunk

    def count_reads(candidate: Path, *args: Any, **kwargs: Any) -> Any:
        if candidate == path and args and args[0] == "rb":
            return CountingReader()
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_reads)

    inspection = inspect_file(
        path,
        max_size_bytes=len(content),
        sample_size=8,
    )

    assert inspection.is_binary is True
    assert inspection.size_bytes == len(content)
    assert bytes_read == 8


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("max_size_bytes", 0), ("sample_size", 0), ("chunk_size", 0)],
)
def test_single_pass_inspection_rejects_invalid_limits(
    tmp_path: Path, keyword: str, value: int
) -> None:
    path = tmp_path / "text.txt"
    path.write_text("text", encoding="utf-8")
    arguments = {"max_size_bytes": 4, keyword: value}

    with pytest.raises(ValueError, match=keyword):
        inspect_file(path, **arguments)


def test_single_pass_inspection_rejects_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    replacement = tmp_path / "replacement.txt"
    path.write_text("original", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")
    original_open = Path.open

    def replace_before_open(candidate: Path, *args: Any, **kwargs: Any) -> Any:
        if candidate == path and args and args[0] == "rb":
            candidate.unlink()
            replacement.replace(candidate)
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)

    with pytest.raises(OSError, match="changed while being opened"):
        inspect_file(path, max_size_bytes=100)


def test_single_pass_inspection_rejects_non_regular_path(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(OSError, match="no longer a regular file"):
        inspect_file(directory, max_size_bytes=100)


@pytest.mark.parametrize(("sample_size", "grow_on_read"), [(8, 1), (2, 2)])
def test_single_pass_inspection_caps_growth_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_size: int,
    grow_on_read: int,
) -> None:
    path = tmp_path / "growing.txt"
    path.write_bytes(b"1234")
    original_open = Path.open

    class GrowingReader:
        def __init__(self) -> None:
            self.file = original_open(path, "rb")
            self.read_count = 0

        def __enter__(self) -> "GrowingReader":
            return self

        def __exit__(self, *args: object) -> None:
            self.file.close()

        def fileno(self) -> int:
            return self.file.fileno()

        def read(self, size: int) -> bytes:
            self.read_count += 1
            if self.read_count == grow_on_read:
                with original_open(path, "ab") as writer:
                    writer.write(b"5")
            return self.file.read(size)

    def grow_after_fstat(candidate: Path, *args: Any, **kwargs: Any) -> Any:
        if candidate == path and args and args[0] == "rb":
            return GrowingReader()
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_after_fstat)

    with pytest.raises(FileTooLargeError) as error:
        inspect_file(path, max_size_bytes=4, sample_size=sample_size)

    assert error.value.size_bytes == 5
