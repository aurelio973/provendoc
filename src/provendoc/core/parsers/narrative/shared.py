"""Shared small utilities for narrative parsers.

- ``CFB_MAGIC``: the OLE2 Compound File Binary container magic.
  Legacy binary containers renamed to OOXML/ODF extensions (old
  .doc/.ppt/.sdw) and MS-OFFCRYPTO encrypted packages both start
  with it — they can never be opened as zip; each parser classifies
  and leaves evidence by it before failing honestly; decryption
  never happens.
- ``localname``: strip the ``{ns}`` namespace prefix from an
  ElementTree tag; shared by the conservative XML walks of
  epub/pptx.
- ``markdown_table_lines``: ragged cell matrix → markdown table
  lines (equal-width padding + a separator line after the first
  row); shared by the table rendering of epub/pptx; the caller
  is responsible for pipe-escaping cells.
"""

from __future__ import annotations

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def localname(tag: str) -> str:
    """Local name of an ElementTree tag (``{ns}`` prefix stripped)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def markdown_table_lines(rows: list[list[str]]) -> list[str]:
    """Ragged cell matrix → markdown table lines.

    Pads rows to equal width and emits the ``|---|`` separator after
    the first row.  Cells must already be pipe-escaped by the caller.
    """
    if not rows:
        return []
    width = max(len(r) for r in rows)
    out = []
    for i, r in enumerate(rows):
        r = r + [""] * (width - len(r))
        out.append("| " + " | ".join(r) + " |")
        if i == 0:
            out.append("|" + "---|" * width)
    return out


__all__ = ["CFB_MAGIC", "localname", "markdown_table_lines"]
