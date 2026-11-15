"""Email chunking with protected-region atomicity (v1).

Prereg: ``specs/email_scenario_prereg_v1_draft.md`` §2. Splits
email text into chunks ON LINE BOUNDARIES ONLY, mirroring the
invoice/academic chunkers' greedy skeleton. The email-specific
rule is ATOMICITY of protected regions:

* the header block (header start .. header/body boundary - 1,
  folded continuation lines included) is never split;
* a quote block (attribution hat + ``>`` run) is never split;
* the signature block (RFC 3676 separator .. block end) is never
  split.

Strategy (same skeleton as ``invoice_chunking`` /
``academic_paper_chunking``):

* accumulate lines up to ``max_chars``;
* prefer cutting after a blank line (soft boundary, a paragraph
  break) when the cut is legal;
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
* a text with NO blank line (degenerate, header/body boundary
  None) protects the whole file as one header region — one
  oversize chunk is the honest projection, never a silent split;
* the region inputs are LEDGER FACTS (located by
  ``email_scenario_dqr``) — this chunker consumes facts, it never
  re-derives them.
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


def normalize_email_regions(
    regions: Iterable[Region | Sequence[int]],
) -> tuple[Region, ...]:
    """Sort + coalesce protected regions (adjacent/overlap merge).

    Non-positive or inverted inputs are dropped silently — ledger
    coordinates are trusted to be sane upstream (the invariants
    check covers that separately).
    """
    raw: list[Region] = []
    for region in regions:
        if isinstance(region, Sequence) and not isinstance(region, str):
            region = tuple(region)
        if (
            isinstance(region, tuple)
            and len(region) == 2
            and isinstance(region[0], int)
            and isinstance(region[1], int)
            and 0 < region[0] <= region[1]
        ):
            raw.append((region[0], region[1]))
    raw.sort()
    merged: list[Region] = []
    for start, end in raw:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def chunk_email_text(
    text: str,
    protected_regions: Iterable[Region | Sequence[int]] = (),
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> ChunkResult:
    """Split email text into chunks with region atomicity.

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

    regions = normalize_email_regions(protected_regions)
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
    "chunk_email_text",
    "normalize_email_regions",
]
