"""Evidence-only DOCX image extraction (B11, gap register §8).

Per-image facts for every ``w:drawing`` that hosts a picture: placement
(inline / anchored), alt text (``wp:docPr@descr``), the image part it points
at, the structural context (body / table cell / text box) and the caption
paragraph sitting immediately before or after the anchor paragraph.

Everything is evidence: alt text and captions are recorded as authored,
missing alt text is reported as missing (never invented), and images are
never re-embedded or re-positioned.
"""

from __future__ import annotations

import re
from typing import Any

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Same caption vocabulary the PDF figure reconstruction accepts, extended
# with the explicit "table" wording DOCX captions often use.
_IMAGE_CAPTION_RE = re.compile(
    r"^(?:figure|fig\.?|chart|image|table|图|表)\s*[\d]+", re.IGNORECASE
)

_MAX_IMAGES = 50
_MAX_TEXT_CHARS = 120

_EMU_PER_CM = 360000.0


def _emu_to_cm(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round(int(value) / _EMU_PER_CM, 2)
    except ValueError:
        return None


def _paragraph_text(element) -> str:
    return "".join(
        node.text or "" for node in element.iter(f"{{{_W_NS}}}t")
    ).strip()


def _ancestor_context(element) -> str:
    """Structural context of a drawing: body, table cell or text box."""
    parent = element.getparent()
    while parent is not None:
        tag = parent.tag
        if tag == f"{{{_W_NS}}}txbxContent":
            return "textbox"
        if tag == f"{{{_W_NS}}}tbl":
            return "table_cell"
        if tag == f"{{{_W_NS}}}body":
            return "body"
        parent = parent.getparent()
    return "unknown"


def _anchor_paragraph(element):
    parent = element.getparent()
    while parent is not None:
        if parent.tag == f"{{{_W_NS}}}p":
            return parent
        parent = parent.getparent()
    return None


def _sibling_caption(paragraph, direction: str) -> str | None:
    """Caption text of the nearest same-level sibling paragraph, if any."""
    if paragraph is None:
        return None
    sibling = paragraph.getprevious() if direction == "before" else paragraph.getnext()
    steps = 0
    while sibling is not None and steps < 2:
        if sibling.tag == f"{{{_W_NS}}}p":
            text = _paragraph_text(sibling)
            if _IMAGE_CAPTION_RE.match(text):
                return text[:_MAX_TEXT_CHARS]
            steps += 1
        sibling = (
            sibling.getprevious() if direction == "before" else sibling.getnext()
        )
    return None


def extract_image_summary(doc) -> dict[str, Any]:
    """Collect per-image evidence from one docx body.

    ``doc`` is an opened python-docx Document.  The summary lands under the
    ``docx_images`` metadata key.
    """
    body = doc.element.body
    rels = doc.part.rels

    images: list[dict[str, Any]] = []
    paragraph_index = -1
    for element in body.iter():
        if element.tag == f"{{{_W_NS}}}p":
            paragraph_index += 1
        if element.tag not in {f"{{{_WP_NS}}}inline", f"{{{_WP_NS}}}anchor"}:
            continue
        # Only picture-carrying drawings are image facts; charts live under
        # docx_charts and shapes/text boxes under docx_layout.
        blip = element.find(
            f".//{{{_PIC_NS}}}blipFill/{{{_A_NS}}}blip"
        )
        if blip is None:
            continue
        rel_id = blip.get(f"{{{_R_NS}}}embed")
        image_part = None
        if rel_id and rel_id in rels:
            image_part = rels[rel_id].target_ref
        doc_pr = element.find(f"{{{_WP_NS}}}docPr")
        extent = element.find(f"{{{_WP_NS}}}extent")
        alt_text = (doc_pr.get("descr") if doc_pr is not None else None) or None
        anchor_paragraph = _anchor_paragraph(element)
        caption_before = _sibling_caption(anchor_paragraph, "before")
        caption_after = _sibling_caption(anchor_paragraph, "after")
        images.append(
            {
                "paragraph_index": paragraph_index,
                "placement": "inline" if element.tag.endswith("inline") else "anchored",
                "name": (doc_pr.get("name") if doc_pr is not None else None),
                "alt_text": alt_text,
                "has_alt_text": bool(alt_text),
                "image_part": image_part,
                "width_cm": _emu_to_cm(extent.get("cx") if extent is not None else None),
                "height_cm": _emu_to_cm(extent.get("cy") if extent is not None else None),
                "context": _ancestor_context(element),
                "caption_before": caption_before,
                "caption_after": caption_after,
            }
        )
        if len(images) >= _MAX_IMAGES:
            break

    return {
        "image_count": len(images),
        "inline_count": sum(1 for image in images if image["placement"] == "inline"),
        "anchored_count": sum(
            1 for image in images if image["placement"] == "anchored"
        ),
        "alt_text_count": sum(1 for image in images if image["has_alt_text"]),
        "missing_alt_text_count": sum(
            1 for image in images if not image["has_alt_text"]
        ),
        "captioned_count": sum(
            1 for image in images if image["caption_before"] or image["caption_after"]
        ),
        "in_table_count": sum(1 for image in images if image["context"] == "table_cell"),
        "in_textbox_count": sum(
            1 for image in images if image["context"] == "textbox"
        ),
        "samples": images[:_MAX_IMAGES],
        "note": (
            "per-image facts (placement, alt text, image part, context, "
            "adjacent captions) are recorded as evidence; missing alt text is "
            "reported as missing and captions are never moved or invented"
        ),
    }


__all__ = ["extract_image_summary"]
