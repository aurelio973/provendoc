"""Invoice chunking + chunk-level invariants (prereg §1.D/E).

Splits (salvaged) invoice text into chunks ON LINE BOUNDARIES ONLY —
a character-level cut through an amount or a tax id would itself be
data corruption. Strategy (v1, conservative):

* greedy line accumulation up to ``max_chars``;
* the cut lands after a soft boundary (blank line or summary line)
  when one exists inside the accumulated window — structural
  boundaries beat byte arithmetic;
* the next chunk starts ``overlap_lines`` lines back (cross-chunk
  semantic continuity, the user-mandated fallback shape).

``check_chunk_invariants`` is INDEPENDENT of the chunker and applies
the project's line-level reconciliation discipline to chunks: every
original line must be covered by some chunk's line range (a gap is a
loss with the missing line numbers listed), and every chunk's text
must reproduce its original-text slice byte-for-byte (any divergence
is an integrity failure — chunks never rewrite).

Honest boundaries:

* one physical line longer than ``max_chars`` becomes its own chunk
  (never character-split); a report of this is in stats;
* overlap deliberately duplicates lines across chunks — duplication
  is recorded per line, not treated as an error;
* chunk-level semantics (an anchor staying inside one chunk) is v2:
  v1 guarantees coverage and integrity, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass

from provendoc.core.scenario.invoice_line_items import SUMMARY_LINE_RE

VERDICT_COVERED = "covered"
VERDICT_LOSS = "loss"

DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP_LINES = 1


@dataclass(frozen=True)
class Chunk:
    """One chunk: 1-based closed line range + verbatim text."""

    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    text: str

    @property
    def line_count(self) -> int:
        """Number of original lines this chunk spans."""
        return self.end_line - self.start_line + 1


@dataclass(frozen=True)
class ChunkResult:
    """Chunking output: all chunks + stats."""

    chunks: tuple[Chunk, ...]
    stats: dict[str, int]

    @property
    def chunk_count(self) -> int:
        """Number of chunks produced."""
        return len(self.chunks)


@dataclass(frozen=True)
class ChunkInvariantResult:
    """Invariant check outcome: verdict + full evidence."""

    verdict: str  # covered | loss
    missing_lines: tuple[int, ...]  # original lines covered by no chunk
    integrity_violations: tuple[str, ...]  # chunks that rewrite lines
    duplicate_lines: tuple[int, ...]  # lines covered more than once
    stats: dict[str, int]


def _is_soft_boundary(line: str) -> bool:
    """True when a chunk boundary may follow this line."""
    return not line.strip() or SUMMARY_LINE_RE.search(line) is not None


def chunk_invoice_text(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> ChunkResult:
    """Split invoice text into line-boundary chunks.

    Greedy accumulation up to ``max_chars``; prefers cutting after a
    soft boundary (blank / summary line); the next chunk rewinds
    ``overlap_lines`` lines. Empty text yields zero chunks. Pure
    function; no I/O.
    """
    lines = text.split("\n")
    n = len(lines)
    stats = {
        "chunk_count": 0,
        "oversize_single_lines": 0,
    }
    if n == 0 or (n == 1 and not lines[0]):
        return ChunkResult(chunks=(), stats=stats)

    chunks: list[Chunk] = []
    start = 0  # 0-based index of the first line of the current chunk
    while start < n:
        end = start
        size = 0
        while end < n:
            add = len(lines[end]) + 1  # line + newline allowance
            if size + add > max_chars and end > start:
                break
            size += add
            end += 1
        if end == start + 1 and size > max_chars:
            stats["oversize_single_lines"] += 1
        cut = end
        if cut < n:
            # prefer the latest soft boundary strictly inside the window
            for i in range(end - 1, start, -1):
                if _is_soft_boundary(lines[i - 1]):
                    cut = i
                    break
        chunks.append(Chunk(
            start_line=start + 1, end_line=cut,
            text="\n".join(lines[start:cut]),
        ))
        stats["chunk_count"] += 1
        if cut >= n:
            break
        start = max(start + 1, cut - overlap_lines)
    return ChunkResult(chunks=tuple(chunks), stats=stats)


def check_chunk_invariants(
    text: str, chunks: tuple[Chunk, ...]
) -> ChunkInvariantResult:
    """Reconcile chunks against the original text (line multiset).

    Coverage: every original line number must fall inside some
    chunk's range; gaps are losses with line numbers listed.
    Integrity: each chunk's text must equal its original slice
    byte-for-byte; a diverging chunk is an integrity violation.
    Overlap duplicates are reported per line, not an error.
    Pure function; no I/O.
    """
    lines = text.split("\n")
    n = len(lines) if text != "" else 0  # "".split() is a [``] artifact
    coverage: dict[int, int] = {}
    for chunk in chunks:
        for line_no in range(chunk.start_line, chunk.end_line + 1):
            coverage[line_no] = coverage.get(line_no, 0) + 1

    missing = tuple(
        sorted(set(range(1, n + 1)) - set(coverage))
    ) if n else ()
    duplicates = tuple(
        sorted(no for no, count in coverage.items() if count > 1)
    )

    violations: list[str] = []
    for idx, chunk in enumerate(chunks):
        lo = chunk.start_line - 1  # 0-based slice bounds
        hi = chunk.end_line
        if lo < 0 or hi > n or lo > hi:
            violations.append(
                f"chunk {idx}: range {chunk.start_line}-{chunk.end_line} "
                f"outside original 1-{n}"
            )
            continue
        expected = "\n".join(lines[lo:hi])
        if chunk.text != expected:
            violations.append(
                f"chunk {idx} (lines {chunk.start_line}-{chunk.end_line}): "
                f"text diverges from original slice"
            )

    verdict = VERDICT_COVERED if not missing and not violations else VERDICT_LOSS
    stats = {
        "original_lines": n,
        "covered_lines": len(coverage),
        "missing_lines": len(missing),
        "duplicate_lines": len(duplicates),
        "integrity_violations": len(violations),
    }
    return ChunkInvariantResult(
        verdict=verdict,
        missing_lines=missing,
        integrity_violations=tuple(violations),
        duplicate_lines=duplicates,
        stats=stats,
    )


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_OVERLAP_LINES",
    "Chunk",
    "ChunkInvariantResult",
    "ChunkResult",
    "VERDICT_COVERED",
    "VERDICT_LOSS",
    "check_chunk_invariants",
    "chunk_invoice_text",
]
