# -*- coding: utf-8 -*-
"""Memoized paragraph-style resolution shared across docx feature modules.

python-docx resolves ``paragraph.style`` by scanning the styles part on
EVERY access — ``styles.get_by_id`` for a ``w:pStyle`` id, or
``styles.default_for`` (a scan of ALL styles) for styleless paragraphs.
On a pathologically large styles.xml (apache-poi bug65649: 2.5MB /
2672 styles) that turns per-paragraph feature passes (bookmarks,
numbering lookup, style-chain reports) into minutes of pure rescanning.

Resolution is deterministic per (document part, styleId): one part ==
one styles.xml, and ``w:pStyle`` ids are unique within it.  So cache
per document part via WeakKeyDictionary — when the package is dropped
the memo goes with it, no cross-document leakage.

Callers: docx_parser._style_of, docx_fields._extract_bookmarks /
paragraph_numref, docx_structure.extract_style_chains.
"""

from weakref import WeakKeyDictionary

from docx.oxml.ns import qn

_W_PSTYLE = qn("w:pStyle")
_W_VAL = qn("w:val")

# document part -> {styleId|None: style object|None}
_part_memos: "WeakKeyDictionary[type, dict]" = WeakKeyDictionary()


def paragraph_style_id(paragraph):
    """Paragraph's explicit ``w:pStyle`` id (None = no explicit style).

    Read straight off the paragraph XML — O(1), unlike
    ``paragraph.style`` which resolves (and scans) the styles part.
    """
    pPr = paragraph._p.pPr
    if pPr is None:
        return None
    node = pPr.find(_W_PSTYLE)
    if node is None:
        return None
    return node.get(_W_VAL)


def paragraph_style(paragraph):
    """Resolved style object for ``paragraph``, memoized per part+styleId.

    Semantics match ``paragraph.style`` exactly (default style for
    styleless paragraphs, None when resolution fails); only the
    repetition of the underlying scan is eliminated.  First resolution
    per distinct style pays one scan; the rest are dictionary hits.
    """
    part = paragraph.part
    memo = _part_memos.get(part)
    if memo is None:
        memo = _part_memos[part] = {}
    key = paragraph_style_id(paragraph)
    if key in memo:
        return memo[key]
    try:
        style = paragraph.style
    except Exception:
        style = None
    memo[key] = style
    return style
