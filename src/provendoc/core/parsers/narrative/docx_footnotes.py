"""DOCX footnote/endnote extraction: anchored notes from the package parts.

Word keeps footnotes and endnotes in dedicated parts
(``word/footnotes.xml`` / ``word/endnotes.xml``); the body only carries
``w:footnoteReference`` / ``w:endnoteReference`` anchors whose numbers
Word renders as superscripts.  python-docx does not model these parts at
all, so without this module the note text is silently invisible to the
narrative.

Everything is evidence: note text is joined verbatim (per-note paragraph
boundaries preserved as newlines), separator/continuation boilerplate
notes Word writes into every package are skipped by their ``w:type``
declaration, and a reference whose note part is missing (or vice versa)
is reported as an unmatched fact — never guessed, never repaired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Word-declared non-content note types present in (almost) every package.
_SKIP_NOTE_TYPES = {
    "separator",
    "continuationSeparator",
    "continuationNotice",
}

# Flood guards: a pathological package with tens of thousands of notes
# cannot blow up the narrative; the counts stay factual and the cut is
# stated in the summary.
_MAX_NOTES = 2000
_MAX_NOTE_TEXT_CHARS = 4000

_NOTE_PARTS = {
    "footnote": "word/footnotes.xml",
    "endnote": "word/endnotes.xml",
}


def _w(local: str) -> str:
    return f"{{{_W_NS}}}{local}"


def _note_text(note_element: ET.Element) -> tuple[str, int]:
    """Verbatim text of one note element, paragraph boundaries preserved."""

    lines: list[str] = []
    for paragraph in note_element.findall(_w("p")):
        text = "".join(
            node.text or "" for node in paragraph.iter(_w("t"))
        )
        if text:
            lines.append(text)
    return "\n".join(lines), len(lines)


def _parse_part(xml_bytes: bytes, kind: str) -> dict[str, dict[str, Any]]:
    """Parse one notes part into {id: note facts}."""

    root = ET.fromstring(xml_bytes)
    notes: dict[str, dict[str, Any]] = {}
    for note in root:
        local = note.tag.split("}", 1)[-1]
        if local not in ("footnote", "endnote"):
            continue
        note_id = note.get(_w("id"))
        if not note_id:
            continue
        note_type = note.get(_w("type"))
        if note_type in _SKIP_NOTE_TYPES:
            continue
        text, paragraph_count = _note_text(note)
        notes[note_id] = {
            "id": note_id,
            "kind": kind,
            "type": note_type,
            "text": text,
            "paragraph_count": paragraph_count,
        }
    return notes


class NoteModel:
    """Footnote + endnote facts loaded once per package."""

    def __init__(self) -> None:
        """Initialize an empty footnote/endnote model."""
        self.notes: dict[tuple[str, str], dict[str, Any]] = {}
        self.notes_truncated = False
        self.parse_errors: list[str] = []

    # ------------------------------------------------------------
    # construction
    # ------------------------------------------------------------
    @classmethod
    def from_package(cls, file_path: str | Path) -> "NoteModel":
        """Build the model from the package's footnote/endnote parts."""
        model = cls()
        try:
            with ZipFile(Path(file_path)) as archive:
                names = set(archive.namelist())
                for kind, part_name in _NOTE_PARTS.items():
                    if part_name not in names:
                        continue
                    try:
                        parsed = _parse_part(archive.read(part_name), kind)
                    except ET.ParseError as error:
                        model.parse_errors.append(
                            f"{part_name} unreadable: {error}"
                        )
                        continue
                    for note_id, note in parsed.items():
                        if len(model.notes) >= _MAX_NOTES:
                            model.notes_truncated = True
                            break
                        model.notes[(kind, note_id)] = note
        except (BadZipFile, OSError):
            # The package opened as a zip for python-docx already; a read
            # failure here is an honest per-document defect.
            return model
        return model

    # ------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------
    def resolve(self, kind: str, note_id: str | None) -> dict[str, Any] | None:
        """Note facts by (kind, note_id); None when the id is missing."""
        if not note_id:
            return None
        return self.notes.get((kind, str(note_id)))

    @property
    def available(self) -> bool:
        """Whether any notes were loaded."""
        return bool(self.notes)

    # ------------------------------------------------------------
    # summary
    # ------------------------------------------------------------
    def summary(
        self,
        reference_count: int,
        anchored_ids: list[tuple[str, str]],
        unmatched_references: list[dict[str, Any]],
        emitted_block_count: int,
    ) -> dict[str, Any]:
        """Summarize note statistics (anchored/unanchored/unmatched references, etc.)."""
        anchored_set = set(anchored_ids)
        unanchored_notes = [
            {"kind": kind, "id": note["id"]}
            for (kind, _note_id), note in self.notes.items()
            if (kind, note["id"]) not in anchored_set
        ]
        return {
            "footnote_count": sum(
                1 for (kind, _) in self.notes if kind == "footnote"
            ),
            "endnote_count": sum(
                1 for (kind, _) in self.notes if kind == "endnote"
            ),
            "reference_count": reference_count,
            "anchored_note_count": len(anchored_set),
            "unanchored_note_count": len(unanchored_notes),
            "unanchored_notes": unanchored_notes[:200],
            "unmatched_reference_count": len(unmatched_references),
            "unmatched_references": unmatched_references[:200],
            "emitted_block_count": emitted_block_count,
            "notes_truncated": self.notes_truncated,
            "notes": [
                {
                    "kind": note["kind"],
                    "id": note["id"],
                    "type": note["type"],
                    "paragraph_count": note["paragraph_count"],
                    "text": note["text"][:_MAX_NOTE_TEXT_CHARS],
                }
                for note in self.notes.values()
            ],
            "parse_errors": list(self.parse_errors),
            "note": (
                "footnote/endnote text is joined verbatim with paragraph "
                "boundaries preserved; separator boilerplate is skipped by "
                "its w:type declaration; unmatched anchors and notes are "
                "reported, never reconciled by guessing"
            ),
        }


def paragraph_note_refs(paragraph) -> list[dict[str, Any]]:
    """``w:footnoteReference`` / ``w:endnoteReference`` anchors in order.

    ``custom_mark`` records ``w:customMarkFollows`` (an author-supplied
    symbol instead of an automatic number) as evidence.
    """

    refs: list[dict[str, Any]] = []
    for tag_name, kind in (
        ("footnoteReference", "footnote"),
        ("endnoteReference", "endnote"),
    ):
        for node in paragraph._p.iter(_w(tag_name)):
            refs.append(
                {
                    "kind": kind,
                    "id": node.get(_w("id")),
                    "custom_mark": node.get(_w("customMarkFollows")) == "1",
                }
            )
    return refs


def truncate_note_text(text: str) -> str:
    """Honest per-note cap: the cut is stated, the count stays factual."""

    if len(text) <= _MAX_NOTE_TEXT_CHARS:
        return text
    return text[:_MAX_NOTE_TEXT_CHARS] + " …[truncated]"


__all__ = [
    "NoteModel",
    "paragraph_note_refs",
    "truncate_note_text",
]
