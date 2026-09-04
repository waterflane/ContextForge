"""Deterministic UTF-8 bounded source regions, independent of model providers."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .codemap import FileCodeMap, SourceRange


@dataclass(frozen=True, slots=True)
class SourceChunk:
    text: str
    source_range: SourceRange
    start_byte: int
    end_byte: int


def plan_source_chunks(
    source: str,
    code_map: FileCodeMap,
    *,
    max_bytes: int = 65_536,
    max_chunks: int = 64,
    overlap_lines: int = 8,
    start_byte: int = 0,
    max_symbols: int | None = None,
    required_symbol_ids: frozenset[str] | None = None,
) -> tuple[tuple[SourceChunk, ...], bool]:
    """Cover source in order; return whether the chunk cap left source uncovered."""
    if (
        max_bytes < 4
        or max_chunks < 1
        or overlap_lines < 0
        or (max_symbols is not None and max_symbols < 1)
    ):
        raise ValueError("invalid source chunk limits")
    raw = source.encode("utf-8")
    if not 0 <= start_byte <= len(raw) or (
        start_byte < len(raw) and raw[start_byte] & 0xC0 == 0x80
    ):
        raise ValueError("chunk start must be a UTF-8 boundary within source")
    line_starts = [0]
    for index, byte in enumerate(raw):
        if byte == 10:
            line_starts.append(index + 1)

    def offset(line: int, column: int) -> int:
        return min(len(raw), line_starts[min(line - 1, len(line_starts) - 1)] + column)

    def position(at: int) -> tuple[int, int]:
        row = bisect_right(line_starts, at) - 1
        return row + 1, at - line_starts[row]

    boundaries = {len(raw)}
    required_ranges: list[tuple[int, int]] = []
    for symbol in code_map.symbols:
        region = symbol.declaration_range
        boundaries.add(offset(region.start_line, region.start_column))
        boundaries.add(offset(region.end_line, region.end_column))
        if required_symbol_ids is None or symbol.symbol_id in required_symbol_ids:
            required_ranges.append(
                (
                    offset(region.start_line, region.start_column),
                    offset(region.end_line, region.end_column),
                )
            )
    ordered_boundaries = sorted(boundaries)
    result: list[SourceChunk] = []
    cursor = start_byte
    covered = start_byte
    while cursor < len(raw) and len(result) < max_chunks:
        ceiling = min(len(raw), cursor + max_bytes)
        boundary_index = bisect_right(ordered_boundaries, ceiling) - 1
        boundary = ordered_boundaries[boundary_index] if boundary_index >= 0 else 0
        if boundary > max(cursor, covered):
            end = boundary
        else:
            row = bisect_right(line_starts, ceiling) - 1
            end = line_starts[row]
            if end <= max(cursor, covered):
                end = ceiling
                while end < len(raw) and raw[end] & 0xC0 == 0x80:
                    end -= 1
        if max_symbols is not None:
            overlapping = [
                (start, stop)
                for start, stop in required_ranges
                if start < end and stop > cursor
            ]
            if len(overlapping) > max_symbols:
                possible = [
                    start
                    for start, _ in overlapping
                    if covered < start < end
                    and sum(left < start for left, _ in overlapping) <= max_symbols
                ]
                if possible:
                    end = max(possible)
        if end <= covered:
            cursor = covered
            continue
        start_line, start_column = position(cursor)
        end_line, end_column = position(end)
        if end_column == 0 and end_line > start_line:
            end_line -= 1
            end_column = end - line_starts[end_line - 1]
        result.append(
            SourceChunk(
                raw[cursor:end].decode("utf-8"),
                SourceRange(
                    start_line=start_line,
                    start_column=start_column,
                    end_line=end_line,
                    end_column=end_column,
                ),
                cursor,
                end,
            )
        )
        covered = end
        next_cursor = end
        if end not in boundaries and overlap_lines:
            row = bisect_right(line_starts, end) - 1
            overlap_start = line_starts[max(0, row - overlap_lines)]
            if cursor < overlap_start < end:
                next_cursor = overlap_start
        cursor = next_cursor
    return tuple(result), covered < len(raw)
