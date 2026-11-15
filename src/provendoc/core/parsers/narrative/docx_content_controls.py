# ============================================================
# Narrative Domain
# DOCX content-control (w:sdt) records (docx_content_controls_v1)
# ============================================================
"""Object-level content-control records for the DOCX body part.

Companion registry to the narrative walk fix
(docx_content_controls_prereg_v1 §2/§3): block-level ``w:sdt``
``sdtContent`` is body — its paragraphs/tables now join the narrative
walk (fixing the silent loss proven by the pre-run probe) — and this
module registers the *structural* evidence: control type, tag/alias/
id/lock attributes, content value, nesting count and data binding.
Structure tags are evidence; control semantics (form-field
validation, dropdown option lists) are deliberately not interpreted.

Facts (``metadata["docx_content_controls"]``, written only when the
body actually has ``w:sdt`` elements — write only when non-empty):

- ``records``: one record per top-level ``w:sdt`` in body XML document
  order (nested sdts do not get their own records — they are counted
  in the outer record's ``nested_count``), 100 records max (overflow
  in ``truncated_count``)
- ``control_count`` / ``by_type`` / ``locked_count`` /
  ``data_binding_count``: full-count facts over all top-level sdts

Fact boundaries (documented, not guessed):

- only the document body part is scanned (header/footer parts are
  not scanned — same boundary as ``docx_review``)
- every field is taken verbatim from the XML (missing → None, never
  inferred); the checkbox ``w14:checked`` value stays a raw string
- ``w:dataBinding`` is registered only (storeItemID/xpath attribute
  values); the bound customXml part is not parsed or recursed into
  (batch 4 §8: customXml default body: no)
"""
from __future__ import annotations

from collections import Counter

# lxml Clark notation: element tags and attribute names are
# "{{namespace-uri}}local" — the braces are part of the name.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"

_W_SDT = f"{{{_W_NS}}}sdt"
_W_SDT_PR = f"{{{_W_NS}}}sdtPr"
_W_SDT_CONTENT = f"{{{_W_NS}}}sdtContent"
_W_P = f"{{{_W_NS}}}p"
_W_TBL = f"{{{_W_NS}}}tbl"
_W_T = f"{{{_W_NS}}}t"
_W_VAL = f"{{{_W_NS}}}val"

# sdtPr type child element → control_type literal (element-name mapped
# to literals, no semantic interpretation; no type element →
# unclassified) — frozen in prereg §2.
_CONTROL_TYPE_BY_TAG = {
    f"{{{_W_NS}}}text": "text",
    f"{{{_W_NS}}}richText": "rich_text",
    f"{{{_W14_NS}}}checkbox": "checkbox",
    f"{{{_W_NS}}}dropDownList": "dropdown",
    f"{{{_W_NS}}}comboBox": "combo",
    f"{{{_W_NS}}}date": "date_picker",
    f"{{{_W_NS}}}picture": "picture",
    f"{{{_W14_NS}}}repeatingSection": "repeating_section",
    f"{{{_W_NS}}}group": "group",
}

EXTRACTOR = "docx_content_controls_v1"
_MAX_TEXT_CHARS = 200
_MAX_RECORDS = 100


def _attribute_value(sdt_pr, child_tag):
    """``w:val`` attribute of the first ``child_tag`` child, or None."""
    if sdt_pr is None:
        return None
    child = sdt_pr.find(child_tag)
    if child is None:
        return None
    value = child.get(_W_VAL)
    return value if value else None


def _control_type(sdt_pr):
    """Map the sdtPr type element to its frozen literal."""
    if sdt_pr is None:
        return "unclassified"
    for child in sdt_pr:
        literal = _CONTROL_TYPE_BY_TAG.get(child.tag)
        if literal:
            return literal
    return "unclassified"


def _content_value(sdt_content):
    """Join all ``w:t`` nodes under sdtContent (full projection), truncated at
    ``_MAX_TEXT_CHARS``; empty joins read as ``None``."""
    if sdt_content is None:
        return None
    parts = []
    for node in sdt_content.iter(_W_T):
        text = node.text
        if isinstance(text, str):
            parts.append(text)
    joined = "".join(parts)
    if not joined:
        return None
    return joined[:_MAX_TEXT_CHARS]


