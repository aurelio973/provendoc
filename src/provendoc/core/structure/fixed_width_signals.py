# ============================================================
# core/structure/fixed_width_signals.py
# ============================================================
"""Fixed-width table candidate signals (observation-only registration; text is
never re-flowed).

Rules:

- Register candidates only (line ranges / column spans / header & separator
  line suspicion / confidence); ``Document.text`` and existing observation
  keys are never touched.
- Columns are decided by **universal blank columns**: a character position
  blank in every grid line (space / tab / past end of line) is a column
  separator; an internal universal blank run of width >= 2 is a gutter, and
  content runs between gutters are columns. This naturally handles both
  left-aligned and right-aligned (numeric) columns; a ragged last column
  imposes no right-gutter requirement.
- Two width bases: ``char`` (str index) first; on failure try
  ``display_width`` (east_asian_width W/F counts as 2). Whichever succeeds is
  registered as ``width_basis``.
- Caps MAX_TABLES / MAX_COLUMNS / MAX_ROWS (bounded evidence; overrun is
  truncated and registered); fewer than 3 consecutive grid lines or fewer
  than 2 internal gutters → no registration (prefer missing evidence over
  fabricated evidence).
"""

import re
import unicodedata

EXTRACTOR_ID = "fixed_width_signals_v1"

MAX_TABLES = 20
MAX_COLUMNS = 32
MAX_ROWS = 200
MIN_GRID_ROWS = 3
MIN_GUTTERS_PER_LINE = 2
MIN_GUTTER_WIDTH = 2

# gutter: >= 2 consecutive spaces, or >= 1 tab.
_GUTTER_RE = re.compile(r"(?: {2,}|\t+)")
# separator line: only -=_ and spaces, with >= 3 consecutive -=_.
_SEPARATOR_RUN_RE = re.compile(r"[-=_]{3,}")


def _is_separator_line(line):
    stripped = line.strip()
    if not stripped:
        return False
    if any(ch not in "-=_ " for ch in stripped):
        return False
    return _SEPARATOR_RUN_RE.search(stripped) is not None


def _gutter_count(line):
    return len(_GUTTER_RE.findall(line))


def _blank_flags(line, basis):
    """Per-position blank flags. Returns (flags, width):

    flags[p] is True when position p is blank (space / tab). Positions
    >= width (past end of line) count as blank in the universal decision;
    callers pad to max_width.
    """
    if basis == "char":
        return [ch in " \t" for ch in line], len(line)
    # display_width: W/F counts as 2 columns.
    flags = []
    for ch in line:
        width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        flags.extend([ch in " \t"] * width)
    return flags, len(flags)


def _column_spans(grid_lines, basis):
    """Universal blank column decision. Returns a list of column spans
    [(start, end), ...] on success, None on failure. Coordinates are
    positions under the given basis (half-open)."""
    flags_list = []
    max_width = 0
    for line in grid_lines:
        flags, width = _blank_flags(line, basis)
        flags_list.append(flags)
        max_width = max(max_width, width)
    if max_width == 0:
        return None
    universal = []
    for pos in range(max_width):
        universal.append(all(pos >= len(flags) or flags[pos] for flags in flags_list))
    # Internal universal blank runs (width >= MIN_GUTTER_WIDTH) are gutters;
    # edge-adjacent runs (leading indent / ragged end) are not separators.
    gutters = []
    pos = 0
    while pos < max_width:
        if universal[pos]:
            end = pos
            while end < max_width and universal[end]:
                end += 1
            if pos > 0 and end < max_width and end - pos >= MIN_GUTTER_WIDTH:
                gutters.append((pos, end))
            pos = end
        else:
            pos += 1
    if len(gutters) < 2:
        return None
    spans = []
    prev = 0
    for start, end in gutters:
        spans.append((prev, start))
        prev = end
    spans.append((prev, max_width))
    if len(spans) > MAX_COLUMNS:
        return None
    return spans


def _evaluate_run(run):
    """Evaluate one consecutive in-grid run. run: [(line_no, line)] (1-based).

    Returns a table record dict, or None (not grid-qualified)."""
    grid = [(no, line) for no, line in run if not _is_separator_line(line)]
    if len(grid) < MIN_GRID_ROWS:
        return None
    has_separator = any(_is_separator_line(line) for _, line in run)
    for basis in ("char", "display_width"):
        spans = _column_spans([line for _, line in grid], basis)
        if spans is not None:
            break
    if spans is None:
        return None
    row_count = len(grid)
    data_rows = row_count - 1  # first row deducted as header candidate
    confidence = "high" if (has_separator or data_rows >= 4) else "medium"
    return {
        "line_start": run[0][0],
        "line_end": run[-1][0],
        "row_count": row_count,
        "col_count": len(spans),
        "columns": [{"char_start": start, "char_end": end} for start, end in spans],
        "has_header_candidate": has_separator,
        "has_separator": has_separator,
        "width_basis": basis,
        "confidence": confidence,
        "heading_overlap_lines": [],
    }


def detect_fixed_width_tables(
    text,
    max_tables=MAX_TABLES,
    max_rows=MAX_ROWS,
):
    """Detect fixed-width table candidates in body text (observation-only).

    Args:
        text: full document text (sanitized).
        max_tables: evidence cap; overrun is truncated and registered as
            ``truncated``.
        max_rows: per-table row cap; overrun is truncated and registered as
            ``row_truncated``.

    Returns:
        dict, or None when no candidates exist (non-empty-write-only rule).
    """
    tables = []
    truncated = False
    run = []

    def close_run():
        if not run:
            return
        block = run[:max_rows]
        row_truncated = len(run) > max_rows
        record = _evaluate_run(block)
        if record is not None:
            if row_truncated:
                record["row_truncated"] = True
            if len(tables) < max_tables:
                tables.append(record)
            else:
                nonlocal truncated
                truncated = True

    for line_no, line in enumerate(text.splitlines(), start=1):
        if _gutter_count(line) >= MIN_GUTTERS_PER_LINE or _is_separator_line(line):
            run.append((line_no, line))
        else:
            close_run()
            run = []
    close_run()

    if not tables:
        return None
    result = {
        "extractor": EXTRACTOR_ID,
        "tables": tables,
        "note": "candidacy only - rows never re-flowed, text never changed",
    }
    if truncated:
        result["truncated"] = True
    return result
