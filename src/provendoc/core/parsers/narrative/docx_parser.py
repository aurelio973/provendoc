# ============================================================
# Narrative Domain
# DOCX Semantic Structure Parser
# ============================================================
import hashlib
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

from docx.opc.constants import CONTENT_TYPE as _CT
from docx.opc.part import PartFactory as _DocxPartFactory
from docx.oxml.ns import qn
from docx.package import Package as _DocxPackage
from docx.parts.document import DocumentPart as _DocxDocumentPart
from docx.table import Table as DocxTable
from docx.table import _Cell as DocxCell
from docx.table import _Row as DocxRow
from docx.text.paragraph import Paragraph as DocxParagraph

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser

# ============================================================
# Word-family main-part content types
# (docx_word_family_routing_prereg_v1 §2.2)
# ============================================================
# python-docx 1.2.0 api.Document only accepts WML_DOCUMENT_MAIN; docm/
# dotx/dotm macro/template variants share the same WordprocessingML schema; PartFactory
# registry is a designed-in extension point of python-docx (docx/__init__.py sets the same precedent),
# and setdefault is idempotent — adds only, never overwrites python-docx's existing mappings.
_DOCM_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"
_DOTX_MAIN = (
    "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.template.main+xml"
)
_DOTM_MAIN = "application/vnd.ms-word.template.macroEnabled.main+xml"

for _macro_template_ct in (_DOCM_MAIN, _DOTX_MAIN, _DOTM_MAIN):
    _DocxPartFactory.part_type_for.setdefault(
        _macro_template_ct, _DocxDocumentPart
    )

_WORD_MAIN_CONTENT_TYPES = frozenset(
    {_CT.WML_DOCUMENT_MAIN, _DOCM_MAIN, _DOTX_MAIN, _DOTM_MAIN}
)


def _open_word_package(file_path):
    """Open the package by relationships and validate the main part content type whitelist.

    The only difference from docx.api.Document is the guard scope: the api layer hard-validates
    the single value WML_DOCUMENT_MAIN, while here it is widened to the four Word-family values
    (docm/dotx/dotm variants share the schema and stay extractable); alien packages (e.g. an xlsx
    renamed to .docx) still raise ValueError through the existing honest-failure path.
    """
    document_part = _DocxPackage.open(file_path).main_document_part
    if document_part.content_type not in _WORD_MAIN_CONTENT_TYPES:
        raise ValueError(
            "not a WordprocessingML main part: content type is "
            f"'{document_part.content_type}'"
        )
    return document_part.document

from provendoc.core.diagnostics import (
    blank_run_excess_ratio,
    build_carrier_facts,
    build_diagnostics,
    build_warnings,
    contains_ansi_escape,
    count_replacement_chars,
    count_suspicious_chars,
    detect_hidden_unicode,
    detect_security_carriers,
    emoji_ratio,
    empty_line_ratio,
    printable_ratio,
    symbol_ratio,
)
from provendoc.core.foundation.metadata import _build_metadata

