"""DOCX SmartArt (diagram) text extraction from the DrawingML parts.

Word stores SmartArt diagrams the same way PowerPoint does: the body
carries a ``dgm:relIds`` reference (``r:dm`` → data part) inside a
``w:drawing``, and the visible text lives in ``word/diagrams/dataN.xml``
(``dgm:pt`` semantic points → ``dgm:t`` → ``a:t``).  This module ports
the PPTX parser's evidence-only approach to DOCX so a flow chart's node
labels become narrative text instead of an uninterpreted "diagram"
declaration.

Only semantic points (``dgm:pt`` with a modelId) contribute text;
connector and layout nodes carry none.  A missing or unreadable data
part yields an honest placeholder — the diagram is never guessed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_RELS_PART = "word/_rels/document.xml.rels"
_DIAGRAM_RELTYPE_SUFFIX = "/diagramData"

# Flood guard: pathological packages cannot flood the narrative.
_MAX_DIAGRAMS = 100
_MAX_ITEMS_PER_DIAGRAM = 200


def _dgm(local: str) -> str:
    return f"{{{_DGM_NS}}}{local}"


def _data_part_items(root: ET.Element) -> list[str]:
    """Semantic-point texts of one diagrams/data part (port of PPTX)."""

    items: list[str] = []
    for point in root.iter(_dgm("pt")):
        if point.get("modelId") is None:
            # Non-semantic point (connection / layout node).
            continue
        texts = [
            node.text
            for node in point.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
            if node.text
        ]
        value = " ".join(texts).strip()
        if value:
            items.append(value)
        if len(items) >= _MAX_ITEMS_PER_DIAGRAM:
            break
    return items


class SmartArtResolver:
    """Diagram data parts preloaded once per package.

    The constructor maps every diagram-data relationship
    (``r:dm``) to its parsed item list; paragraph lookups then need no
    package access, so the parser loop stays cheap.
    """

    def __init__(self) -> None:
        """Initialize an empty resolver state."""
        self._by_rel_id: dict[str, dict[str, Any]] = {}
        self.parse_errors: list[str] = []
        self.truncated = False

    @classmethod
    def from_package(cls, file_path: str | Path) -> "SmartArtResolver":
        """Preload every diagram-data relationship and its entries from the package."""
        resolver = cls()
        try:
            with ZipFile(Path(file_path)) as archive:
                names = set(archive.namelist())
                if _RELS_PART not in names:
                    return resolver
                try:
                    rels_root = ET.fromstring(archive.read(_RELS_PART))
                except ET.ParseError as error:
                    resolver.parse_errors.append(
                        f"document rels unreadable: {error}"
                    )
                    return resolver
                for rel in rels_root:
                    rel_id = rel.get("Id")
                    rel_type = rel.get("Type") or ""
                    target = rel.get("Target") or ""
                    if not rel_id or not rel_type.endswith(
                        _DIAGRAM_RELTYPE_SUFFIX
                    ):
                        continue
                    if len(resolver._by_rel_id) >= _MAX_DIAGRAMS:
                        resolver.truncated = True
                        break
                    part_name = target.lstrip("/")
                    if not part_name.startswith("word/"):
                        part_name = f"word/{part_name}"
                    if part_name not in names:
                        resolver._by_rel_id[rel_id] = {
                            "part": part_name,
                            "items": [],
                            "readable": False,
                            "reason": "data part missing from package",
                        }
                        continue
                    try:
                        root = ET.fromstring(archive.read(part_name))
                    except ET.ParseError as error:
                        resolver._by_rel_id[rel_id] = {
                            "part": part_name,
                            "items": [],
                            "readable": False,
                            "reason": str(error),
                        }
                        continue
                    resolver._by_rel_id[rel_id] = {
                        "part": part_name,
                        "items": _data_part_items(root),
                        "readable": True,
                        "reason": None,
                    }
        except (BadZipFile, OSError):
            return resolver
        return resolver

    # ------------------------------------------------------------
    # paragraph lookup
    # ------------------------------------------------------------
    def paragraph_diagrams(self, paragraph) -> list[dict[str, Any]]:
        """Diagram facts for every ``dgm:relIds`` in one paragraph."""

        diagrams: list[dict[str, Any]] = []
        for rel_ids in paragraph._p.iter(_dgm("relIds")):
            rel_id = rel_ids.get(f"{{{_R_NS}}}dm")
            if not rel_id:
                continue
            fact = self._by_rel_id.get(rel_id)
            if fact is None:
                diagrams.append(
                    {
                        "rel_id": rel_id,
                        "part": None,
                        "items": [],
                        "readable": False,
                        "reason": "no diagram-data relationship for r:dm",
                    }
                )
                continue
            diagrams.append({"rel_id": rel_id, **fact})
        return diagrams

    # ------------------------------------------------------------
    # summary
    # ------------------------------------------------------------
    def summary(self, referenced_count: int) -> dict[str, Any]:
        """Summarize diagram statistics (relationship count, readable count, total entries, etc.)."""
        readable = [fact for fact in self._by_rel_id.values() if fact["readable"]]
        return {
            "diagram_relationship_count": len(self._by_rel_id),
            "referenced_diagram_count": referenced_count,
            "readable_diagram_count": len(readable),
            "item_total": sum(len(fact["items"]) for fact in readable),
            "empty_diagram_count": sum(
                1 for fact in readable if not fact["items"]
            ),
            "unreadable_diagram_count": sum(
                1 for fact in self._by_rel_id.values() if not fact["readable"]
            ),
            "truncated": self.truncated,
            "parse_errors": list(self.parse_errors),
            "note": (
                "SmartArt node labels are extracted from the diagram data "
                "parts (semantic points only) and rendered as list lines, "
                "mirroring the PPTX contract; unreadable data parts are "
                "reported as placeholders, never guessed"
            ),
        }


def diagram_block_text(items: list[str]) -> str:
    """Narrative text for one SmartArt diagram (PPTX-compatible shape)."""

    if not items:
        return "[SmartArt diagram]"
    lines = ["[SmartArt diagram]"]
    lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


__all__ = ["SmartArtResolver", "diagram_block_text"]
