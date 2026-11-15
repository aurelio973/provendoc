"""Text/font/character evidence extraction (B8: encoding, fonts, RTL, CJK).

Everything here is evidence: no text is rewritten, no font is substituted,
and no reordering happens.  The extractor records the facts a consumer
needs to judge extraction quality for encoding-sensitive documents —

* font inventory with subset prefixes, embedding status, and /ToUnicode
  presence (the CMap that maps glyphs back to Unicode);
* text render mode 3 (invisible text layers, e.g. OCR overlays);
* line direction / writing-mode counts (rotated text, RTL lines, CJK
  vertical writing);
* a script/character-class profile (ligatures, PUA, combining marks,
  RTL scripts, CJK) shared with the DOCX parser via ``script_stats``;
* G9: overlong grapheme clusters — runs of >= 8 consecutive combining
  marks within a single span, a signature of broken /ToUnicode CMaps
  and adversarial content streams (recorded as evidence, never altered).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.foundation.script_stats import (
    detect_scripts,
    is_rtl_script,
    script_profile,
)
from provendoc.core.foundation.zero_width_runs import zero_width_runs
from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFExtractionResult,
    PDFPage,
)

# BaseFont subset prefix: six uppercase letters followed by "+", e.g.
# "ABCDEF+Helvetica" — a subsetted embedded font.
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

# Text render-mode operator in content streams, e.g. "3 Tr".
_RENDER_MODE_RE = re.compile(rb"(\d+)\s+Tr\b")

# Render-mode-3 span channel (P2, spec engineering principle 2): PyMuPDF surfaces
# invisible text with alpha == 0 in get_text("dict") spans.  The hidden
# text still enters the normal page text (existing behaviour unchanged);
# this additive channel separately records the per-page hidden text
# content so the pipeline can compare the hidden layer directly against
# the OCR layer (word-multiset, same as the native-vs-OCR comparison).
_INVISIBLE_ALPHA = 0
_MAX_INVISIBLE_CHARS_PER_PAGE = 65536
_MAX_INVISIBLE_PAGES = 500

# G9: overlong grapheme clusters.
# A run of >= 8 consecutive combining marks inside one span is
# pathological for narrative text: natural scripts (Devanagari,
# Arabic tashkeel, Hebrew niqqud, Thai) stack at most 3-4 marks on a
# single base.  Longer runs indicate a broken /ToUnicode CMap (thousands
# of marks from a single glyph), adversarial content, or font-encoding
# corruption.  We record them as evidence — detection only, no rewrite.
_CLUSTER_MIN_RUN = 8
# Cap samples to keep metadata bounded under adversarial input: at most
# 20 clusters recorded per page and 100 per document, and each sample
# string truncated to 80 characters so a pathological span cannot
# bloat the evidence metadata.
_MAX_CLUSTERS_PER_PAGE = 20
_MAX_CLUSTERS_PER_DOC = 100
_MAX_CLUSTER_SAMPLE_CHARS = 80

# Encodings that guarantee a deterministic glyph→Unicode mapping even
# without /ToUnicode; only fonts outside this set are flagged as risk.
_IMPLICIT_ENCODING_PREFIXES = ("WinAnsi", "MacRoman", "Standard", "PDFDoc", "ASCII")

# Font types whose text extraction depends on a CMap.
_CID_FONT_TYPES = ("Type0", "CIDFontType0", "CIDFontType2")


class TextCharacterExtractor:
    """Extract font/encoding/character evidence without altering any text."""

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Extract font/encoding/character-level evidence (text untouched)."""
        document = fitz.open(file_path)
        try:
            font_facts = self._font_facts(document)
            page_text_parts: list[str] = []
            actualtext_pages: list[int] = []
            actualtext_occurrences = 0
            invisible_text_pages: list[int] = []
            # P2 (engineering principle 2): per-page hidden-layer text content from
            # alpha==0 spans — additive channel, never alters page text.
            invisible_text_by_page: dict[int, dict[str, Any]] = {}
            invisible_pages_truncated = False
            rtl_lines = 0
            rotated_lines = 0
            vertical_lines = 0

            # G9: overlong grapheme clusters — runs of >= _CLUSTER_MIN_RUN
            # consecutive combining marks within a single span.  Detection
            # only: the extracted text is never rewritten or re-ordered.
            clusters_by_page: dict[int, list[dict[str, Any]]] = {}
            doc_cluster_count = 0
            doc_clusters_truncated = False

            for index, page in enumerate(document.pages()):
                page_number = index + 1
                raw = page.get_text("dict", sort=False)
                page_text: list[str] = []
                for block in raw.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        direction = line.get("dir", (1.0, 0.0))
                        if int(line.get("wmode", 0)) == 1:
                            vertical_lines += 1
                        elif float(direction[0]) < 0:
                            rtl_lines += 1
                        elif (
                            round(float(direction[0]), 3),
                            round(float(direction[1]), 3),
                        ) != (1.0, 0.0):
                            rotated_lines += 1
                        for span in line.get("spans", []):
                            span_text = str(span.get("text", ""))
                            page_text.append(span_text)
                            if (
                                span_text.strip()
                                and int(span.get("alpha", 255) or 0) == _INVISIBLE_ALPHA
                            ):
                                if len(invisible_text_by_page) >= _MAX_INVISIBLE_PAGES:
                                    invisible_pages_truncated = True
                                elif page_number not in invisible_text_by_page:
                                    hidden_chars = len(span_text)
                                    invisible_text_by_page[page_number] = {
                                        "text": span_text[:_MAX_INVISIBLE_CHARS_PER_PAGE],
                                        "char_count": hidden_chars,
                                        "truncated": (
                                            hidden_chars > _MAX_INVISIBLE_CHARS_PER_PAGE
                                        ),
                                    }
                                else:
                                    entry = invisible_text_by_page[page_number]
                                    if (
                                        len(entry["text"])
                                        < _MAX_INVISIBLE_CHARS_PER_PAGE
                                    ):
                                        entry["text"] = (
                                            entry["text"]
                                            + span_text
                                        )[:_MAX_INVISIBLE_CHARS_PER_PAGE]
                                    entry["char_count"] += len(span_text)
                                    entry["truncated"] = (
                                        entry["char_count"] > len(entry["text"])
                                    )

                            # G9: scan this span for overlong combining-mark
                            # runs.  Only spans containing at least one
                            # combining mark are scanned in full — a cheap
                            # pre-filter keeps the common case (plain Latin
                            # or CJK text) at zero extra cost.
                            if "\u0300" <= span_text[:1] or self._has_combining_mark(span_text):
                                for run_start, run_len in zero_width_runs(
                                    span_text, min_run=_CLUSTER_MIN_RUN
                                ):
                                    if doc_cluster_count >= _MAX_CLUSTERS_PER_DOC:
                                        doc_clusters_truncated = True
                                        break
                                    if len(clusters_by_page.get(page_number, [])) >= _MAX_CLUSTERS_PER_PAGE:
                                        doc_clusters_truncated = True
                                        break
                                    sample = span_text[run_start:run_start + _MAX_CLUSTER_SAMPLE_CHARS]
                                    cluster_record = {
                                        "page": page_number,
                                        "run_length": run_len,
                                        "span_start": run_start,
                                        "sample": sample,
                                        "sample_truncated": run_len > _MAX_CLUSTER_SAMPLE_CHARS,
                                    }
                                    clusters_by_page.setdefault(page_number, []).append(cluster_record)
                                    doc_cluster_count += 1
                        if doc_clusters_truncated:
                            break
                    if doc_clusters_truncated:
                        break
                if doc_clusters_truncated:
                    break
                page_text_parts.append("".join(page_text))
                try:
                    contents = page.read_contents()
                except Exception:  # Evidence gathering is never fatal.
                    contents = b""
                occurrences = contents.count(b"/ActualText")
                if occurrences:
                    actualtext_occurrences += occurrences
                    actualtext_pages.append(page_number)
                render_modes = {
                    int(match.group(1))
                    for match in _RENDER_MODE_RE.finditer(contents)
                }
                if 3 in render_modes:  # Render mode 3 = invisible text layer.
                    invisible_text_pages.append(page_number)

            full_text = "\n".join(page_text_parts)
            profile = script_profile(full_text)
            scripts = detect_scripts(full_text)
            # Replacement characters (U+FFFD) are the glyph-to-Unicode
            # failure signal (broken CMap / ToUnicode): the share of the
            # extracted text that carries no reliable Unicode mapping.
            replacement_char_count = full_text.count("\ufffd")
            replacement_char_ratio = (
                replacement_char_count / len(full_text) if full_text else 0.0
            )

            # G9: assemble the overlong-cluster evidence summary.  Count,
            # affected pages, worst-case run length, and bounded samples.
            all_clusters = [
                cluster
                for clusters in clusters_by_page.values()
                for cluster in clusters
            ]
            longest_run = max(
                (c["run_length"] for c in all_clusters), default=0
            )
            long_grapheme_clusters: dict[str, Any] = {
                "count": len(all_clusters),
                "pages": sorted(clusters_by_page.keys()),
                "longest_run": longest_run,
                "min_run_threshold": _CLUSTER_MIN_RUN,
                "truncated": doc_clusters_truncated,
                "samples": all_clusters[:_MAX_CLUSTERS_PER_DOC],
            }

            # Namespaced under a single key: unlike the image/table additive
            # extractors (flat "page_count"/"source_kind" keys), character
            # evidence must never collide with or overwrite existing
            # extraction metadata keys.
            metadata: dict[str, Any] = {
                "pdf_text_characters": {
                    "page_count": len(page_text_parts),
                    "fonts": font_facts["fonts"],
                    "font_count": len(font_facts["fonts"]),
                    "subset_font_count": font_facts["subset_font_count"],
                    "non_embedded_font_count": font_facts["non_embedded_font_count"],
                    "tounicode_missing_fonts": font_facts["tounicode_missing_fonts"],
                    "tounicode_missing_risk": font_facts["tounicode_missing_risk"],
                    "actualtext_pages": actualtext_pages,
                    "actualtext_occurrences": actualtext_occurrences,
                    "invisible_text_pages": invisible_text_pages,
                    "invisible_text_by_page": (
                        {
                            str(page): entry
                            for page, entry in sorted(invisible_text_by_page.items())
                        }
                    ),
                    "invisible_text_pages_truncated": invisible_pages_truncated,
                    "invisible_detection_note": (
                        "two verbatim channels: invisible_text_pages lists pages "
                        "whose content stream declares render mode 3 (regex); "
                        "invisible_text_by_page records the hidden text content "
                        "from alpha==0 spans. The channels may disagree — a "
                        "declared render mode with no drawn glyphs is regex-only; "
                        "both facts are kept as-is"
                    ),
                    "long_grapheme_clusters": long_grapheme_clusters,
                    "rtl_line_count": rtl_lines,
                    "rotated_line_count": rotated_lines,
                    "vertical_line_count": vertical_lines,
                    "char_profile": profile,
                    "replacement_char_count": replacement_char_count,
                    "replacement_char_ratio": round(replacement_char_ratio, 4),
                    "scripts": scripts,
                    "rtl_scripts_present": any(
                        is_rtl_script(name) for name in scripts
                    ),
                }
            }
            return PDFExtractionResult(
                pages=[],
                extraction_method="text_characters",
                metadata=metadata,
            )
        finally:
            document.close()

    @staticmethod
    def _has_combining_mark(text: str) -> bool:
        """Whether ``text`` contains any combining mark (Mn/Mc/Me).

        Cheap pre-filter for the G9 cluster scan: spans without any
        combining mark cannot contain an overlong mark run, so they
        skip the full regex scan entirely.
        """
        for char in text:
            if (
                "\u0300" <= char <= "\u036f"
                or "\u0483" <= char <= "\u05bd"
                or "\u064b" <= char <= "\u065f"
                or "\u0900" <= char <= "\u0903"
                or "\u093a" <= char <= "\u094d"
                or "\u0951" <= char <= "\u0957"
                or "\u0e31" <= char <= "\u0e3a"
                or "\u0eb1" <= char <= "\u0ebc"
                or "\u1ab0" <= char <= "\u1aff"
                or "\u1dc0" <= char <= "\u1dff"
                or "\u20d0" <= char <= "\u20f0"
                or "\ufe20" <= char <= "\ufe2f"
            ):
                return True
        return False

    # ------------------------------------------------------------
    # font inventory
    # ------------------------------------------------------------
    def _font_facts(self, document: fitz.Document) -> dict[str, Any]:
        fonts: list[dict[str, Any]] = []
        seen_xrefs: dict[int | tuple[str, str, str], dict[str, Any]] = {}

        for index, page in enumerate(document.pages()):
            page_number = index + 1
            for entry in page.get_fonts(full=True):
                # PyMuPDF 7-tuple: (xref, ext, type, basefont, name, encoding, referencer)
                xref, ext, font_type, basefont, _name, encoding = entry[:6]
                embedded = bool(ext) and ext != "n/a"
                key = int(xref) if xref else (str(basefont), str(font_type), str(encoding))
                fact = seen_xrefs.get(key)
                if fact is None:
                    fact = {
                        "xref": int(xref) if xref else None,
                        "basefont": str(basefont),
                        "type": str(font_type),
                        "encoding": str(encoding or ""),
                        "subset": bool(_SUBSET_PREFIX_RE.match(str(basefont))),
                        "embedded": embedded,
                        "tounicode": self._has_tounicode(document, xref),
                        "pages": [],
                    }
                    seen_xrefs[key] = fact
                    fonts.append(fact)
                if page_number not in fact["pages"]:
                    fact["pages"].append(page_number)

        fonts.sort(
            key=lambda fact: (fact["pages"][0] if fact["pages"] else 0, fact["basefont"])
        )

        missing = [
            {
                "basefont": fact["basefont"],
                "type": fact["type"],
                "encoding": fact["encoding"],
                "subset": fact["subset"],
            }
            for fact in fonts
            if not fact["tounicode"]
        ]
        risk = [
            item
            for item in missing
            if self._tounicode_gap_is_risky(item)
        ]
        return {
            "fonts": fonts,
            "subset_font_count": sum(1 for fact in fonts if fact["subset"]),
            "non_embedded_font_count": sum(1 for fact in fonts if not fact["embedded"]),
            "tounicode_missing_fonts": missing,
            "tounicode_missing_risk": risk,
        }

    @staticmethod
    def _has_tounicode(document: fitz.Document, xref: int | str) -> bool:
        """Whether the font dictionary carries a /ToUnicode CMap.

        Base-14 fonts (Helvetica etc.) without /ToUnicode are normal; the
        caller distinguishes risk via ``_tounicode_gap_is_risky``.
        """
        try:
            kind, _value = document.xref_get_key(int(xref), "ToUnicode")
        except Exception:  # Evidence gathering is never fatal.
            return False
        return kind != "null"

    @staticmethod
    def _tounicode_gap_is_risky(font: dict[str, Any]) -> bool:
        """A missing /ToUnicode only threatens extraction for CMap fonts,
        subset fonts, and fonts without a standard implicit encoding."""
        if font["subset"]:
            return True
        if any(cid in font["type"] for cid in _CID_FONT_TYPES):
            return True
        encoding = font["encoding"]
        return not any(encoding.startswith(prefix) for prefix in _IMPLICIT_ENCODING_PREFIXES)

    # ------------------------------------------------------------
    # page merge contract (character evidence is document-level)
    # ------------------------------------------------------------
    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        character_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Character evidence lives in metadata only; pages pass through."""
        return text_pages