def _control_level(sdt, sdt_content):
    """``block`` (sdtContent has direct w:p/w:tbl children), ``run``
    (sdt sits inside a paragraph), else ``None``."""
    if sdt_content is not None:
        for child in sdt_content:
            if child.tag in (_W_P, _W_TBL):
                return "block"
    for ancestor in sdt.iterancestors():
        if ancestor.tag == _W_P:
            return "run"
    return None


def _nested_count(sdt):
    """Count of w:sdt descendants (the sdt itself excluded)."""
    return max(0, sum(1 for _ in sdt.iter(_W_SDT)) - 1)


def _data_binding(sdt_pr):
    """w:dataBinding attribute values, registered only (F4)."""
    if sdt_pr is None:
        return None
    element = sdt_pr.find(f"{{{_W_NS}}}dataBinding")
    if element is None:
        return None
    return {
        "store_item_id": element.get(f"{{{_W_NS}}}storeItemID") or None,
        "xpath": element.get(f"{{{_W_NS}}}xpath") or None,
    }


def extract_content_controls_summary(doc, block_index_by_element=None):
    """Object-level content-control records for the body part.

    Args:
        doc: an opened ``docx.Document`` instance.
        block_index_by_element: optional ``{id(element): docx_blocks
            index}`` map from the parser body walk (block-level sdts
            only); used to fill ``block_index``, else ``None``.

    Returns:
        dict for ``metadata["docx_content_controls"]`` or ``None``
        when the body has no ``w:sdt`` elements (write only when
        non-empty).
    """
    body = doc.element.body
    block_index_by_element = block_index_by_element or {}
    records = []
    by_type = Counter()
    truncated_count = 0
    control_count = 0
    locked_count = 0
    data_binding_count = 0
    for sdt in body.iter(_W_SDT):
        if any(
            ancestor.tag == _W_SDT for ancestor in sdt.iterancestors()
        ):
            # Nested sdt (F6 counting tier): inner levels are not
            # expanded into records; the outer record's nested_count
            # expresses them.
            continue
        control_count += 1
        sdt_pr = sdt.find(_W_SDT_PR)
        sdt_content = sdt.find(_W_SDT_CONTENT)
        control_type = _control_type(sdt_pr)
        by_type[control_type] += 1
        locked = _attribute_value(sdt_pr, f"{{{_W_NS}}}lock")
        if locked is not None:
            locked_count += 1
        if _data_binding(sdt_pr) is not None:
            data_binding_count += 1
        if len(records) >= _MAX_RECORDS:
            truncated_count += 1
            continue
        try:
            records.append({
                "control_type": control_type,
                "tag": _attribute_value(sdt_pr, f"{{{_W_NS}}}tag"),
                "title": _attribute_value(sdt_pr, f"{{{_W_NS}}}alias"),
                "id": _attribute_value(sdt_pr, f"{{{_W_NS}}}id"),
                "locked": locked,
                "value": _content_value(sdt_content),
                "level": _control_level(sdt, sdt_content),
                "nested_count": _nested_count(sdt),
                "data_binding": _data_binding(sdt_pr),
                "block_index": block_index_by_element.get(id(sdt)),
            })
        except Exception:
            # One bad record does not spoil the batch (prereg §2):
            # skip on a single record-construction exception.
            continue
    if not control_count:
        return None
    return {
        "extractor": EXTRACTOR,
        "control_count": control_count,
        "by_type": dict(by_type),
        "locked_count": locked_count,
        "data_binding_count": data_binding_count,
        "truncated_count": truncated_count,
        "records": records,
        "note": (
            "content-control text (sdtContent) is part of the "
            "narrative body and has been joined into the walk; "
            "structural tags are evidence only — control semantics "
            "(form-field validation, dropdown options) are not "
            "interpreted; nested sdts count into the outer record's "
            "nested_count; body part scanned only (header/footer "
            "parts are not scanned); records capped at 100 "
            "(truncated_count), value truncated at 200 chars"
        ),
    }
