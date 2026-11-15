"""Evidence-only Word-specific marker records (17-code system, p1b batch).

docx_word_marker_codes_prereg_v1: nine of the seventeen candidate reason
codes from the user spec (third batch, §12) have corpus facts and are
registered here into the existing ``docx_unresolved`` channel
(marker_contract.md §1); the remaining eight are frozen per-code with
reasons in the prereg §4 — prefer honest absence to fabrication.

Everything is evidence: no structure is ever repaired, no placeholder
text changes, and records are additive to the existing
``IMAGE_NOT_EXTRACTED`` / ``CHART_DATA_MISSING`` / SmartArt records
(marker_contract §2 codes stay untouched).
"""

from __future__ import annotations

from typing import Any

from docx.oxml.ns import qn

_DOCUMENT_PART = "word/document.xml"


def extract_marker_records(
    doc,
    package_summary: dict[str, Any],
    links_summary: dict[str, Any],
    security_summary: dict[str, Any],
    numbering_model,
) -> list[dict[str, Any]]:
    """Collect the nine p1b marker records, in frozen prereg §2 order.

    Called by ``docx_parser`` after the package/links/security summaries
    exist; every sub-check degrades silently (a broken part simply yields
    no records, it never fails the parse).
    """
    records: list[dict[str, Any]] = []

    # F1 MISSING_DOCUMENT_XML is appended by the parser right after the
    # package summary (it must survive the open failure), never here.
    records.extend(_header_footer_relationship_records(doc))
    records.extend(_unresolved_style_records(doc))
    records.extend(_unresolved_numbering_records(doc, numbering_model))
    records.extend(_broken_field_result_records(links_summary))
    records.extend(_broken_bookmark_range_records(doc))
    records.extend(_altchunk_records(doc))
    records.extend(_embedded_object_records(security_summary))
    return records


def missing_document_xml_record(package_summary: dict[str, Any]) -> dict[str, Any] | None:
    """F1: the required word/document.xml part is absent from the zip.

    Evidence comes from the existing ``docx_package`` facts — the zip is
    never re-opened.  Returned separately so the parser can append it
    *before* python-docx attempts (and fails) the open.
    """
    try:
        missing = package_summary["parts"]["required_parts_missing"]
    except Exception:
        return None
    if _DOCUMENT_PART not in missing:
        return None
    return {
        "object_type": "package",
        "object_id": _DOCUMENT_PART,
        "resolution": "unresolved",
        "reason_code": "MISSING_DOCUMENT_XML",
        "source": _DOCUMENT_PART,
        "placeholder_text": None,
    }


# ------------------------------------------------------------
# F2 header/footer relationships (sectPr references that resolve to nothing)
# ------------------------------------------------------------

def _header_footer_relationship_records(doc) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        defined_rids = set(doc.part.rels.keys())
    except Exception:
        return records
    try:
        sections = list(doc.sections)
    except Exception:
        return records
    for index, section in enumerate(sections):
        try:
            sect_pr = section._sectPr
        except Exception:
            continue
        for tag, code, object_type in (
            ("w:headerReference", "MISSING_HEADER_RELATIONSHIP", "header"),
            ("w:footerReference", "MISSING_FOOTER_RELATIONSHIP", "footer"),
        ):
            for reference in sect_pr.findall(qn(tag)):
                rid = reference.get(qn("r:id"))
                if rid is None or rid in defined_rids:
                    continue
                records.append(
                    {
                        "object_type": object_type,
                        "object_id": rid,
                        "resolution": "unresolved",
                        "reason_code": code,
                        "source": f"{_DOCUMENT_PART}#sect-{index}",
                        "placeholder_text": None,
                        "reference_type": reference.get(qn("w:type")),
                    }
                )
    return records


# ------------------------------------------------------------
# F3 style ids referenced in the body but never defined in styles.xml
# ------------------------------------------------------------

def _unresolved_style_records(doc) -> list[dict[str, Any]]:
    try:
        defined = {style.style_id for style in doc.styles}
    except Exception:
        return []

    referenced: list[tuple[str, str]] = []  # (style_id, reference_kind)
    seen: set[tuple[str, str]] = set()
    try:
        body = doc.element.body
        for kind_tag, kind_name in (
            ("w:pStyle", "pStyle"),
            ("w:rStyle", "rStyle"),
            ("w:tblStyle", "tblStyle"),
        ):
            for node in body.iter(qn(kind_tag)):
                style_id = node.get(qn("w:val"))
                if not style_id:
                    continue
                key = (style_id, kind_name)
                if key in seen:
                    continue
                seen.add(key)
                referenced.append(key)
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    for style_id, kind_name in referenced:
        if style_id in defined:
            continue
        records.append(
            {
                "object_type": "style",
                "object_id": style_id,
                "resolution": "unresolved",
                "reason_code": "UNRESOLVED_STYLE_ID",
                "source": _DOCUMENT_PART,
                "placeholder_text": None,
                "reference_kind": kind_name,
            }
        )
    return records


# ------------------------------------------------------------
# F4 numbering ids referenced in the body but never defined
# ------------------------------------------------------------

def _unresolved_numbering_records(doc, numbering_model) -> list[dict[str, Any]]:
    # numId "0" is Word's explicit "remove numbering" sentinel — never a
    # reference (docx_structure.py L35 convention, frozen in prereg §2).
    try:
        defined = set(numbering_model.num_to_abstract.keys())
    except Exception:
        return []

    referenced: list[str] = []
    seen: set[str] = set()
    try:
        for num_id_node in doc.element.body.iter(qn("w:numId")):
            num_id = num_id_node.get(qn("w:val"))
            if not num_id or num_id == "0" or num_id in seen:
                continue
            seen.add(num_id)
            referenced.append(num_id)
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    for num_id in referenced:
        if num_id in defined:
            continue
        records.append(
            {
                "object_type": "numbering",
                "object_id": f"num-{num_id}",
                "resolution": "unresolved",
                "reason_code": "UNRESOLVED_NUMBERING_ID",
                "source": _DOCUMENT_PART,
                "placeholder_text": None,
            }
        )
    return records


