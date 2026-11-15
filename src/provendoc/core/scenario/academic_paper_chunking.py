"""Academic-paper chunking with protected-region atomicity (v1.5).

Splits paper text into chunks ON LINE BOUNDARIES ONLY, mirroring
``invoice_chunking`` (prereg: ``specs/academic_paper_chunk_atomicity_prereg_v1.md``).
The paper-specific rule is ATOMICITY of protected regions:

* a display-math block (a contiguous run of ``formula_lines``) is
  never split across a chunk boundary;
* a References entry (a blank-line-separated run inside the
  references region) is never split across a chunk boundary.

Strategy (greedy, same skeleton as the invoice chunker):

* accumulate lines up to ``max_chars``;
* prefer cutting after a blank line (soft boundary) when the cut is
  legal;
* if the candidate cut would split a protected region, the cut is
  pushed FORWARD to that region's last line — the region joins the
  current chunk whole (hard boundary extension);
* the next chunk starts ``overlap_lines`` lines back; overlap may
  duplicate whole regions, which is by design, never a split.

Dataclasses are REUSED from ``invoice_chunking`` (single Chunk
source of truth); ``check_chunk_invariants`` (coverage + byte
integrity) applies unchanged.

Honest boundaries:

* a region whose own size exceeds ``max_chars`` becomes its own
  chunk — never character-split; counted in stats;
* validity of a cut is defined as: no region [s, e] with
  s <= cut < e. By induction every chunk START also avoids landing
  mid-region (each accepted cut is valid, and the next start is at
  or before that cut);
* the region inputs are LEDGER FACTS (formula_lines located by the
  dqr locator, references entries produced by the dqr module) —
  this chunker consumes facts, it never re-derives them.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from provendoc.core.scenario.invoice_chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_LINES,
    Chunk,
    ChunkResult,
)

Region = tuple[int, int]  # 1-based closed [start_line, end_line]


def normalize_regions(
    formula_lines: Iterable[int],
    references_entries: Iterable[Region | Sequence[int]],
) -> tuple[Region, ...]:
    """Merge formula-line runs + reference entries into sorted regions.

    Contiguous/adjacent inputs are coalesced (two back-to-back
    formula runs are one block); overlapping inputs are merged.
    Non-positive or inverted inputs are dropped silently — ledger
    coordinates are trusted to be sane upstream (invariants check
    that separately).
    """
    raw: list[Region] = []
    prev: int | None = None
    for line_no in sorted(set(formula_lines)):
        if isinstance(line_no, int) and line_no > 0:
            if prev is not None and line_no == prev + 1 and raw:
                raw[-1] = (raw[-1][0], line_no)
            else:
                raw.append((line_no, line_no))
            prev = line_no
    for entry in references_entries:
        if isinstance(entry, Sequence) and not isinstance(entry, str):
            entry = tuple(entry)
        if (
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], int)
            and isinstance(entry[1], int)
            and 0 < entry[0] <= entry[1]
        ):
            raw.append((entry[0], entry[1]))
    raw.sort()
    merged: list[Region] = []
    for start, end in raw:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def chunk_academic_paper_text(
    text: str,
    formula_lines: Iterable[int] = (),
    references_entries: Iterable[Region | Sequence[int]] = (),
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> ChunkResult:
    """Split paper text into chunks with region atomicity.

    Pure function; no I/O; empty text yields zero chunks. See the
    module docstring for the cut rule and honest boundaries.
    """
    lines = text.split("\n")
    n = len(lines)
    stats = {
        "chunk_count": 0,
        "oversize_single_lines": 0,
        "protected_regions": 0,
        "extended_cuts": 0,
        "oversized_regions": 0,
    }
    if n == 0 or (n == 1 and not lines[0]):
        return ChunkResult(chunks=(), stats=stats)

    regions = normalize_regions(formula_lines, references_entries)
    stats["protected_regions"] = len(regions)
    # line number -> that region's end line (for O(1) cut checks)
    block_end: dict[int, int] = {}
    for start, end in regions:
        for line_no in range(start, min(end, n) + 1):
            block_end[line_no] = end
    for start, end in regions:
        region_chars = sum(len(lines[k]) + 1 for k in range(start - 1, min(end, n)))
        if region_chars > max_chars:
            stats["oversized_regions"] += 1

    def _valid_cut(line_no: int) -> bool:
        """Cut AFTER ``line_no`` is legal ⇔ it splits no region."""
        end = block_end.get(line_no)
        return end is None or end <= line_no

    chunks: list[Chunk] = []
    start = 0  # 0-based index of the current chunk's first line
    while start < n:
        end = start
        size = 0
        while end < n:
            add = len(lines[end]) + 1
            if size + add > max_chars and end > start:
                break
            size += add
            end += 1
        if end == start + 1 and size > max_chars:
            stats["oversize_single_lines"] += 1
        cut = end  # 0-based exclusive == 1-based last-included line
        if cut < n:
            # prefer the latest legal blank-line boundary in-window
            for i in range(end - 1, start, -1):
                if not lines[i - 1].strip() and _valid_cut(i):
                    cut = i
                    break
            if not _valid_cut(cut):
                # hard extension: carry the split region whole
                extended = min(block_end[cut], n)
                if extended > cut:
                    stats["extended_cuts"] += 1
                    cut = extended
        chunks.append(Chunk(
            start_line=start + 1, end_line=cut,
            text="\n".join(lines[start:cut]),
        ))
        stats["chunk_count"] += 1
        if cut >= n:
            break
        start = max(start + 1, cut - overlap_lines)
    return ChunkResult(chunks=tuple(chunks), stats=stats)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_OVERLAP_LINES",
    "Chunk",
    "ChunkResult",
    "chunk_academic_paper_text",
    "normalize_regions",
]
