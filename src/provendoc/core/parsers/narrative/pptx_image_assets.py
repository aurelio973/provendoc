"""PPTX image asset extraction: original bytes plus placement evidence.

Mirrors the docx_image_assets contract on the presentation side: for
every ``p:pic`` on a slide, the referenced image part (``a:blip``
``r:embed`` → slide rels → ``ppt/media/…``) is read bit-exact from the
OOXML zip package, identified by a content-hash ``asset_id`` and recorded
with its placement facts (slide number, shape name/alt text, placed
extent in cm, pixel size and effective dpi from the stream header).

Original bytes are stored, never re-encoded.  A broken relationship
target or unreadable part is reported as ``extracted=False`` with the
reason — never as a silent skip, and never as a parser failure.  The
narrative text is not touched: the join point to the manifest is the
slide number plus shape facts (pptx slides render no per-image
placeholder line; the manifest is evidence, not a text layer).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from provendoc.core.image_assets.dimensions import (
    dpi_from_cm,
    pixel_dimensions,
)
from provendoc.core.image_assets.schema import (
    PAYLOAD_KEY,
    asset_id_for,
    image_format_for,
)
from provendoc.core.parsers.narrative.pptx_evidence import (
    _SLIDE_NAME_RE,
    slide_rels,
)

_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

_EMU_PER_CM = 360000.0
_MAX_PLACEMENTS = 200


def _emu_to_cm(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round(int(value) / _EMU_PER_CM, 2)
    except ValueError:
        return None


def extract_pptx_image_assets(file_path: str | Path) -> dict[str, Any]:
    """Collect per-placement image asset records with original bytes.

    The result lands under the ``pptx_image_assets`` metadata key and
    follows the image-assets manifest schema (asset ids join the
    extraction facts to the binary store; the pptx narrative text
    carries no placeholder, the slide facts are the join point).
    """
    records: list[dict[str, Any]] = []

    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
        slides = sorted(
            (int(m.group(1)), name)
            for name in names
            if (m := _SLIDE_NAME_RE.match(name))
        )

        for number, name in slides:
            if len(records) >= _MAX_PLACEMENTS:
                break
            try:
                root = ElementTree.fromstring(zf.read(name))
            except ElementTree.ParseError:
                continue
            rels = slide_rels(zf, names, number)

            for pic in root.iter(f"{{{_NS['p']}}}pic"):
                if len(records) >= _MAX_PLACEMENTS:
                    break
                _record_placement(pic, number, rels, zf, records)

    return {
        "extractor": "pptx_image_assets_v1",
        "records": records,
    }


def _record_placement(
    pic, slide_number, rels, zf, records
) -> None:
    """Append one placement record from a ``p:pic`` element."""
    blip = pic.find(f".//{{{_NS['a']}}}blip")
    rel_id = (
        blip.get(f"{{{_NS['r']}}}embed") if blip is not None else None
    )

    cnvpr = pic.find(
        f".//{{{_NS['p']}}}nvPicPr/{{{_NS['p']}}}cNvPr"
    )
    name = (cnvpr.get("name") if cnvpr is not None else None) or None
    alt_text = (
        (cnvpr.get("descr") if cnvpr is not None else None) or None
    )

    ext = pic.find(
        f".//{{{_NS['p']}}}spPr/{{{_NS['a']}}}xfrm/{{{_NS['a']}}}ext"
    )
    width_cm = _emu_to_cm(ext.get("cx") if ext is not None else None)
    height_cm = _emu_to_cm(ext.get("cy") if ext is not None else None)

    part_name = None
    data: bytes | None = None
    not_extracted_reason = None
    if rel_id and rel_id in rels:
        part_name = rels[rel_id]
        try:
            data = zf.read(part_name)
        except KeyError:
            data = None
        if data is None:
            not_extracted_reason = "part_not_readable"
    else:
        not_extracted_reason = "missing_relationship"

    if data:
        asset_id = asset_id_for(data)
        existing = next(
            (r for r in records if r.get("asset_id") == asset_id),
            None,
        )
        ref_count = (existing["ref_count"] + 1) if existing else 1
        # Pixel facts from the stream header (PNG IHDR / JPEG SOF / GIF
        # / BMP) — never decoded; effective_dpi is arithmetic over the
        # header size and the placed extent (cm).  A None is a fact gap
        # (unparseable header / missing extent), never a guess.
        pixel_width, pixel_height = pixel_dimensions(data)
        records.append(
            {
                "asset_id": asset_id,
                "sha256": hashlib.sha256(data).hexdigest(),
                "extracted": True,
                "format": image_format_for(part_name, None),
                "byte_size": len(data),
                "part_name": part_name,
                "ref_count": ref_count,
                "slide_number": slide_number,
                "name": name,
                "alt_text": alt_text,
                "has_alt_text": bool(alt_text),
                "pixel_width": pixel_width,
                "pixel_height": pixel_height,
                "effective_dpi": dpi_from_cm(pixel_width, width_cm),
                "width_cm": width_cm,
                "height_cm": height_cm,
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
                "slide_number": slide_number,
                "name": name,
                "alt_text": alt_text,
                "width_cm": width_cm,
                "height_cm": height_cm,
            }
        )


__all__ = ["extract_pptx_image_assets"]
