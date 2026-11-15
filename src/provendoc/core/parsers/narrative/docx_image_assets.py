"""DOCX image asset extraction: original bytes plus placement evidence.

Extends the B11 evidence-only image summary (``docx_images``) with the
binary side: for every ``w:drawing`` hosting a picture, the referenced
image part is read bit-exact from the OOXML zip package, identified by a
content-hash ``asset_id`` and recorded with its placement facts
(paragraph index, inline/anchored, structural context, alt text, adjacent
captions).  The same image placed twice deduplicates to one asset with a
reference count.

Original bytes are stored, never re-encoded.  A broken relationship
target or unreadable part is reported as ``extracted=False`` with the
reason — never as a silent skip, and never as a parser failure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from provendoc.core.image_assets.dimensions import (
    dpi_from_cm,
    pixel_dimensions,
)
from provendoc.core.image_assets.schema import (
    PAYLOAD_KEY,
    asset_id_for,
    image_format_for,
)
from provendoc.core.parsers.narrative.docx_images import (
    _EMU_PER_CM,
    _ancestor_context,
    _anchor_paragraph,
    _sibling_caption,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_MAX_ASSET_PLACEMENTS = 200


def _emu_to_cm(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round(int(value) / _EMU_PER_CM, 2)
    except ValueError:
        return None


def extract_image_assets(doc, file_path: str | Path) -> dict[str, Any]:
    """Collect per-placement image asset records with original bytes.

    ``doc`` is an opened python-docx Document; ``file_path`` is the source
    package the bytes are read from.  The result lands under the
    ``docx_image_assets`` metadata key and follows the image-assets
    manifest schema (asset ids join to the narrative placeholders).
    """
    package_path = Path(file_path)
    body = doc.element.body
    rels = doc.part.rels

    # Read image parts bit-exact from the zip; the extraction stays
    # independent of python-docx package internals.
    part_bytes: dict[str, bytes] = {}
    try:
        with ZipFile(package_path) as archive:
            for name in archive.namelist():
                if name.startswith("word/media/"):
                    part_bytes[name] = archive.read(name)
    except Exception:
        # The package was already opened by python-docx above, so a read
        # failure here is an honest per-document defect, not a crash.
        part_bytes = {}

    records: list[dict[str, Any]] = []

    _W_P_TAG = f"{{{_W_NS}}}p"
    _DRAWING_TAGS = {
        f"{{{_WP_NS}}}inline",
        f"{{{_WP_NS}}}anchor",
    }

    def _record_placement(element, paragraph_index) -> bool:
        """Append one placement record; returns False if not a drawing."""
        blip = element.find(
            f".//{{{_PIC_NS}}}blipFill/{{{_A_NS}}}blip"
        )
        if blip is None:
            return False
        rel_id = blip.get(f"{{{_R_NS}}}embed")
        doc_pr = element.find(f"{{{_WP_NS}}}docPr")
        extent = element.find(f"{{{_WP_NS}}}extent")
        alt_text = (doc_pr.get("descr") if doc_pr is not None else None) or None
        anchor_paragraph = _anchor_paragraph(element)
        caption_before = _sibling_caption(anchor_paragraph, "before")
        caption_after = _sibling_caption(anchor_paragraph, "after")

        part_name = None
        data: bytes | None = None
        not_extracted_reason = None
        if rel_id and rel_id in rels:
            target = rels[rel_id].target_ref
            # ``target_ref`` is package-relative ("media/image1.png" style);
            # normalize to the zip's "word/media/…" naming.
            part_name = target.lstrip("/")
            if not part_name.startswith("word/"):
                part_name = f"word/{part_name}"
            data = part_bytes.get(part_name)
            if data is None:
                not_extracted_reason = "part_not_readable"
        else:
            not_extracted_reason = "missing_relationship"

        placement = (
            "inline" if element.tag.endswith("inline") else "anchored"
        )
        context = _ancestor_context(element)
        name = (doc_pr.get("name") if doc_pr is not None else None)

        if data:
            asset_id = asset_id_for(data)
            existing = next(
                (r for r in records if r.get("asset_id") == asset_id),
                None,
            )
            ref_count = (existing["ref_count"] + 1) if existing else 1
            # Pixel facts from the stream header (PNG IHDR / JPEG SOF /
            # GIF / BMP) — never decoded; effective_dpi is arithmetic over
            # the header size and the placed extent (cm).  A None is a fact
            # gap (unparseable header / missing extent), never a guess.
            pixel_width, pixel_height = pixel_dimensions(data)
            width_cm = _emu_to_cm(
                extent.get("cx") if extent is not None else None
            )
            height_cm = _emu_to_cm(
                extent.get("cy") if extent is not None else None
            )
            records.append(
                {
                    "asset_id": asset_id,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "extracted": True,
                    "format": image_format_for(part_name, None),
                    "byte_size": len(data),
                    "part_name": part_name,
                    "ref_count": ref_count,
                    "paragraph_index": paragraph_index,
                    "placement": placement,
                    "name": name,
                    "alt_text": alt_text,
                    "has_alt_text": bool(alt_text),
                    "pixel_width": pixel_width,
                    "pixel_height": pixel_height,
                    "effective_dpi": dpi_from_cm(pixel_width, width_cm),
                    "width_cm": width_cm,
                    "height_cm": height_cm,
                    "context": context,
                    "caption_before": caption_before,
                    "caption_after": caption_after,
                    PAYLOAD_KEY: data,
                }
            )
        else:
            records.append(
                {
                    "asset_id": None,
                    "extracted": False,
                    "not_extracted_reason": not_extracted_reason or "unknown",
                    "part_name": part_name,
                    "paragraph_index": paragraph_index,
                    "placement": placement,
                    "name": name,
                    "alt_text": alt_text,
                    "width_cm": _emu_to_cm(
                        extent.get("cx") if extent is not None else None
                    ),
                    "height_cm": _emu_to_cm(
                        extent.get("cy") if extent is not None else None
                    ),
                    "context": context,
                    "caption_before": caption_before,
                    "caption_after": caption_after,
                }
            )
        return True

    # Two passes with explicit index spaces:
    # 1. body-level paragraphs only — the index matches the parser's
    #    ``doc.paragraphs`` enumeration, so the placeholder loop can join
    #    records by paragraph_index without off-by-N drift (a table between
    #    two paragraphs would otherwise shift every later index).  Drawings
    #    nested inside these paragraphs (textboxes included) are covered
    #    by ``paragraph.iter()``.
    # 2. drawings inside non-paragraph containers (tables, content
    #    controls, …) — still recorded in the manifest, but
    #    paragraph_index is None: there is no body-paragraph position to
    #    join to, and inventing one would lie.  Splitting by direct child
    #    of body (not by element identity — lxml proxies are unstable
    #    across iterations) guarantees each drawing is visited exactly
    #    once.
    body_children = list(body)
    for paragraph_index, paragraph in enumerate(
        child for child in body_children if child.tag == _W_P_TAG
    ):
        if len(records) >= _MAX_ASSET_PLACEMENTS:
            break
        for element in paragraph.iter():
            if len(records) >= _MAX_ASSET_PLACEMENTS:
                break
            if element.tag in _DRAWING_TAGS:
                _record_placement(element, paragraph_index)

    for container in body_children:
        if container.tag == _W_P_TAG:
            continue
        if len(records) >= _MAX_ASSET_PLACEMENTS:
            break
        for element in container.iter():
            if len(records) >= _MAX_ASSET_PLACEMENTS:
                break
            if element.tag in _DRAWING_TAGS:
                _record_placement(element, None)

    return {
        "extractor": "docx_image_assets_v1",
        "records": records,
    }


def placement_placeholder(record: dict[str, Any]) -> str:
    """Narrative placeholder text for one image placement.

    The placeholder is the join point between the text stream and the
    manifest: it carries the asset id plus, when authored, the caption —
    the caption is repeated on both sides by design so either side can
    be consumed standalone.
    """
    if not record.get("extracted"):
        reason = record.get("not_extracted_reason") or "unknown"
        return f"[Image: not_extracted ({reason})]"
    parts = [f"[Image: {record['asset_id']}"]
    caption = record.get("caption_after") or record.get("caption_before")
    if caption:
        parts.append(f" | Caption: {caption}")
    alt = record.get("alt_text")
    if alt:
        parts.append(f" | Alt: {alt[:120]}")
    parts.append("]")
    return "".join(parts)


__all__ = ["extract_image_assets", "placement_placeholder"]
