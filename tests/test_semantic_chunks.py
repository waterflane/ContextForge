from pathlib import Path

import pytest

from contextforge.intelligence import extract_code_map
from contextforge.intelligence.chunks import plan_source_chunks
from contextforge.repositories import scan_repository


@pytest.mark.parametrize(
    "source",
    [
        "const huge = '" + "Ж🙂" * 20_000 + "';\r\n",
        "function huge() {\n" + "  work();\n" * 12_000 + "}\n",
        "".join(f"function f{i}() {{ return {i}; }}\r\n" for i in range(4000)),
    ],
    ids=["long-utf8-line", "large-symbol", "many-symbols"],
)
def test_chunks_cover_source_without_utf8_splits(tmp_path: Path, source: str) -> None:
    (tmp_path / "large.ts").write_text(source, encoding="utf-8", newline="")
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    chunks, truncated = plan_source_chunks(source, code_map)
    assert not truncated
    assert len(chunks) > 1
    raw = source.encode("utf-8")
    covered = 0
    for chunk in chunks:
        assert chunk.start_byte <= covered < chunk.end_byte
        assert chunk.text.encode("utf-8") == raw[chunk.start_byte : chunk.end_byte]
        assert len(chunk.text.encode("utf-8")) <= 65_536
        covered = chunk.end_byte
    assert covered == len(raw)
    assert (chunks, truncated) == plan_source_chunks(source, code_map)


def test_chunk_cap_is_explicit_and_preserves_source_order(tmp_path: Path) -> None:
    source = "line contents\n" * 2000
    (tmp_path / "unknown.txt").write_text(source, encoding="utf-8")
    snapshot = scan_repository(tmp_path)
    code_map = extract_code_map(snapshot, snapshot.files[0])
    chunks, truncated = plan_source_chunks(
        source, code_map, max_bytes=128, max_chunks=64
    )
    assert truncated
    assert len(chunks) == 64
    assert chunks[0].start_byte == 0
    assert all(
        left.end_byte < right.end_byte
        for left, right in zip(chunks, chunks[1:], strict=False)
    )
    tail, _ = plan_source_chunks(
        source, code_map, max_bytes=128, start_byte=chunks[2].start_byte
    )
    assert tail[0].start_byte == chunks[2].start_byte
    with pytest.raises(ValueError, match="limits"):
        plan_source_chunks(source, code_map, max_bytes=3)
    with pytest.raises(ValueError, match="boundary"):
        plan_source_chunks(source, code_map, start_byte=-1)