# ------------------------------------------------------------
# F5 field cached results that no longer mirror their bookmark
# ------------------------------------------------------------

def _broken_field_result_records(links_summary: dict[str, Any]) -> list[dict[str, Any]]:
    # The comparison fact is carried by the existing
    # docx_links.field_result_mismatches key (docx_field_freshness prereg
    # precedent: never duplicate a comparison, reference it).
    records: list[dict[str, Any]] = []
    try:
        mismatches = links_summary["field_result_mismatches"]
    except Exception:
        return records
    for mismatch in mismatches:
        records.append(
            {
                "object_type": "field",
                "object_id": str(mismatch.get("target") or ""),
                "resolution": "unresolved",
                "reason_code": "BROKEN_FIELD_RESULT",
                "source": str(mismatch.get("location") or _DOCUMENT_PART),
                "placeholder_text": None,
                "expected": mismatch.get("expected"),
                "cached": mismatch.get("cached"),
            }
        )
    return records


# ------------------------------------------------------------
# F6 bookmarkStart ids never closed by a bookmarkEnd (per part)
# ------------------------------------------------------------

def _part_bookmark_records(element, part_name: str) -> list[dict[str, Any]]:
    starts: list[tuple[str, str | None]] = []  # (id, name) in document order
    ended: set[str] = set()
    seen_start_ids: set[str] = set()
    for node in element.iter(qn("w:bookmarkStart")):
        node_id = node.get(qn("w:id"))
        if node_id is None:
            continue
        if node_id not in seen_start_ids:
            seen_start_ids.add(node_id)
            starts.append((node_id, node.get(qn("w:name"))))
    for node in element.iter(qn("w:bookmarkEnd")):
        node_id = node.get(qn("w:id"))
        if node_id is not None:
            ended.add(node_id)

    records: list[dict[str, Any]] = []
    for node_id, name in starts:
        if node_id in ended:
            continue
        records.append(
            {
                "object_type": "bookmark",
                "object_id": name if name else f"id-{node_id}",
                "resolution": "unresolved",
                "reason_code": "BROKEN_BOOKMARK_RANGE",
                "source": part_name,
                "placeholder_text": None,
            }
        )
    return records


def _broken_bookmark_range_records(doc) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        records.extend(_part_bookmark_records(doc.element.body, _DOCUMENT_PART))
    # A corrupted document body failing the bookmark scan skips that
    # part (observation, never a blocker).
    except Exception:
        pass
    # Header/footer parts follow the same per-part rule; linked-to-previous
    # parts and repeats are skipped (docx_fields._paragraph_locations
    # dedup precedent).
    seen_parts: set[int] = set()
    try:
        sections = list(doc.sections)
    except Exception:
        return records
    for section_index, section in enumerate(sections):
        for part_name, part in (
            ("header", section.header),
            ("footer", section.footer),
            ("first_page_header", section.first_page_header),
            ("first_page_footer", section.first_page_footer),
            ("even_page_header", section.even_page_header),
            ("even_page_footer", section.even_page_footer),
        ):
            if part is None or id(part) in seen_parts:
                continue
            try:
                if part.is_linked_to_previous:
                    continue
            # python-docx may raise arbitrary exceptions on attribute
            # access to a corrupted part; a failed probe is treated as
            # an independent part.
            except Exception:
                pass
            seen_parts.add(id(part))
            try:
                element = part.part._element
            except Exception:
                continue
            try:
                partname = str(part.part.partname)
            except Exception:
                partname = f"{part_name}:sec{section_index}"
            records.extend(_part_bookmark_records(element, partname))
    return records


# ------------------------------------------------------------
# F7 altChunk placeholders (external content is never expanded, by design)
# ------------------------------------------------------------

def _altchunk_records(doc) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        rels = doc.part.rels
        for node in doc.element.body.iter(qn("w:altChunk")):
            rid = node.get(qn("r:id"))
            if rid is None:
                continue
            try:
                target = str(rels[rid].target_ref)
            except Exception:
                target = None
            records.append(
                {
                    "object_type": "alt_chunk",
                    "object_id": rid,
                    "resolution": "unsupported",
                    "reason_code": "ALTCHUNK_NOT_EXPANDED",
                    "source": _DOCUMENT_PART,
                    "placeholder_text": None,
                    "target": target,
                }
            )
    except Exception:
        return records
    return records


# ------------------------------------------------------------
# F8 embedded object parts (OLE internals are never extracted, by design)
# ------------------------------------------------------------

def _embedded_object_records(security_summary: dict[str, Any]) -> list[dict[str, Any]]:
    # One record per word/embeddings/ part; o:OLEObject declarations stay
    # in the docx_security evidence and are never duplicated here.
    records: list[dict[str, Any]] = []
    try:
        embedded_files = security_summary["embedded_objects"]["embedded_files"]
    except Exception:
        return records
    for embedded in embedded_files:
        records.append(
            {
                "object_type": "embedded_object",
                "object_id": str(embedded.get("name") or ""),
                "resolution": "unsupported",
                "reason_code": "EMBEDDED_OBJECT_NOT_EXTRACTED",
                "source": "word/embeddings",
                "placeholder_text": None,
            }
        )
    return records
