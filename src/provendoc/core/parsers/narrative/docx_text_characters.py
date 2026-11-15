"""Evidence-only extraction of DOCX character/encoding/run-direction facts.

B8 scope (gap register DOCX §4): RTL text, CJK and East-Asian fonts,
mixed-language runs, soft hyphens, non-breaking spaces, tabs / manual
breaks / soft returns, automatic hyphenation, missing fonts, and symbol
fonts (Wingdings & friends).

Everything is evidence.  The narrative text is never rewritten here:
python-docx already projects ``w:tab``/``w:br``/``w:cr`` into the run
text, while ``w:softHyphen`` (automatic hyphenation) and ``w:sym``
(symbol-font characters) are invisible to it — those are counted and
sampled instead of being guessed into text.  Whether a referenced font
is actually installed on the reading machine cannot be verified from the
package, so only "referenced" vs "embedded" is reported.
"""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from docx.oxml.ns import qn

from provendoc.core.foundation.script_stats import (
    detect_scripts,
    script_profile,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_FONT_TABLE_PATH = "word/fontTable.xml"
_FONTS_PART_PREFIX = "word/fonts/"

# Fonts whose code points are symbol dingbats rather than text.
SYMBOL_FONT_NAMES = {
    "Wingdings",
    "Wingdings 2",
    "Wingdings 3",
    "Webdings",
    "Symbol",
    "Marlett",
    "MS Outlook",
    "MT Extra",
    "Bookshelf Symbol 1",
    "Bookshelf Symbol 2",
    "Bookshelf Symbol 3",
    "Bookshelf Symbol 4",
    "Bookshelf Symbol 5",
    "Bookshelf Symbol 6",
    "Bookshelf Symbol 7",
}

_MAX_SAMPLES = 10

# w:br/@w:type values with their evidence label; absent attribute means a
# plain text-wrapping (soft) line break.
_BREAK_TYPES = ("", "textWrapping", "page", "column")


def extract_text_character_summary(doc, file_path: str | Path) -> dict[str, Any]:
    """Collect character/encoding/direction evidence from one opened docx."""
    body = doc.element.body

    tab_count = 0
    br_by_type: Counter[str] = Counter()
    cr_count = 0
    soft_hyphen_count = 0
    no_break_hyphen_count = 0
    nbsp_count = 0
    sym_entries: list[dict[str, str]] = []
    rtl_run_count = 0
    rtl_samples: list[str] = []
    symbol_font_runs: Counter[str] = Counter()
    mixed_script_run_count = 0
    mixed_script_samples: list[str] = []
    fonts_referenced: Counter[str] = Counter()
    bidi_paragraph_count = 0
    text_parts: list[str] = []

    for paragraph in body.iter(qn("w:p")):
        if paragraph.find(qn("w:pPr")) is not None:
            if paragraph.find(qn("w:pPr")).find(qn("w:bidi")) is not None:
                bidi_paragraph_count += 1

        for run in paragraph.findall(qn("w:r")):
            run_text_parts: list[str] = []
            is_rtl_run = False
            symbol_font: str | None = None

            r_pr = run.find(qn("w:rPr"))
            if r_pr is not None:
                if r_pr.find(qn("w:rtl")) is not None:
                    is_rtl_run = True
                r_fonts = r_pr.find(qn("w:rFonts"))
                if r_fonts is not None:
                    for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                        value = r_fonts.get(qn(attribute))
                        if value:
                            fonts_referenced[value] += 1
                            if value in SYMBOL_FONT_NAMES:
                                symbol_font = value

            for child in run:
                tag = child.tag
                if tag == qn("w:t"):
                    value = child.text or ""
                    run_text_parts.append(value)
                    nbsp_count += value.count("\u00A0")
                elif tag == qn("w:tab"):
                    tab_count += 1
                    run_text_parts.append("\t")
                elif tag == qn("w:br"):
                    br_type = child.get(qn("w:type")) or ""
                    br_by_type[br_type] += 1
                elif tag == qn("w:cr"):
                    cr_count += 1
                elif tag == qn("w:softHyphen"):
                    soft_hyphen_count += 1
                elif tag == qn("w:noBreakHyphen"):
                    no_break_hyphen_count += 1
                elif tag == qn("w:sym"):
                    if len(sym_entries) < _MAX_SAMPLES:
                        sym_entries.append(
                            {
                                "font": child.get(qn("w:font")) or "",
                                "char": child.get(qn("w:char")) or "",
                            }
                        )

            run_text = "".join(run_text_parts)
            if run_text:
                text_parts.append(run_text)
            if is_rtl_run:
                rtl_run_count += 1
                if run_text and len(rtl_samples) < _MAX_SAMPLES:
                    rtl_samples.append(run_text[:60])
            if symbol_font and run_text:
                symbol_font_runs[symbol_font] += 1
            if run_text:
                scripts = detect_scripts(run_text)
                if len(scripts) >= 2:
                    mixed_script_run_count += 1
                    if len(mixed_script_samples) < _MAX_SAMPLES:
                        mixed_script_samples.append(run_text[:60])

    full_text = "\n".join(text_parts)
    profile = script_profile(full_text)
    scripts = detect_scripts(full_text)
    fonts_embedded = _embedded_fonts(file_path)
    font_aliases = _font_aliases(file_path)

    summary: dict[str, Any] = {
        "tab_count": tab_count,
        "break_counts": dict(br_by_type),
        "cr_count": cr_count,
        "soft_hyphen_count": soft_hyphen_count,
        "no_break_hyphen_count": no_break_hyphen_count,
        "nbsp_count": nbsp_count,
        "sym_count": len(sym_entries),
        "sym_samples": sym_entries,
        "rtl_run_count": rtl_run_count,
        "rtl_run_samples": rtl_samples,
        "bidi_paragraph_count": bidi_paragraph_count,
        "symbol_font_runs": dict(symbol_font_runs),
        "mixed_script_run_count": mixed_script_run_count,
        "mixed_script_run_samples": mixed_script_samples,
        "fonts_referenced": sorted(fonts_referenced),
        "fonts_referenced_counts": dict(fonts_referenced),
        "fonts_embedded": fonts_embedded,
        "fonts_referenced_without_embed": sorted(
            set(fonts_referenced) - set(fonts_embedded)
        ),
        "char_profile": profile,
        "scripts": scripts,
    }
    # Write only when non-empty (additive-only): documents with no
    # w:altName declaration get zero new keys.
    if font_aliases:
        summary["font_aliases"] = font_aliases
    return summary


def _embedded_fonts(file_path: str | Path) -> list[str]:
    """Font names with an embedded font file, from ``word/fontTable.xml``.

    A font referenced but not embedded renders differently on machines
    without it; whether the reader has the font installed locally cannot
    be known from the package, so only embedding is reported.
    """
    try:
        with zipfile.ZipFile(file_path) as package:
            if _FONT_TABLE_PATH not in package.namelist():
                return []
            root = ET.fromstring(package.read(_FONT_TABLE_PATH))
    except Exception:  # Evidence gathering is never fatal.
        return []

    embedded: list[str] = []
    for font in root.findall(f"{{{_W_NS}}}font"):
        name = font.get(f"{{{_W_NS}}}name")
        if not name:
            continue
        has_embedded_part = (
            font.find(f"{{{_W_NS}}}embedRegular") is not None
            or font.find(f"{{{_W_NS}}}embedBold") is not None
            or font.find(f"{{{_W_NS}}}embedItalic") is not None
            or font.find(f"{{{_W_NS}}}embedBoldItalic") is not None
        )
        if has_embedded_part:
            embedded.append(name)
    return sorted(set(embedded))


def _font_aliases(file_path: str | Path) -> dict[str, list[str]]:
    """w:altName font aliases from ``word/fontTable.xml``, verbatim.

    r25 face-2 pending-adjudication DOCX altName: a font's alias
    declaration gives the same font a second (by default invisible)
    name — on the record it is declaration-side evidence; values
    are recorded verbatim (name → [alias...]), and only a
    non-empty mapping is returned (the caller decides from that
    whether to write the new key).
    """
    try:
        with zipfile.ZipFile(file_path) as package:
            if _FONT_TABLE_PATH not in package.namelist():
                return {}
            root = ET.fromstring(package.read(_FONT_TABLE_PATH))
    except Exception:  # Evidence gathering is never fatal.
        return {}

    aliases: dict[str, list[str]] = {}
    for font in root.findall(f"{{{_W_NS}}}font"):
        name = font.get(f"{{{_W_NS}}}name")
        if not name:
            continue
        values = [
            node.get(f"{{{_W_NS}}}val") or ""
            for node in font.findall(f"{{{_W_NS}}}altName")
        ]
        values = [value for value in values if value]
        if values:
            aliases[name] = values
    return aliases
