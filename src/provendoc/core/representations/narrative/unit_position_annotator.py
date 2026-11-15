# ============================================================
# UNIT POSITION ANNOTATOR (shared: txt / markdown)
# ============================================================
"""Source-position provenance (line/char/byte) for base-granularity
RepresentationUnits.

Every base unit carries line/char/byte positions (basis
``document_text_utf8``), the source file's sha256, and a continuation
marker for size-degradation slices. Shared module consumed by both
the txt and markdown layers (single source of truth).

Location is a best-effort anchor-line bracketing: units are produced
in document order, and a monotonic line cursor disambiguates repeated
lines; fence separator lines added by the representation layer (bare
code / ASCII art wrapping) are not used as anchors. Units that cannot
be located get no position fields — one bad unit does not sink the
whole batch.
"""

from dataclasses import replace as _dc_replace


def annotate_unit_positions(units, document):
    """Locate each base unit in document.text and stamp positions.

    Coordinate conventions:
    - ``source_context``: line_start/line_end (1-based) written via a
      whole-dataclass replace — shared instances must be replaced
      wholesale; in-place mutation would leak into other units;
    - ``metadata``: char_start/char_end/byte_start/byte_end
      (half-open, UTF-8 byte counts) + positions_basis +
      source_sha256 (when present) + continuation (only for
      size_split slices).
    """
    text = document.text or ""
    if not text:
        return
    lines = text.split("\n")
    line_starts = [0]
    byte_starts = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)
        byte_starts.append(byte_starts[-1] + len(ln.encode("utf-8")) + 1)
    source_sha256 = (document.metadata or {}).get("sha256")

    cursor = 0
    for unit in units:
        try:
            located = locate_unit(unit, lines, cursor)
            if located is None:
                continue
            start, end = located
            col = len(lines[start]) - len(lines[start].lstrip())
            char_start = line_starts[start] + col
            char_end = line_starts[end] + len(lines[end])
            byte_start = byte_starts[start] + len(
                lines[start][:col].encode("utf-8")
            )
            byte_end = byte_starts[end] + len(
                lines[end].encode("utf-8")
            )
            # source_context may be one instance shared across several
            # units — it must be replaced wholesale; in-place mutation
            # would leak into other units.
            unit.source_context = _dc_replace(
                unit.source_context,
                line_start=start + 1,
                line_end=end + 1,
            )
            unit.metadata["char_start"] = char_start
            unit.metadata["char_end"] = char_end
            # byte offsets never overwrite existing keys: upgraded
            # embedded-code units already carry the parser-stage
            # byte_start/byte_end of the inner (inside-fence) source
            # (embedded_code_blocks evidence) — semantically different
            # from this layer's whole-block span (fence lines
            # included); evidence wins, and the inner span cannot be
            # losslessly recovered from the chunk span. txt has no
            # such pre-existing keys, so this guard never fires there.
            if "byte_start" not in unit.metadata:
                unit.metadata["byte_start"] = byte_start
            if "byte_end" not in unit.metadata:
                unit.metadata["byte_end"] = byte_end
            unit.metadata["positions_basis"] = "document_text_utf8"
            if source_sha256:
                unit.metadata["source_sha256"] = source_sha256
            if unit.metadata.get("size_split"):
                unit.metadata["continuation"] = True
            # Advance the cursor past the last line: unit contents are
            # disjoint, and only a monotonic cursor disambiguates
            # repeated lines (the second unit of a repeated paragraph
            # must not re-anchor to the first unit's lines).
            cursor = end + 1
        except Exception:  # pragma: no cover - defensive guard
            continue


def locate_unit(unit, lines, cursor):
    """Find the unit's (start_line, end_line) 0-based span, or None."""
    raw_lines = unit.text.split("\n")
    anchors = [ln.strip() for ln in raw_lines if ln.strip()]
    # Fence separator lines are not anchors: the ``` wrapping bare
    # code / ASCII art is added by the representation layer and absent
    # from the source; source-native fence lines are located via their
    # content lines.
    if anchors and anchors[0].startswith(("```", "~~~")):
        anchors = anchors[1:]
    if anchors and anchors[-1].startswith(("```", "~~~")):
        anchors = anchors[:-1]
    if not anchors:
        return None

    start = None
    for needle in anchors[:3]:
        for i in range(cursor, len(lines)):
            if lines[i].strip() == needle:
                start = i
                break
        if start is not None:
            break
    if start is None:
        return None

    # Search the last line forward from start + anchor count - 1
    # (the span never shrinks inside the unit); if not found, fall
    # back to the first match from start, then to start itself.
    min_end = start + len(anchors) - 1
    end = None
    for needle in reversed(anchors[-3:]):
        for j in range(min_end, len(lines)):
            if lines[j].strip() == needle:
                end = j
                break
        if end is not None:
            break
    if end is None:
        for needle in reversed(anchors[-3:]):
            for j in range(start, len(lines)):
                if lines[j].strip() == needle:
                    end = j
                    break
            if end is not None:
                break
    if end is None:
        end = start
    return start, end