# ZIP container guard (zip-bomb protection, security checklist §75 v1): pre-decompression central-directory quota
from provendoc.core.parsers.container_guard import (
    check_zip_package_quietly as _check_zip_package_safe,
)
from provendoc.core.parsers.container_guard import (
    zip_guard_error,
)
from provendoc.core.parsers.narrative.docx_charts import extract_chart_summary
from provendoc.core.parsers.narrative.docx_content_controls import (
    extract_content_controls_summary,
)
from provendoc.core.parsers.narrative.docx_diagrams import (
    SmartArtResolver,
    diagram_block_text,
)
from provendoc.core.parsers.narrative.docx_embedded_strategy import (
    extract_embedded_strategy,
)
from provendoc.core.parsers.narrative.docx_fields import extract_links_summary
from provendoc.core.parsers.narrative.docx_footnotes import (
    NoteModel,
    paragraph_note_refs,
    truncate_note_text,
)
from provendoc.core.parsers.narrative.docx_image_assets import (
    extract_image_assets as extract_image_assets_impl,
)
from provendoc.core.parsers.narrative.docx_image_assets import (
    placement_placeholder,
)
from provendoc.core.parsers.narrative.docx_images import extract_image_summary
from provendoc.core.parsers.narrative.docx_layout import extract_layout_summary
from provendoc.core.parsers.narrative.docx_markers import (
    extract_marker_records,
    missing_document_xml_record,
)
from provendoc.core.parsers.narrative.docx_package import extract_package_summary
from provendoc.core.parsers.narrative.docx_review import extract_review_summary
from provendoc.core.parsers.narrative.docx_revisions import (
    extract_revision_summary,
)
from provendoc.core.parsers.narrative.docx_security import (
    classify_container,
    extract_security_summary,
)
from provendoc.core.parsers.narrative.docx_structure import (
    ListNumberingTracker,
    NumberingModel,
    extract_structure_summary,
    numbering_summary,
    paragraph_numref,
)
from provendoc.core.parsers.narrative.docx_style_memo import (
    paragraph_style,
    paragraph_style_id,
)
from provendoc.core.parsers.narrative.docx_tables import extract_table_summary
from provendoc.core.parsers.narrative.docx_text_characters import (
    extract_text_character_summary,
)
from provendoc.core.parsers.narrative.omml import (
    OMATH,
    is_display_math,
    iter_omath,
    omml_linear_text,
    omml_structure_counts,
    omml_to_mathml,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.equation_numbers import match_equation_number
from provendoc.core.structure.heading_patterns import (
    MAX_HEADING_LEN,
    match_heading_line,
)
from provendoc.core.structure.title_matching import titles_equivalent

# Style-name signals, matched on NFKC-normalized, casefolded names so that
# "Heading 1", "heading1" and the zh heading form behave identically.
_STYLE_HEADING_LEVEL_RE = re.compile(r"(?:heading|标题)\s*([0-9])", re.IGNORECASE)
_STYLE_HEADING_PLAIN_RE = re.compile(r"^heading$", re.IGNORECASE)
_STYLE_TITLE_RE = re.compile(r"^(?:title|document\s+title|标题|文档标题)$", re.IGNORECASE)
_STYLE_SUBTITLE_RE = re.compile(
    r"^(?:subtitle|document\s+subtitle|副标题|文档副标题)$", re.IGNORECASE
)
_STYLE_TOC_LEVEL_RE = re.compile(r"^(?:toc|目录)\s*([0-9])?$", re.IGNORECASE)

# Whole-line 【…】 section convention (Chinese drug-insert section headers).
# Same shape as the txt-side _BRACKET_HEADING_PATTERN in semantic_diagnostics and the PDF-side
# pipeline._BRACKET_HEADING_RE: a text convention, not an author-declared structure.
_BRACKET_HEADING_RE = re.compile(r"^【(.+?)】$")
_BRACKET_MAX_TEXT_LEN = 30

# Terminal punctuation: a fallback heading candidate ending in one of these
# is a numbered sentence, not a structural heading (same guard as the PDF
# heading heuristics).
_TERMINAL_PUNCTUATION = (
    "。", ".", ";", "；", "?", "？", "!", "！", ":", "：", ",", "，",
)


def _runs_like_prose(text):
    """Whether the text behaves like a running sentence, not a heading.

    Headings are short and unpunctuated; prose carries terminal
    punctuation or exceeds the heading length cap. Used to demote
    DIRECT w:outlineLvl carried by ordinary quotation/reference lines
    (stress004: 11 orphan-heading false positives came from citation
    paragraphs whose authors set outline levels). Style-chain
    outlineLvl is a deliberate declaration and never passes here.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > MAX_HEADING_LEN:
        return True
    return stripped.endswith(_TERMINAL_PUNCTUATION)


# ============================================================
# Parser
# ============================================================
class DocxParser(BaseParser):
    """
    DOCX parser focuses on:

    - semantic structure extraction
    - heading hierarchy recovery
    - paragraph/list/table detection
    - lightweight narrative semantics

    DOCX does NOT require recovery.
    Structure already exists explicitly.
    """
    def __init__(
            self,
            extract_tables=True,
            detect_lists=True,
            detect_code_block=True,
            preserve_empty=False,
            enable_image_assets=True,
            limits=None
    ):
        """Initialize with table/list/code-block/empty-paragraph and image-asset switches.

        ``limits`` is a resource guard (guards, not filters; default None keeps the historical behavior),
        prereg oversize_structure_limits_prereg_v1）：``max_body_elements``
        truncating body traversal on limit violation and recording it in ``metadata["resource_guard"]``.
        """
        self.extract_tables = extract_tables
        self.detect_lists = detect_lists
        self.detect_code_blocks = detect_code_block
        self.preserve_empty = preserve_empty
        # Auto-trigger: extraction is a natural no-op for documents without
        # w:drawing pictures, so the default on means "image detected →
        # assets + placeholders + (at the run layer) derived OpenCV
        # processing".  False = explicit opt-out.
        self.enable_image_assets = enable_image_assets
        self.limits = limits
        # Style-name / style-chain derived-value cache (bug65649 performance fix):
        # in-the-wild styles.xml can bloat pathologically (measured 2.5MB / 2672 styles),
        # while python-docx's para.style does a linear scan of the styles element on
        # every access; paragraphs without w:pStyle hit styles.default_for — scanning
        # all styles for w:default="1". Style-object parsing already goes through docx_style_memo
        # (memoized per document part, shared by all docx feature modules); only the
        # derived values (display name / pPr chain) are cached here, reset per package in parse().
        self._style_name_cache = {}
        self._style_chain_cache = {}

    # ========================================================
    # public
    # ========================================================
    def parse(self, file_path):
        """Parse a .docx into a Document; OLE containers (encrypted packages / legacy .doc) fail honestly."""
        metadata = _build_metadata(file_path)
        # file_type follows the same convention as the other eight narrative parsers (formerly a gap:
        # docx was always missing this key, so upstream/downstream could only guess from the extension)
        metadata["file_type"] = "docx"

        # ZIP container guard (zip-bomb protection, security checklist §75 v1): read the central directory only,
        # decide before decompression; a violation means honest failure; when the guard itself cannot read
        # a central directory (OLE containers etc.) it lets the file pass, leaving it to the later classify_container/parsing
        # to error naturally — the guard is never fatal and never answers on the parser's behalf.
        _zip_guard = _check_zip_package_safe(file_path)
        if _zip_guard is not None:
            metadata["zip_package_guard"] = _zip_guard
            if _zip_guard.get("verdict") == "violation":
                metadata["parse_failed"] = True
                metadata["parse_error"] = zip_guard_error(_zip_guard)
                return Document(text="", metadata=metadata)

        start_time = time.perf_counter()

        # Object-level unresolved registration (marker contract §1): the list is initialized before the try,
        # so the open-failure path (the except branch back-fills docx_unresolved) can still carry
        # the records collected before open (docx_word_marker_codes prereg §2 F1).
        unresolved_records = []

        try:
            raw_bytes = Path(file_path).read_bytes()
            metadata["sha256"] = hashlib.sha256(raw_bytes).hexdigest()

            # Container pre-flight: a .docx that is actually an OLE/CFB
            # binary (an encrypted OOXML package or a legacy .doc) can
            # never open as a zip.  The classification is recorded as
            # evidence before the honest failure.
            container_facts = classify_container(raw_bytes)
            if container_facts["ole_container"]:
                metadata["docx_security"] = {"encryption": container_facts}
                raise ValueError(
                    "not an Open XML zip package: "
                    + str(container_facts["note"])
                )

            # B9: package-level evidence must be recorded BEFORE python-docx
            # opens the file — a strict-namespace package or one missing its
            # required parts cannot be opened at all, and the honest failure
            # must still carry the attributable package facts.
            metadata["docx_package"] = extract_package_summary(file_path)
            # F1 MISSING_DOCUMENT_XML (prereg §2): taken from package facts without reopening the zip;
            # must be enqueued before the open attempt so an open failure can still carry the record.
            _missing_doc_record = missing_document_xml_record(
                metadata["docx_package"]
            )
            if _missing_doc_record is not None:
                unresolved_records.append(_missing_doc_record)

            doc = _open_word_package(file_path)
            # Derived cache reset per package: a styleId resolves deterministically inside one
            # package and is never reused across packages (the same id means different things
            # in different styles.xml). Style objects themselves go through docx_style_memo (keyed per part, auto-isolated).
            self._style_name_cache = {}
            self._style_chain_cache = {}
            blocks = []
            current_heading_path = []
            block_type_counter = Counter()
            # Object-level unresolved registration (marker contract §1, 2026-09-28):
            # body placeholder lines stay put; machine-readable facts go through the registration channel — locatable,
            # countable, filterable, recoverable; the aggregate is written non-empty-only into
            # metadata["docx_unresolved"] (narrative contract unchanged).
            # (unresolved_records is already initialized before the try; see the F1 wiring.)
            toc_field_detected = False
            toc_entries = []
            formula_facts_all = []
            # numbering.xml model + document-order counter simulation:
            # python-docx's paragraph.text never contains the automatic
            # numbers Word renders, so markers are recomputed here.
            numbering_model = NumberingModel.from_document(doc)
            marker_tracker = ListNumberingTracker(numbering_model)
            list_item_count = 0

            # ====================================================
            # footnotes/endnotes + SmartArt resolvers (one load
            # per package; paragraph lookups stay cheap)
            # ====================================================
            note_model = NoteModel.from_package(file_path)
            smartart_resolver = SmartArtResolver.from_package(file_path)
            note_reference_count = 0
            note_anchored_ids: list[tuple[str, str]] = []
            note_unmatched_references: list[dict] = []
            note_block_count = 0
            smartart_referenced_count = 0

            # ====================================================
            # image assets (opt-in): original bytes + placement
            # evidence, joined to the narrative through
            # placeholder blocks that carry the asset id
            # ====================================================
            image_asset_records = []
            if self.enable_image_assets:
                image_asset_records = extract_image_assets_impl(
                    doc, file_path
                )["records"]
            images_by_paragraph = {}
            for record in image_asset_records:
                images_by_paragraph.setdefault(
                    record.get("paragraph_index"), []
                ).append(record)

            # ====================================================
            # Footnote/endnote blocks for one paragraph: emitted
            # right after the paragraph's own block so the note
            # text stays anchored to its reference in reading
            # order (the note part text was previously invisible
            # to the narrative entirely).
            # ====================================================
            def _emit_note_blocks(para, p_index):
                nonlocal note_reference_count, note_block_count
                for ref in paragraph_note_refs(para):
                    note_reference_count += 1
                    note = note_model.resolve(ref["kind"], ref["id"])
                    label = (
                        "Footnote" if ref["kind"] == "footnote" else "Endnote"
                    )
                    if note is not None and note["text"]:
                        note_anchored_ids.append(
                            (ref["kind"], str(note["id"]))
                        )
                        block_type_counter["footnote"] += 1
                        blocks.append(
                            {
                                "type": "footnote",
                                "text": (
                                    f"[{label} {note['id']}] "
                                    f"{truncate_note_text(note['text'])}"
                                ),
                                "note_kind": ref["kind"],
                                "note_id": note["id"],
                                "custom_mark": ref["custom_mark"],
                                "paragraph_index": p_index,
                                "heading_path": list(current_heading_path),
                                "semantic_scope": "narrative",
                                "source": f"docx_{ref['kind']}",
                            }
                        )
                        note_block_count += 1
                    else:
                        note_unmatched_references.append(
                            {
                                "kind": ref["kind"],
                                "id": ref["id"],
                                "para_index": p_index,
                                "reason": "note part missing or empty",
                            }
                        )

            # ====================================================
            # body children in document order: paragraphs and
            # tables interleaved.  Tables used to be appended
            # after every paragraph, losing their position and
            # inheriting the last heading as heading_path; the
            # body-child walk keeps reading order and the
            # heading context active at the table's position.
            # paragraph_index counts direct w:p children only,
            # matching doc.paragraphs / image-asset joins.
            # ====================================================
            _W_P_TAG = qn("w:p")
            _W_TBL_TAG = qn("w:tbl")
            _W_SDT_TAG = qn("w:sdt")
            _W_TR_TAG = qn("w:tr")
            _W_TC_TAG = qn("w:tc")
            paragraph_index = -1
            # Block-level sdt → body-block start index (the block_index data
            # source of docx_content_controls prereg §2): where an sdt element appears,
            # len(blocks) is the first block number of its expanded sdtContent.
            # lxml proxy discipline: id() is stable only while the proxy is alive; the keepalive
            # list holds references up to the registry call site (within the parse scope), so the registry's
            # id-keyed back-fill never mismatches.
            sdt_block_index_by_element = {}
            sdt_block_index_keepalive = []

            # Resource guard (guards, not filters, prereg
            # oversize_structure_limits_prereg_v1): the count of direct body
            # children is decidable up front (len is a cheap lxml op); over the cap, traversal is truncated;
            # the beyond-cap portion is neither extracted nor added to blocks; facts go into resource_guard.
            body_total = len(doc.element.body)
            body_cap = (
                self.limits.max_body_elements
                if self.limits is not None else None
            )
            body_guard_reasons = []
            if body_cap is not None and body_total > body_cap:
                body_guard_reasons.append(
                    f"body children beyond {body_cap} not extracted "
                    f"(total {body_total}, max_body_elements={body_cap})"
                )
            body_seen = 0

            for child, sdt_depth in _iter_body_children(doc.element.body):
                if sdt_depth == 0 and body_cap is not None:
                    body_seen += 1
                    if body_seen > body_cap:
                        break
                if child.tag == _W_SDT_TAG:
                    # Block-level content controls (docx_content_controls prereg §3):
                    # sdtContent is the body itself; its paragraphs/tables are already
                    # merged into traversal order by _iter_body_children with sdt_depth>0;
                    # only the body-block start index is registered here (for the registry's block_index
                    # back-fill); the sdt element itself produces no body block.
                    sdt_block_index_by_element[id(child)] = len(blocks)
                    sdt_block_index_keepalive.append(child)
                    continue
                if child.tag == _W_TBL_TAG:
                    if not self.extract_tables:
                        continue
                    table = DocxTable(child, doc)
                    rows = []

                    def _row_cells(row_element):
                        """Inline cell projection, sdt-aware (an extension of the prereg §3 fix surface,
                        evidenced by the real_public probe):

                        - rows without sdt: python-docx row.cells original semantics
                          (gridSpan duplication + vMerge continue delegating
                          upward), byte-identical with the existing behavior;
                        - rows containing sdt (sdt wrapping tc or paragraphs):
                          _iter_body_children expands to get tc,
                          gridSpan duplication kept, vMerge continue is not
                          delegated (pathological combination; the content itself is the evidence).
                        """
                        if row_element.find(_W_SDT_TAG) is None:
                            return DocxRow(row_element, table).cells
                        cells = []
                        for (
                            tc_element,
                            _tc_depth,
                        ) in _iter_body_children(row_element):
                            if tc_element.tag != _W_TC_TAG:
                                continue
                            cell = DocxCell(tc_element, table)
                            try:
                                span = max(
                                    int(tc_element.grid_span), 1
                                )
                            except Exception:
                                span = 1
                            cells.extend([cell] * span)
                        return tuple(cells)

                    # Block-level sdt transparency also covers table walks
                    # (an extension of the docx_content_controls prereg §3 fix surface,
                    # evidenced by the real_public probe): python-docx's
                    # table.rows / row.cells / direct-child w:p projections of a cell
                    # all look at direct children only, so table rows wrapped in sdt
                    # (tr→sdtContent) and paragraphs inside cells
                    # (tc→sdtContent→p) would be dropped wholesale and silently — expanded
                    # via the same _iter_body_children as the body.
                    for row_element, _row_depth in _iter_body_children(
                        child
                    ):
                        if row_element.tag != _W_TR_TAG:
                            continue
                        try:
                            row_cells = _row_cells(row_element)
                        except Exception:
                            # Pathological-combination fallback (sdt-wrapped row +
                            # vMerge continue: python-docx's
                            # _tc_above grid back-trace may raise): fall back to the
                            # direct-child tc projection — break one row, not the whole table.
                            row_cells = tuple(
                                DocxCell(tc, table)
                                for tc in row_element.findall(
                                    _W_TC_TAG
                                )
                            )
                        row_data = []
                        for cell in row_cells:
                            # Cell text follows the final view too: w:ins merged in,
                            # w:del/w:txbxContent excluded (the same rule as the
                            # paragraph walk); paragraphs are taken in full via sdt-aware traversal
                            # (direct child w:p + sdt expansion), preserving
                            # python-docx cell.text's paragraph-joining semantics.
                            cell_parts = []
                            for (
                                cell_child,
                                _cell_depth,
                            ) in _iter_body_children(cell._tc):
                                if cell_child.tag != _W_P_TAG:
                                    continue
                                cell_parts.append(
                                    self._paragraph_text_with_math(
                                        DocxParagraph(cell_child, doc),
                                        [],
                                    )
                                )
                            row_data.append(
                                "\n".join(cell_parts).strip()
                            )
                        rows.append(row_data)
                    block_type_counter["table"] += 1
                    blocks.append(
                        {
                            "type": "table",
                            "rows": rows,
                            "heading_path": list(current_heading_path),
                            "semantic_scope": "table",
                            "source": "docx_table",
                            # Nearest preceding direct-child paragraph
                            # count — informational fact, never a
                            # reading-order claim (same anchor
                            # semantics as cell note anchoring below);
                            # table not being a paragraph, the key name
                            # states it. -1 (no preceding paragraph)
                            # degrades to None.
                            "preceding_paragraph_index": (
                                paragraph_index
                                if paragraph_index >= 0
                                else None
                            ),
                        }
                    )
                    # Notes anchored inside table cells: the table block
                    # captures cell text, not footnote references, so
                    # cell-anchored notes are emitted right after the
                    # table block (they would otherwise only surface as
                    # unanchored facts in the note summary).  The anchor
                    # position is the nearest preceding top-level
                    # paragraph — an informational fact, never a
                    # reading-order claim.
                    for cell_paragraph_element in child.iter(_W_P_TAG):
                        _emit_note_blocks(
                            DocxParagraph(cell_paragraph_element, doc),
                            paragraph_index,
                        )
                    continue
                if child.tag != _W_P_TAG:
                    # sectPr and other structural body children
                    continue
                if sdt_depth == 0:
                    paragraph_index += 1
                # sdt-expanded paragraphs record paragraph number None: the direct-child paragraph count
                # contract (aligned with doc.paragraphs / the image-asset join) must not drift
                # because of sdt expansion; None only conveys the information fact "not a direct-child
                # paragraph" and takes part in no number-keyed join.
                p_index = paragraph_index if sdt_depth == 0 else None
                para = DocxParagraph(child, doc)
                math_nodes = list(iter_omath(para._p))
                math_facts = []
                math_pairs = []
                if math_nodes:
                    # python-docx's para.text skips m:oMath entirely; a
                    # display-equation paragraph would read as empty and the
                    # formula text would silently disappear.  Rebuild the
                    # paragraph text in document order, inserting each
                    # formula's verbatim linear text where it occurs.
                    for node in math_nodes:
                        linear = omml_linear_text(node)
                        if not linear.strip():
                            continue
                        mathml, mathml_unsupported = omml_to_mathml(node)
                        fact = {
                            "display": is_display_math(node),
                            "text": linear,
                            "structure": omml_structure_counts(node),
                            "mathml": mathml,
                            "mathml_unsupported_elements": mathml_unsupported,
                        }
                        math_facts.append(fact)
                        math_pairs.append((node, linear))
                        formula_facts_all.append(
                            {**fact, "para_index": p_index}
                        )
                # Body = final view (docx_revision_text prereg §3):
                # a unified XML-aware walk — text inside w:ins is merged in (previously
                # silently dropped by python-docx's direct-run projection); w:delText inside
                # w:del and drawing text boxes (w:txbxContent) never enter
                # the body (deleted text goes only into the docx_revisions registry).
                text = self._paragraph_text_with_math(
                    para, math_pairs
                ).strip()

                # ==================================================
                # image placeholder(s) for this paragraph (opt-in):
                # a paragraph carrying pictures keeps its narrative
                # text, and every picture gets a placeholder block
                # in reading position — the join point to the
                # image-asset manifest.
                # ==================================================
                paragraph_images = images_by_paragraph.get(p_index)
                if paragraph_images:
                    for image_seq, image_record in enumerate(
                        paragraph_images, start=1
                    ):
                        caption = (
                            image_record.get("caption_after")
                            or image_record.get("caption_before")
                        )
                        placeholder = placement_placeholder(image_record)
                        block_type_counter["image"] += 1
                        blocks.append({
                            "type": "image",
                            "text": placeholder,
                            "asset_id": image_record.get("asset_id"),
                            "extracted": image_record.get(
                                "extracted", False
                            ),
                            "caption": caption,
                            "alt_text": image_record.get("alt_text"),
                            "paragraph_index": p_index,
                            "heading_path": list(current_heading_path),
                            "semantic_scope": "narrative",
                            "source": "docx_image",
                        })
                        # Unextracted image → object-level registration (marker
                        # contract §1.2; body placeholder line unchanged). The unextracted record carries no
                        # asset_id; object_id is located by paragraph number + within-paragraph index
                        # (the placeholder block itself carries the same paragraph_index anchor).
                        if not image_record.get("extracted", False):
                            unresolved_records.append({
                                "object_type": "image",
                                "object_id": (
                                    f"para-{p_index}"
                                    f"-image-{image_seq}"
                                ),
                                "resolution": "unresolved",
                                "reason_code": "IMAGE_NOT_EXTRACTED",
                                "source": (
                                    image_record.get("part_name")
                                    or "word/document.xml"
                                ),
                                "placeholder_text": placeholder,
                                "not_extracted_reason": (
                                    image_record.get(
                                        "not_extracted_reason"
                                    )
                                    or "unknown"
                                ),
                            })

                # ================================================
                # SmartArt (dgm:relIds): diagram node labels
                # rendered as list lines at reading position,
                # mirroring the PPTX contract.
                # ================================================
                paragraph_diagrams = smartart_resolver.paragraph_diagrams(para)
                for diagram in paragraph_diagrams:
                    smartart_referenced_count += 1
                    block_type_counter["diagram"] += 1
                    diagram_text = diagram_block_text(diagram["items"])
                    blocks.append(
                        {
                            "type": "diagram",
                            "text": diagram_text,
                            "readable": diagram["readable"],
                            "reason": diagram.get("reason"),
                            "paragraph_index": p_index,
                            "heading_path": list(current_heading_path),
                            "semantic_scope": "narrative",
                            "source": "docx_smartart",
                        }
                    )
                    # Unreadable figure → object-level registration (marker
                    # contract §1.2; body placeholder line unchanged). object_id follows
                    # the rel_id precedent; reason distinguishes missing part /
                    # corrupted XML (the same taxonomy as pptx).
                    if not diagram["readable"]:
                        reason_text = diagram.get("reason") or ""
                        unresolved_records.append({
                            "object_type": "smartart",
                            "object_id": diagram["rel_id"],
                            "resolution": "unresolved",
                            "reason_code": (
                                "SMARTART_PART_MISSING"
                                if diagram.get("part") is None
                                or "missing from package" in reason_text
                                else "SMARTART_DATA_UNREADABLE"
                            ),
                            "source": (
                                diagram.get("part")
                                or "word/document.xml"
                            ),
                            "placeholder_text": diagram_text,
                            "reason": reason_text or None,
                        })

                if not text and not self.preserve_empty:
                    # An empty paragraph may still carry note anchors
                    # (e.g. a footnote reference alone on a line).
                    _emit_note_blocks(para, p_index)
                    continue

                style_name = self._style_name(para)
                numref = paragraph_numref(para) if self.detect_lists else None
                semantic_type = self._detect_semantic_type(
                    text,style_name,para,numref
                )

                # Feed the counter in document order for EVERY numbered
                # paragraph, whatever block type it becomes: Word's
                # numbering does not pause for interleaved content.
                marker = None
                if numref is not None:
                    marker = marker_tracker.feed(numref[0],numref[1])
                    list_item_count += 1

                # ================================================
                # display formula paragraph: the paragraph's only
                # content is OMML (plus a possible trailing number)
                # ================================================
                non_math_text = self._non_math_text(para).strip()
                if math_facts and (
                    not non_math_text
                    or match_equation_number(non_math_text) is not None
                ):
                    block_type_counter["formula"] += 1
                    formula_block = {
                        "type": "formula",
                        "text": text,
                        "heading_path": list(current_heading_path),
                        "style": style_name,
                        "semantic_scope": "narrative",
                        "source": "docx_omml",
                        "formulas": math_facts,
                        # Direct-child w:p count (0-based); None for
                        # sdt-expanded paragraphs (contract matches the existing
                        # paragraph_index key of footnote/image blocks).
                        "paragraph_index": p_index,
                    }
                    equation = match_equation_number(text)
                    if equation is not None:
                        formula_block["equation_number"] = equation.number
                        formula_block["equation_number_source"] = equation.source
                    blocks.append(formula_block)
                    _emit_note_blocks(para, p_index)
                    continue

                block_type_counter[semantic_type] += 1

                # ================================================
                # toc entry (navigation, not narrative)
                # ================================================
                if semantic_type == "toc_entry":
                    toc_level = self._toc_style_level(style_name)
                    if self._contains_toc_field(para):
                        toc_field_detected = True
                    toc_entries.append({
                        "text": text,
                        "level": toc_level,
                        "style": style_name,
                    })
                    blocks.append({
                        "type": "toc_entry",
                        "text": text,
                        "toc_level": toc_level,
                        "heading_path": list(current_heading_path),
                        "style": style_name,
                        "semantic_scope": "navigation",
                        "source": (
                            "docx_toc_style"
                            if toc_level is not None
                            else "docx_toc_field"
                        ),
                        "paragraph_index": p_index,
                    })
                    _emit_note_blocks(para, p_index)
                    continue

                # ================================================
                # heading
                # ================================================
                if semantic_type == "heading":
                    heading_level,heading_source = self._resolve_heading_level(
                        para,style_name,text
                    )
                    current_heading_path =(
                        self._update_heading_path(
                            current_heading_path,
                            text,
                            heading_level
                        )
                    )
                    blocks.append({
                        "type": "heading",
                        "text": text,
                        "heading_level": heading_level,
                        "heading_path": list(current_heading_path),
                        "style": style_name,
                        "semantic_scope": "narrative",
                        "source": heading_source,
                        "confidence": (
                            0.95
                            if heading_source in {
                                "docx_outline_level",
                                "docx_style",
                                "docx_title_style",
                            }
                            else 0.6
                        ),
                        "paragraph_index": p_index,
                    })
                    _emit_note_blocks(para, p_index)
                    continue

                # ================================================
                # normal semantic block
                # ================================================
                normal_block = {
                    "type": semantic_type,
                    "text": text,
                    "heading_path": list(current_heading_path),
                    "style": style_name,
                    "semantic_scope": (
                        "code"
                        if semantic_type == "code"
                        else "narrative"
                    ),
                    "source": "docx_paragraph",
                    "paragraph_index": p_index,
                }
                if math_facts:
                    # Inline OMML inside prose: the merged text keeps the
                    # formula content in reading position, and the per-formula
                    # facts stay attached for auditing.
                    normal_block["formulas"] = math_facts
                if numref is not None:
                    num_id,ilvl = numref
                    level = numbering_model.resolve(num_id,ilvl)
                    normal_block["numbering"] = {
                        "num_id": num_id,
                        "ilvl": ilvl,
                        "num_fmt": level.num_fmt if level is not None else None,
                        "marker": marker,
                    }
                    if (
                        marker
                        and semantic_type == "list"
                        and not _starts_with_marker(text,marker)
                    ):
                        # Word renders the marker outside the stored text;
                        # prepend it so the narrative carries the number the
                        # reader actually sees (never duplicated when the
                        # text already spells it out).
                        normal_block["text"] = f"{marker} {text}"
                blocks.append(normal_block)
                _emit_note_blocks(para, p_index)

            # ================================================
            # footnote / SmartArt evidence summaries
            # ================================================
            metadata["docx_footnotes"] = note_model.summary(
                reference_count=note_reference_count,
                anchored_ids=note_anchored_ids,
                unmatched_references=note_unmatched_references,
                emitted_block_count=note_block_count,
            )
            metadata["docx_diagrams"] = smartart_resolver.summary(
                referenced_count=smartart_referenced_count,
            )

            if body_guard_reasons:
                # Written only when triggered (zero new keys on the default path, zero golden diff).
                metadata["resource_guard"] = {
                    "scheme": "resource_limits_v1",
                    "limits": self.limits.as_dict(),
                    "reasons": body_guard_reasons,
                }

            # ====================================================
            # diagnostics
            # ====================================================
            reconstructed_text = self._reconstruct_text(blocks)
            diagnostics = self._build_text_diagnostics(reconstructed_text)
            metadata["diagnostics"] = diagnostics
            metadata["warnings"] = build_warnings(diagnostics)
            # Domain-document signals (contracts / health-check reports / drug inserts / invoices): detected
            # on the reconstructed narrative text; counts only, no interpretation.
            metadata["domain_signals"] = detect_domain_signals(reconstructed_text)
            metadata["block_distribution"] = dict(block_type_counter)
            metadata["docx_blocks"] = blocks
            metadata["docx_formulas"] = self._formula_summary(
                formula_facts_all,blocks
            )
            metadata["docx_toc"] = self._toc_consistency(
                toc_entries,blocks,toc_field_detected
            )
            metadata["docx_links"] = extract_links_summary(doc)
            metadata["docx_structure"] = extract_structure_summary(doc)
            metadata["docx_numbering"] = numbering_summary(
                numbering_model,list_item_count
            )
            metadata["docx_security"] = extract_security_summary(
                doc,raw_bytes,file_path
            )
            # p2b embedded-recursion strategy layer (docx_embedded_strategy prereg §2):
            # parent-child model registration (no key written without an embeddings part, zero golden diff);
            # the v1 strategy is register-only — embedded bodies are never expanded/extracted/executed.
            embedded_strategy = extract_embedded_strategy(
                doc, file_path, metadata.get("sha256")
            )
            if embedded_strategy is not None:
                metadata["docx_embedded_strategy"] = embedded_strategy
            metadata["docx_text_characters"] = extract_text_character_summary(
                doc,file_path
            )
            metadata["docx_layout"] = extract_layout_summary(doc)
            metadata["docx_tables"] = extract_table_summary(doc)
            metadata["docx_images"] = extract_image_summary(doc)
            metadata["docx_charts"] = extract_chart_summary(doc, file_path)
            # p1b Word anomaly-code system (docx_word_marker_codes prereg §2):
            # the nine-code records append to the existing registration channel; the existing four codes and their order stay unchanged.
            unresolved_records.extend(
                extract_marker_records(
                    doc,
                    metadata["docx_package"],
                    metadata["docx_links"],
                    metadata["docx_security"],
                    numbering_model,
                )
            )
            # Object-level revision registration (docx_revision_text prereg §2): w:ins
            # text is already merged into the body's final view; w:del text goes only into this registry;
            # documents without revisions write no key (zero golden diff).
            revision_summary = extract_revision_summary(doc)
            if revision_summary is not None:
                metadata["docx_revisions"] = revision_summary
            # Content-control registration (docx_content_controls prereg §2): block-level
            # sdtContent is already merged into the body walk (§3); structural evidence is registered here;
            # documents without controls write no key.
            controls_summary = extract_content_controls_summary(
                doc, sdt_block_index_by_element
            )
            if controls_summary is not None:
                metadata["docx_content_controls"] = controls_summary
            # Whole chart-channel read failure → object-level registration (marker
            # contract §1.2): chart_part_error is a package-level fact (enumerating/parsing
            # word/charts/* failed); no per-chart body placeholder line, and placeholder
            # is empty; detailed error evidence travels with the record (stable code outward, evidence inward).
            chart_part_error = metadata["docx_charts"].get(
                "chart_part_error"
            )
            if chart_part_error:
                unresolved_records.append({
                    "object_type": "chart",
                    "object_id": "charts",
                    "resolution": "unresolved",
                    "reason_code": "CHART_DATA_MISSING",
                    "source": "word/charts",
                    "placeholder_text": None,
                    "chart_part_error": chart_part_error,
                })
            metadata["docx_review"] = extract_review_summary(doc)
            review_tracked = metadata["docx_review"]["tracked_changes"]
            if review_tracked["has_tracked_changes"]:
                # Visible degradation: in documents with unaccepted revisions, runs wrapped in w:ins/w:del
                # are outside python-docx's direct-run text traversal, and the extraction would drop
                # the revision-marked content wholesale — the fact must be visible, never happen silently
                # (same discipline as the U+FFFD / empty-page warnings).
                metadata["warnings"].append(
                    "document carries unresolved tracked changes "
                    f"({review_tracked['insertion_count']} insertions, "
                    f"{review_tracked['deletion_count']} deletions); "
                    "revision-marked content is omitted from the "
                    "extracted text (neither the revision-marked state "
                    "nor a resolved final document)"
                )
            review_comments = metadata["docx_review"]["comments"]
            if review_comments["comment_count"]:
                metadata["warnings"].append(
                    f"document carries {review_comments['comment_count']} "
                    "review comments (comment bodies are not part of the "
                    "narrative text)"
                )
            if self.enable_image_assets:
                from provendoc.core.image_assets.schema import (
                    image_assets_metadata,
                )

                asset_entry, asset_payloads = image_assets_metadata(
                    image_asset_records, "docx_image_assets_v1"
                )
                metadata["docx_image_assets"] = asset_entry
                if asset_payloads:
                    metadata["image_asset_payloads"] = asset_payloads
            # Object-level unresolved-registration key (marker contract §1.1):
            # written only when non-empty — no key for documents without unresolved objects (honest-absence discipline).
            if unresolved_records:
                metadata["docx_unresolved"] = {
                    "extractor": "docx_unresolved_v1",
                    "records": unresolved_records,
                }
            metadata["parse_duration_ms"] = round(
                (time.perf_counter()
                - start_time
                ) * 1000,
            3
            )

            # Honest failure: the package opened and every part was read,
            # but nothing narrative was extractable (image/chart-only
            # documents, header-only alternate content, ...).  A successful
            # parse that yields no text would force the representation
            # layer to emit an empty document — the contract instead
            # requires an explicit parse_failed with the evidence intact.
            if not reconstructed_text.strip():
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "document contains no extractable narrative text"
                )
                return Document(
                    text="",
                    metadata=metadata
                )

            # Security-carrier census (invisible_carrier_v1,
            # security_carrier_formats_prereg_v1 §2.1; same shape as the
            # doc_cfb bypass precedent): docx extraction goes through python-docx, not the base
            # _read_and_diagnose path, so this is mounted on the post-extraction body view;
            # this parser has no sanitization step — reconstructed_text is the pre-sanitization
            # raw view. metadata["security_carriers"] is written only when non-zero,
            # and the fact codes are appended into warnings (zero-diff discipline: no carrier, no new key).
            carrier_census = detect_security_carriers(reconstructed_text)
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata.setdefault("warnings", []).extend(
                    build_carrier_facts(carrier_census)
                )

            return Document(
                text=reconstructed_text,
                metadata=metadata
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            # Failure-path back-fill (prereg §2 F1 boundary): registrations collected before open
            # are not dropped by the failure — an honest failure still carries locatable facts.
            if unresolved_records:
                metadata["docx_unresolved"] = {
                    "extractor": "docx_unresolved_v1",
                    "records": unresolved_records,
                }
            return Document(
                text = "",
                metadata=metadata
            )

    # ========================================================
    # text diagnostics
    # ========================================================
    @staticmethod
    def _build_text_diagnostics(text):
        return build_diagnostics(
            replacement_chars=count_replacement_chars(text),
            suspicious_chars=count_suspicious_chars(text),
            printable_ratio=printable_ratio(text),
            symbol_ratio=symbol_ratio(text),
            empty_line_ratio=empty_line_ratio(text),
            hidden_unicode=detect_hidden_unicode(text),
            contains_ansi_escape=contains_ansi_escape(text),
            emoji_ratio=emoji_ratio(text),
            blank_run_excess_ratio=blank_run_excess_ratio(text),
        )

    # ========================================================
    # semantic type detection
    # ========================================================
    def _detect_semantic_type(
            self,
            text,
            style_name,
            para=None,
            numref=None
    ):
        normalized = _normalize_style_name(style_name)
        style_lower = normalized.lower()

        # ====================================================
        # toc entry: TOC field or a TOC/zh-TOC styled paragraph
        # (must precede heading: TOC entries reuse heading text)
        # ====================================================
        if self._toc_style_level(style_name) is not None:
            return "toc_entry"
        if para is not None and self._contains_toc_field(para):
            return "toc_entry"

        # ====================================================
        # heading: explicit style or outline level
        # ====================================================
        if self._style_heading_level(style_name) is not None:
            return "heading"
        if para is not None and self._outline_level(para) is not None:
            # stress004 guardrail: some documents carry w:outlineLvl directly on
            # ordinary prose paragraphs (author formatting mislabeling; a measured quotation
            # paragraph with direct outlineLvl=Y caused 11 orphan-heading false positives). Real headings are short and
            # do not end with terminal punctuation; direct outlineLvl + prose traits → demote
            # to a plain paragraph, routed through the later list/code/paragraph classification.
            # outlineLvl inherited through the style chain is an intentional declaration — keep the original verdict.
            if (
                self._ppr_outline_level(para._p.pPr) is not None
                and _runs_like_prose(text)
            ):
                pass
            else:
                return "heading"

        # ====================================================
        # list: style name, text prefix, or declared numbering
        # (direct w:numPr or inherited through the style chain)
        # ====================================================
        if self.detect_lists:
            if (
                style_lower.startswith("list")
                or style_lower.startswith("列表")
                or text.startswith("- ")
                or text.startswith("• ")
                or text.startswith("* ")
            ):
                return "list"
            if numref is None and para is not None:
                numref = paragraph_numref(para)
            if numref is not None:
                return "list"

        # ====================================================
        # code heuristic
        # ====================================================
        if self.detect_code_blocks:
            if self._looks_like_code(text):
                return "code"

        # ====================================================
        # bracket convention: full-line 【…】 section labels
        # (Chinese drug-insert section convention; a text-convention verdict when style/numbering evidence is absent)
        # ====================================================
        if (
            "\n" not in text
            and len(text) <= _BRACKET_MAX_TEXT_LEN
            and _BRACKET_HEADING_RE.match(text.strip())
        ):
            return "heading"

        # ====================================================
        # numbering fallback: unstyled structural numbering
        # ====================================================
        if para is not None and self._numbering_fallback_heading(
            text,para,style_lower
        ):
            return "heading"
        return "paragraph"

    # ========================================================
    # style name helpers
    # ========================================================
    @staticmethod
    def _style_heading_level(style_name):
        """Heading level carried by the style name, en or zh ("标题 1").

        Title/Subtitle map to levels 1/2 but carry a distinct source so the
        fact "this is the document title" stays distinguishable from "this
        is a level-1 section heading".
        """
        normalized = _normalize_style_name(style_name)
        if not normalized:
            return None
        match = _STYLE_HEADING_LEVEL_RE.search(normalized)
        if match:
            return int(match.group(1))
        if _STYLE_HEADING_PLAIN_RE.match(normalized):
            return 1
        if _STYLE_TITLE_RE.match(normalized):
            return 1
        if _STYLE_SUBTITLE_RE.match(normalized):
            return 2
        return None

    @staticmethod
    def _toc_style_level(style_name):
        """TOC depth from "TOC N" / "目录 N" style names, else None."""
        normalized = _normalize_style_name(style_name)
        if not normalized:
            return None
        match = _STYLE_TOC_LEVEL_RE.match(normalized)
        if not match:
            return None
        digit = match.group(1)
        return int(digit) if digit else None

    # ========================================================
    # style resolution (bug65649: per-style-id memoization)
    # ========================================================
    @staticmethod
    def _paragraph_style_id(para):
        """Paragraph's explicit ``w:pStyle`` id (None = no explicit style).

        Delegates to the shared O(1) XML reader (docx_style_memo) —
        unlike ``para.style`` which resolves (and scans) the styles part.
        """
        return paragraph_style_id(para)

    def _style_of(self, para):
        """Resolved style object via the shared per-part memo.

        python-docx re-resolves ``para.style`` on every access; on a
        pathologically large styles.xml (bug65649: 2.5MB / 2672 styles)
        that is a linear styles scan per paragraph, and styleless
        paragraphs hit ``styles.default_for`` (a scan of ALL styles).
        Delegates to docx_style_memo so parser / fields / structure
        modules share ONE resolution per (package, style id).
        """
        return paragraph_style(para)

    def _style_name(self, para):
        """Style display name via the per-style-id memo (see _style_of)."""
        key = self._paragraph_style_id(para)
        cached = self._style_name_cache.get(key)
        if cached is not None:
            return cached
        style = self._style_of(para)
        name = ""
        if style is not None:
            try:
                name = style.name or ""
            except Exception:
                name = ""
        self._style_name_cache[key] = name
        return name

    def _style_chain_pprs(self, para):
        """pPr elements of para's style chain, memoized per style id.

        All paragraphs sharing one explicit style (or all styleless
        paragraphs sharing the document default) resolve the SAME
        chain; only the paragraph's own pPr differs. Memoizing the
        chain per style id turns _outline_level/_has_list_numbering's
        per-paragraph style walk into a dictionary hit.
        """
        key = self._paragraph_style_id(para)
        cached = self._style_chain_cache.get(key)
        if cached is not None:
            return cached
        style = self._style_of(para)
        pprs = []
        visited = set()
        while style is not None and id(style) not in visited:
            visited.add(id(style))
            pprs.append(getattr(style.element, "pPr", None))
            try:
                style = style.base_style
            except Exception:
                break
        cached = tuple(pprs)
        self._style_chain_cache[key] = cached
        return cached

    # ========================================================
    # paragraph XML helpers
    # ========================================================
    def _outline_level(self, para):
        """w:outlineLvl on the paragraph or its style inheritance chain.

        Word commonly stores ``w:outlineLvl`` on a custom paragraph style,
        not on each ``w:pPr``.  We inspect direct paragraph formatting first,
        then the resolved style and its ``basedOn`` chain.  Values are XML's
        0-based declaration converted to the runtime's 1-based level.

        bug65649: the chain walk is memoized per style id
        (``_style_chain_pprs``) — the uncached version re-resolved
        ``para.style`` for every paragraph and hung on large styles.xml.
        """
        for pPr in (para._p.pPr, *self._style_chain_pprs(para)):
            level = self._ppr_outline_level(pPr)
            if level is not None:
                return level
        return None

    @staticmethod
    def _ppr_outline_level(pPr):
        if pPr is None:
            return None
        node = pPr.find(qn("w:outlineLvl"))
        if node is None:
            return None
        try:
            level = int(node.get(qn("w:val"))) + 1
        except (TypeError, ValueError):
            return None
        return level if 1 <= level <= 9 else None

    def _has_list_numbering(self, para):
        """Whether paragraph or inherited style declares ``w:numPr``.

        Ordered-list styles often inherit their numbering through the style
        XML; only inspecting direct ``w:pPr`` would falsely promote visible
        "1. item" text into a structural heading.  Chain walk memoized per
        style id (bug65649, see ``_style_chain_pprs``).
        """
        pprs = (para._p.pPr, *self._style_chain_pprs(para))
        return any(
            pPr is not None and pPr.find(qn("w:numPr")) is not None
            for pPr in pprs
        )

    @staticmethod
    def _contains_toc_field(para):
        """Whether the paragraph carries TOC field instructions (w:instrText)."""
        try:
            instructions = para._p.xpath(".//w:instrText")
        except Exception:
            return False
        for node in instructions:
            instruction = "".join(node.itertext())
            if "TOC" in instruction.upper():
                return True
        return False

    # ========================================================
    # OMML helpers
    # ========================================================
    @staticmethod
    def _paragraph_text_with_math(
            para,
            math_pairs,
    ):
        """Rebuild paragraph text in document order, OMML included.

        python-docx joins only ``w:r``/``w:t`` runs; this walk emits every
        ``w:t`` (excluding those inside OMML, which arrive via their
        ``m:oMath`` node's verbatim linear text) at the exact position where
        Word placed the formula.  Nothing is inferred about rendering.

        B8: ``w:tab``/``w:br``/``w:cr`` are projected exactly the way
        python-docx's ``Run.text`` does ("\t" / "\n" / "\n") so formula
        paragraphs keep the same text contract as plain paragraphs;
        ``w:softHyphen`` and ``w:sym`` stay text-invisible (like python-docx)
        and are counted as evidence in ``docx_text_characters`` instead.

        Final view (docx_revision_text_prereg_v1 §3): the walk sees every
        ``w:t`` descendant, so runs inside ``w:ins`` join the text (they
        used to be silently dropped by python-docx's direct-run
        projection); ``w:del``/``w:txbxContent`` containers are excluded
        (deleted text is registry-only evidence, textbox text belongs to
        the drawing object).  r25 (adversarial face-2 ①): ``w:moveFrom``
        containers are excluded too — moved-away text is not part of the
        final view (Word renders it only in the all-marks view); it is
        preserved as object-level evidence in ``docx_revisions`` instead.
        ``w:moveTo`` (moved-here) text stays in the final view, same
        contract as ``w:ins``.  Nothing is inferred about rendering.
        """
        math_by_id = {id(node): linear for node,linear in math_pairs}
        # Single-pass DFS instead of per-node ancestor back-tracking (bug65649: in giant tables
        # of 370k nodes, the old O(nodes×depth) iterancestors() accumulated to minutes). Marking and
        # consumption must share the same pass: lxml proxies do not guarantee id() stability across traversals.
        # Child nodes are pushed in reverse order to preserve document order. Tag comparisons use module-level
        # constants (qn() has per-call parsing overhead; this loop can account for millions of calls).
        parts = []
        stack = [(para._p, False, False)]
        while stack:
            el, hidden, in_math = stack.pop()
            tag = el.tag
            nid = id(el)
            if nid in math_by_id:
                # The formula as a whole is replaced by its linearized text; the subtree is not expanded
                # (in the old behavior, oMath interior nodes were all skipped via _inside_omath).
                if not hidden:
                    parts.append(math_by_id[nid])
                continue
            nh = hidden or tag in (
                _W_DEL_TAG,
                _W_TXBX_CONTENT_TAG,
                _W_MOVE_FROM_TAG,
            )
            nm = in_math or tag == OMATH
            if not nh and not nm:
                if tag == _W_T_TAG:
                    parts.append(el.text or "")
                elif tag == _W_TAB_TAG:
                    parts.append("\t")
                elif tag in (_W_BR_TAG, _W_CR_TAG):
                    parts.append("\n")
            for child in reversed(el):
                stack.append((child, nh, nm))
        return "".join(parts)

    @staticmethod
    def _non_math_text(para):
        """Paragraph text with OMML excluded (python-docx's own
        projection); hidden containers (w:del/w:txbxContent, plus
        w:moveFrom since r25 face-2 ①) are excluded too — same
        final-view rules as the narrative walk,
        same single-pass DFS (marking and consumption in the same pass; see the notes in
        _paragraph_text_with_math; hidden/formula subtree pruning, equivalent to the old per-node filtering)."""
        parts = []
        stack = [(para._p, False, False)]
        while stack:
            el, hidden, in_math = stack.pop()
            tag = el.tag
            nh = hidden or tag in (
                _W_DEL_TAG,
                _W_TXBX_CONTENT_TAG,
                _W_MOVE_FROM_TAG,
            )
            nm = in_math or tag == OMATH
            if not nh and not nm and tag == _W_T_TAG:
                parts.append(el.text or "")
            if nh or tag == OMATH:
                continue
            for child in reversed(el):
                stack.append((child, nh, nm))
        return "".join(parts)

    @staticmethod
    def _formula_summary(formula_facts,blocks):
        """Aggregate OMML evidence for the document metadata."""
        structure_totals = Counter()
        for fact in formula_facts:
            for key,count in fact.get("structure",{}).items():
                structure_totals[key] += count
        return {
            "formula_count": len(formula_facts),
            "display_count": sum(
                1 for fact in formula_facts if fact.get("display")
            ),
            "inline_count": sum(
                1 for fact in formula_facts if not fact.get("display")
            ),
            "equation_numbers": [
                block["equation_number"]
                for block in blocks
                if block.get("type") == "formula"
                and block.get("equation_number")
            ],
            "structure_counts": dict(structure_totals),
            "mathml_unsupported_elements": sorted({
                name
                for fact in formula_facts
                for name in fact.get("mathml_unsupported_elements",[])
            }),
            "formulas": [
                {
                    "para_index": fact.get("para_index"),
                    "display": fact.get("display"),
                    "text": fact.get("text"),
                    "structure": fact.get("structure",{}),
                }
                for fact in formula_facts
            ],
        }

    # ========================================================
    # heading level resolution (priority chain)
    # ========================================================
    def _resolve_heading_level(
            self,
            para,
            style_name,
            text
    ):
        """Resolve (heading_level, source) with an explicit priority chain.

        1. w:outlineLvl direct formatting — the most specific declaration;
        2. style name ("Heading 2" / "标题 2" / Title / Subtitle);
        3. structural numbering in the text ("2.3.1 …" / "一、…"), used only
           when the paragraph has no list numbering and no terminal
           punctuation — candidate grade, never presented as style-derived.
        """
        outline_level = self._outline_level(para)
        if outline_level is not None:
            return outline_level,"docx_outline_level"

        style_level = self._style_heading_level(style_name)
        if style_level is not None:
            normalized = _normalize_style_name(style_name)
            if _STYLE_TITLE_RE.match(normalized) or _STYLE_SUBTITLE_RE.match(normalized):
                return style_level,"docx_title_style"
            return style_level,"docx_style"

        # Whole-line 【…】 section convention (drug inserts): flat level 1, a text convention
        # rather than a style/outline declaration → recorded as "bracket_fallback", classified as inferred.
        stripped = text.strip()
        if (
            "\n" not in text
            and len(stripped) <= _BRACKET_MAX_TEXT_LEN
            and _BRACKET_HEADING_RE.match(stripped)
        ):
            return 1,"bracket_fallback"

        match = match_heading_line(text)
        if (
            match is not None
            and match.rest.strip()
            and len(match.rest.strip()) >= 2
            and not self._has_list_numbering(para)
        ):
            return max(1,match.depth_hint),"numbering_fallback"

        return 1,"docx_style_default"

    # ========================================================
    # numbering fallback gate
    # ========================================================
    def _numbering_fallback_heading(
            self,
            text,
            para,
            style_lower
    ):
        """Whether an unstyled paragraph's numbering looks structural.

        Guards: no list style, no w:numPr (ordered-list items keep their own
        type), single line, length cap, no terminal punctuation (a numbered
        sentence is prose), and a matched pattern with a non-trivial title.
        """
        if not text or "\n" in text:
            return False
        if style_lower.startswith(("list", "列表")):
            return False
        if self._has_list_numbering(para):
            return False
        if len(text) > MAX_HEADING_LEN:
            return False
        if text.endswith(_TERMINAL_PUNCTUATION):
            return False
        match = match_heading_line(text)
        if match is None:
            return False
        rest = match.rest.strip()
        return len(rest) >= 2

    # ========================================================
    # toc consistency
    # ========================================================
    @staticmethod
    def _toc_consistency(toc_entries,blocks,field_detected):
        """Cross-check TOC entries against the headings actually detected.

        Entries are preserved verbatim; the agreement count is factual
        (title-equivalence against heading blocks) and never mutates either
        side. Consistency below 1 is reported, not repaired.
        """
        heading_titles = [
            block["text"]
            for block in blocks
            if block.get("type") == "heading"
        ]
        matched = 0
        for entry in toc_entries:
            if any(
                titles_equivalent(entry["text"],title)
                for title in heading_titles
            ):
                matched += 1
        return {
            "field_detected": field_detected,
            "entry_count": len(toc_entries),
            "entries": toc_entries,
            "heading_count": len(heading_titles),
            "heading_matched_count": matched,
            "consistent": (
                bool(toc_entries)
                and matched == len(toc_entries)
            ),
        }

    # ========================================================
    # heading path update
    # ========================================================
    def _update_heading_path(
            self,
            current_path,
            heading,
            level
    ):
        path = list(current_path)

        while len(path) >= level:
            path.pop()
        path.append(heading)
        return path

    # ========================================================
    # code heuristic
    # ========================================================
    def _looks_like_code(self,text):
        code_signals = [
            "def ",
            "class ",
            "import ",
            "return ",
            "{",
            "}",
            "();",
            "if (",
            "console.log"
        ]
        signal_hits = sum(
            1
            for signal in code_signals
            if signal in text
        )
        return signal_hits >= 2

    # ========================================================
    # reconstruct
    # ========================================================
    def _reconstruct_text(self,blocks):
        lines = []
        for block in blocks:
            block_type = block["type"]

            # =================================================
            # toc entry: navigation evidence, kept out of the
            # narrative text (it duplicates real headings)
            # =================================================
            if block_type == "toc_entry":
                continue

            # =================================================
            # table
            # =================================================
            if block_type == "table":
                lines.append("[TABLE]")
                for row in block["rows"]:
                    lines.append(" | ".join(row))
                continue

            # =================================================
            # display formula: verbatim OMML linear text
            # (inline math is already inside paragraph text)
            # =================================================
            if block_type == "formula":
                lines.append(block["text"])
                continue

            # =================================================
            # heading
            # =================================================
            if block_type == "heading":
                level = block.get("heading_level",1)
                prefix = "#" * level
                lines.append(f"{prefix} {block['text']}")
                continue

            lines.append(block["text"])

        return "\n\n".join(lines)


def _normalize_style_name(style_name):
    """NFKC-normalize a style name and collapse internal whitespace."""
    if not style_name:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(style_name))).strip()


def _starts_with_marker(text: str, marker: str) -> bool:
    """Whether paragraph text already spells out its list marker.

    Word normally stores automatic numbering outside the text; documents
    produced by other tools often hard-code "1. …" into the run itself.
    The check is conservative so a marker is never duplicated.
    """
    normalized = marker.strip()
    if not normalized:
        return True
    stripped = text.lstrip()
    return (
        stripped == normalized
        or stripped.startswith(normalized + " ")
        or stripped.startswith(normalized + "\t")
    )


# Containers excluded from the final-view walk (docx_revision_text_prereg_v1 §3):
# revision deletions (w:delText never enters the body, only the docx_revisions registry) and
# drawing text boxes (w:txbxContent belongs to drawing-object assets, not the body).
# r25 face-2 ①: w:moveFrom (moved-out text) is not part of the final view — Word renders it
# only in the all-markup view; excluded under the same rule as w:del, the text goes only into the
# move_records registry of docx_revisions. w:moveTo (moved-in text) stays in the body under the same contract as w:ins.
_W_DEL_TAG = qn("w:del")
_W_TXBX_CONTENT_TAG = qn("w:txbxContent")
_W_MOVE_FROM_TAG = qn("w:moveFrom")

# Hot tags inside the text-projection loop (qn() has per-call parsing overhead; calling it in the loop body
# hits the millions of calls on giant tables; hoisted to module level for constant comparison).
_W_T_TAG = qn("w:t")
_W_TAB_TAG = qn("w:tab")
_W_BR_TAG = qn("w:br")
_W_CR_TAG = qn("w:cr")


# Block-level sdt expansion cap (sdt-in-sdt protection; prereg registration item F6 does not expand
# nested inner levels; this only guards against pathological recursion).
_MAX_SDT_EXPANSION_DEPTH = 5


def _iter_body_children(container, depth=0):
    """Body-style children in document order, as (element, depth).

    Block-level ``w:sdt`` containers are transparent for the walk:
    their ``sdtContent`` is part of the body's reading order
    (docx_content_controls_prereg_v1 §3 — fixing the silent loss
    where python-docx's ``doc.paragraphs`` skips sdt-wrapped
    paragraphs entirely), so every ``w:p``/``w:tbl`` inside is
    yielded right after the sdt element itself with ``depth > 0``.
    Callers keep direct-child accounting (body cap, paragraph-index
    alignment with ``doc.paragraphs`` / image joins) on ``depth == 0``
    entries only.
    """
    for child in container:
        yield child, depth
        if (
            child.tag == qn("w:sdt")
            and depth < _MAX_SDT_EXPANSION_DEPTH
        ):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_body_children(content, depth + 1)

