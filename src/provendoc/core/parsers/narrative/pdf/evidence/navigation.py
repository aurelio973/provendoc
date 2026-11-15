"""Immutable PDF navigation evidence: bookmarks, page labels, named destinations, links.

PyMuPDF exposes four navigation surfaces that the text-evidence extractors
deliberately ignore:

* the bookmark/outline tree (``get_toc``) — the author-declared chapter
  hierarchy with per-entry page anchors;
* page labels (``get_page_labels``) — the printed page numbering that may
  differ from physical page numbers (roman-numeral prefaces, restarted
  numbering per chapter);
* named destinations (``resolve_names``) — anchor names referenced by
  internal links;
* link annotations (``Page.get_links``) — clickable regions on the page:
  internal jumps, external URIs, launch actions, and named-destination
  references (recorded verbatim with the anchor text under the rect).

This extractor records them verbatim as evidence.  It never rewrites the
hierarchy, never guesses a missing anchor, and never maps a label to a page
unless the PDF itself declares the rule.  Interpretation (heading-level
refinement, TOC cross-checks, link resolution) lives in the reconstruction
layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

# Cap recorded link annotations so a pathological PDF (tens of thousands of
# auto-generated link rects) cannot flood the evidence payload.
_MAX_LINKS = 2000
_MAX_ANCHOR_TEXT_CHARS = 120

# Spec §15: Tagged PDF structure facts.  The tag-tree walk is bounded so a
# pathological struct tree cannot monopolize the run; the truncation is
# always explicit.
_MAX_STRUCT_ELEMENTS = 2000
_STRUCT_XREF_RE = re.compile(r"(\d+)\s+0\s+R")

# PyMuPDF link-kind constants → stable string names.
_LINK_KIND_NAMES = {
    getattr(fitz, "LINK_GOTO", 1): "internal",
    getattr(fitz, "LINK_URI", 2): "external",
    getattr(fitz, "LINK_LAUNCH", 3): "launch",
    getattr(fitz, "LINK_NAMED", 4): "named",
    getattr(fitz, "LINK_GOTOR", 5): "remote",
}


@dataclass
class BookmarkNode:
    """One bookmark (outline) entry with its page anchor preserved."""

    bookmark_id: str
    title: str
    level: int  # 1-based depth exactly as stored in the outline tree
    page_number: int  # 1-based physical page; <=0 means the anchor is broken
    dest: dict[str, Any] | None = None  # {"kind","xref","page","to","zoom"}
    children: list["BookmarkNode"] = field(default_factory=list)


@dataclass(frozen=True)
class PageLabelRange:
    """One contiguous run of pages sharing a numbering rule."""

    start_page: int  # 1-based physical, inclusive
    end_page: int  # 1-based physical, inclusive
    style: str  # 'D' decimal, 'r'/'R' roman, 'a'/'A' alpha, '' prefix-only
    prefix: str
    start_number: int


@dataclass
class NavigationEvidence:
    """All navigation surfaces extracted from one PDF, verbatim."""

    page_count: int
    bookmarks: list[BookmarkNode] = field(default_factory=list)
    page_label_ranges: list[PageLabelRange] = field(default_factory=list)
    named_destinations: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Spec §15: Tagged PDF structure facts (declared flags + bounded
    # structure-element type counts), verbatim.
    structure_tags: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Derived lookups (pure functions over the recorded evidence)
    # ------------------------------------------------------------------

    def flat_bookmarks(self) -> list[BookmarkNode]:
        """Depth-first flatten of the bookmark tree, preserving order."""

        flat: list[BookmarkNode] = []

        def walk(node: BookmarkNode) -> None:
            """Depth-first node collection."""
            flat.append(node)
            for child in node.children:
                walk(child)

        for root in self.bookmarks:
            walk(root)
        return flat

    def min_bookmark_level(self) -> int | None:
        """Minimum level in the bookmark tree (None when there are no bookmarks)."""
        levels = [node.level for node in self.flat_bookmarks()]
        return min(levels) if levels else None

    def page_label(self, page_number: int) -> str | None:
        """Printed label for a physical page, or None when undeclared.

        Only pages covered by a declared range get a label; the extractor
        never invents numbering for uncovered pages.
        """

        for label_range in self.page_label_ranges:
            if label_range.start_page <= page_number <= label_range.end_page:
                offset = page_number - label_range.start_page
                number = label_range.start_number + offset
                numeral = _format_number(number, label_range.style)
                if not numeral:
                    return label_range.prefix or None
                return f"{label_range.prefix}{numeral}"
        return None

    def label_page_map(self) -> dict[str, int]:
        """Inverse mapping (printed label → 1-based physical page).

        First declaration wins on collisions, mirroring how a reader scans
        from the front of the document.
        """

        mapping: dict[str, int] = {}
        for page_number in range(1, self.page_count + 1):
            label = self.page_label(page_number)
            if label is None or label in mapping:
                continue
            mapping[label] = page_number
        return mapping

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe serialization for parser metadata payloads."""

        flat = self.flat_bookmarks()
        return {
            "page_count": self.page_count,
            "bookmark_count": len(flat),
            "min_bookmark_level": self.min_bookmark_level(),
            "bookmarks": [_bookmark_to_dict(node) for node in self.bookmarks],
            "flat_bookmarks": [
                {
                    "bookmark_id": node.bookmark_id,
                    "title": node.title,
                    "level": node.level,
                    "page_number": node.page_number,
                    "page_label": self.page_label(node.page_number)
                    if node.page_number >= 1
                    else None,
                    "dest": node.dest,
                }
                for node in flat
            ],
            "page_label_ranges": [
                {
                    "start_page": label_range.start_page,
                    "end_page": label_range.end_page,
                    "style": label_range.style,
                    "prefix": label_range.prefix,
                    "start_number": label_range.start_number,
                }
                for label_range in self.page_label_ranges
            ],
            "page_label_map": {
                str(page): self.page_label(page)
                for page in range(1, self.page_count + 1)
                if self.page_label(page) is not None
            },
            "label_page_map": self.label_page_map(),
            "named_destinations": dict(self.named_destinations),
            "link_count": len(self.links),
            "links": list(self.links),
            "notes": list(self.notes),
            "structure_tags": dict(self.structure_tags),
        }


def _bookmark_to_dict(node: BookmarkNode) -> dict[str, Any]:
    return {
        "bookmark_id": node.bookmark_id,
        "title": node.title,
        "level": node.level,
        "page_number": node.page_number,
        "dest": node.dest,
        "children": [_bookmark_to_dict(child) for child in node.children],
    }


_ROMAN_VALUES = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _format_number(number: int, style: str) -> str:
    """Format a page number per the PDF label style, or '' for prefix-only."""

    if number < 1:
        # Styles never defined non-positive numbering; treat as unnumbered.
        return ""
    if style == "D":
        return str(number)
    if style in {"r", "R"}:
        parts: list[str] = []
        remaining = number
        for value, numeral in _ROMAN_VALUES:
            while remaining >= value:
                parts.append(numeral)
                remaining -= value
        roman = "".join(parts)
        return roman.lower() if style == "r" else roman
    if style in {"a", "A"}:
        letters: list[str] = []
        remaining = number
        while remaining > 0:
            remaining, index = divmod(remaining - 1, 26)
            letters.append(chr(ord("a") + index))
        alpha = "".join(reversed(letters))
        return alpha if style == "a" else alpha.upper()
    return ""


def _serialize_dest(dest: object) -> dict[str, Any] | None:
    """Normalize a PyMuPDF destination dict into JSON-safe primitives."""

    if not isinstance(dest, dict):
        return None
    serialized: dict[str, Any] = {}
    for key in ("kind", "xref", "page", "zoom"):
        if key in dest:
            try:
                serialized[key] = int(dest[key])
            except (TypeError, ValueError):
                serialized[key] = dest[key]
    to = dest.get("to")
    if to is not None:
        try:
            serialized["to"] = [float(to.x), float(to.y)]
        except AttributeError:
            serialized["to"] = [float(value) for value in to]
    if "name" in dest:
        serialized["name"] = str(dest["name"])
    if "nameddest" in dest:
        serialized["nameddest"] = str(dest["nameddest"])
    if "uri" in dest:
        serialized["uri"] = str(dest["uri"])
    return serialized or None


def _structure_tag_facts(document: fitz.Document) -> dict[str, Any]:
    """Extract Tagged PDF structure facts from the catalog, verbatim.

    Reads the raw xref table via ``xref_get_key`` — the only fully trustworthy
    surface, since parser-level "is tagged" claims are often wrong.  The
    structure tree (StructTreeRoot → K children) is walked breadth-first with
    a visited-set (cycle guard) and hard element cap; truncation is recorded
    explicitly.  Every absent key is reported as ``None`` rather than guessed.

    A PDF is "tagged" only when it declares *both* the MarkInfo/Marked flag
    and a StructTreeRoot; partial or absent declarations are recorded as
    False (the document does not declare itself tagged) — never guessed.
    """
    facts: dict[str, Any] = {
        "tagged_declared": False,
        "markinfo_present": None,
        "markinfo_marked": None,
        "struct_tree_root_present": None,
        "struct_elem_type_counts": {},
        "struct_elem_count": 0,
        "struct_elem_truncated": False,
        "notes": [
            "Structure tag facts are recorded verbatim from the xref table; "
            "per-block tagged reading order is NOT used (MCID resolution "
            "against content streams is out of scope for the evidence layer)."
        ],
    }

    try:
        catalog_xref = document.pdf_catalog()
    except Exception:
        # Unreadable catalog → nothing can be claimed; keep Nones.
        facts["notes"].append("pdf_catalog() failed; struct facts unavailable.")
        return facts

    def _raw(xref: int, key: str) -> tuple[str, str] | None:
        """xref_get_key with absent keys normalized to None.

        PyMuPDF returns ('null', 'null') for missing keys; treating that as
        "present" would fabricate MarkInfo flags that do not exist.
        """
        try:
            value = document.xref_get_key(xref, key)
        except Exception:
            return None
        if value is not None and value[0] == "null":
            return None
        return value

    markinfo = _raw(catalog_xref, "MarkInfo")
    if markinfo is None:
        facts["markinfo_present"] = False
    else:
        facts["markinfo_present"] = True
        marked = _raw(catalog_xref, "MarkInfo/Marked")
        if marked is not None:
            facts["markinfo_marked"] = marked[1] == "true"
        else:
            # MarkInfo dict exists but /Marked key absent inside it.
            facts["markinfo_marked"] = None

    struct_tree_root = _raw(catalog_xref, "StructTreeRoot")
    root_present = struct_tree_root is not None and struct_tree_root[0] == "xref"
    facts["struct_tree_root_present"] = bool(root_present)

    # A PDF is "tagged" only when it *declares* both the struct tree and the
    # MarkInfo/Marked flag; declaring half of it means not declared tagged.
    facts["tagged_declared"] = bool(facts["markinfo_marked"] is True and root_present)
    if not root_present:
        return facts

    # Walk the structure tree from StructTreeRoot's /K children, counting
    # /S structure types breadth-first with cycle + count guards.
    root_match = _STRUCT_XREF_RE.search(struct_tree_root[1] if struct_tree_root else "")
    if not root_match:
        facts["notes"].append("StructTreeRoot xref could not be parsed; tree not walked.")
        return facts
    root_xref = int(root_match.group(1))

    counts: dict[str, int] = {}
    total = 0
    truncated = False
    queue: list[int] = [root_xref]
    visited: set[int] = set()
    while queue:
        if total >= _MAX_STRUCT_ELEMENTS:
            truncated = True
            break
        xref = queue.pop(0)
        if xref in visited:
            continue
        visited.add(xref)
        s_value = _raw(xref, "S")
        if s_value is not None and s_value[0] == "name":
            type_name = s_value[1].lstrip("/")
            counts[type_name] = counts.get(type_name, 0) + 1
            total += 1
        k_value = _raw(xref, "K")
        if k_value is None:
            continue
        if k_value[0] == "xref":
            k_match = _STRUCT_XREF_RE.search(k_value[1])
            if k_match:
                queue.append(int(k_match.group(1)))
        elif k_value[0] == "array":
            # Array /K holds a mix of element refs and bare MCID integers;
            # only "N 0 R" refs are enqueued — integer MCIDs are markers,
            # not structure elements, and are skipped without counting.
            queue.extend(int(ref) for ref in _STRUCT_XREF_RE.findall(k_value[1]))

    facts["struct_elem_type_counts"] = counts
    facts["struct_elem_count"] = total
    facts["struct_elem_truncated"] = truncated
    return facts


class NavigationExtractor:
    """Record bookmark tree, page labels, and named destinations verbatim."""

    def extract(self, file_path: str | Path) -> NavigationEvidence:
        """Extract the bookmark tree, page labels, named destinations, and link evidence."""
        document = fitz.open(file_path)
        try:
            page_count = int(document.page_count)
            evidence = NavigationEvidence(page_count=page_count)
            self._extract_bookmarks(document, evidence)
            self._extract_page_labels(document, evidence)
            self._extract_named_destinations(document, evidence)
            self._extract_links(document, evidence)
            self._extract_structure_tags(document, evidence)
            return evidence
        finally:
            document.close()

    @staticmethod
    def _extract_structure_tags(
        document: fitz.Document, evidence: NavigationEvidence
    ) -> None:
        """Record Tagged PDF structure facts (spec §15), verbatim.

        Structure element types (/P, /H1-H6, /Table, /Figure, /L, /Artifact…)
        are counted exactly as stored.  Per-block tagged order would require
        MCID resolution against content streams; that mapping is *not*
        performed, and the note says so — the parser's selected reading order
        stays the layout-derived one, never silently replaced by tags.
        """
        evidence.structure_tags = _structure_tag_facts(document)

    def _extract_bookmarks(self, document: fitz.Document, evidence: NavigationEvidence) -> None:
        try:
            entries = document.get_toc(simple=False)
        except Exception as error:  # Broken outlines must not be fatal.
            evidence.notes.append(f"bookmark tree unreadable: {error}")
            return
        if not entries:
            return

        roots: list[BookmarkNode] = []
        stack: list[tuple[int, BookmarkNode]] = []
        sequence = 0
        for entry in entries:
            try:
                level = int(entry[0])
                title = str(entry[1] or "").strip()
                page_number = int(entry[2] or 0)
                dest = _serialize_dest(entry[3]) if len(entry) > 3 else None
            except (TypeError, ValueError, IndexError) as error:
                evidence.notes.append(f"bookmark entry #{sequence} unreadable: {error}")
                sequence += 1
                continue

            if level < 1:
                level = 1
            if stack and level > stack[-1][0] + 1:
                # Repair broken level jumps (lvl 1 → 3 with no 2 in between)
                # by clamping to parent+1; the original level is preserved in
                # the note so nothing is silently rewritten.
                evidence.notes.append(
                    f"bookmark '{title}' level {level} clamped below stack depth"
                )
                level = stack[-1][0] + 1
            if page_number < 1 and dest and isinstance(dest.get("page"), int):
                # Resolve LINK_GOTO anchors whose top-level page field failed.
                resolved = dest["page"] + 1
                if resolved >= 1:
                    page_number = resolved

            node = BookmarkNode(
                bookmark_id=f"toc:{sequence}",
                title=title,
                level=level,
                page_number=page_number,
                dest=dest,
            )
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1].children.append(node)
            else:
                roots.append(node)
            stack.append((level, node))
            sequence += 1

        evidence.bookmarks = roots

    def _extract_page_labels(self, document: fitz.Document, evidence: NavigationEvidence) -> None:
        try:
            raw_ranges = document.get_page_labels()
        except Exception as error:
            evidence.notes.append(f"page labels unreadable: {error}")
            return
        if not raw_ranges:
            return

        normalized: list[tuple[int, dict[str, Any]]] = []
        for raw in raw_ranges:
            if not isinstance(raw, dict) or "startpage" not in raw:
                continue
            try:
                start_page = int(raw["startpage"]) + 1  # 0-based → 1-based
            except (TypeError, ValueError):
                continue
            normalized.append(
                (
                    start_page,
                    {
                        "style": str(raw.get("style", "") or ""),
                        "prefix": str(raw.get("prefix", "") or ""),
                        "firstpagenum": int(raw.get("firstpagenum", 1) or 1),
                    },
                )
            )
        normalized.sort(key=lambda item: item[0])
        if not normalized:
            return

        # PyMuPDF reports only start pages; each range ends where the next
        # begins (or at the last page of the document).
        for index, (start_page, spec) in enumerate(normalized):
            end_page = (
                normalized[index + 1][0] - 1
                if index + 1 < len(normalized)
                else evidence.page_count
            )
            if end_page < start_page:
                continue
            evidence.page_label_ranges.append(
                PageLabelRange(
                    start_page=start_page,
                    end_page=min(end_page, evidence.page_count),
                    style=spec["style"],
                    prefix=spec["prefix"],
                    start_number=spec["firstpagenum"],
                )
            )

    def _extract_named_destinations(self, document: fitz.Document, evidence: NavigationEvidence) -> None:
        try:
            names = document.resolve_names()
        except Exception as error:
            evidence.notes.append(f"named destinations unreadable: {error}")
            return
        if not isinstance(names, dict):
            return
        for name, spec in names.items():
            if not isinstance(spec, dict):
                continue
            entry: dict[str, Any] = {}
            page = spec.get("page")
            if isinstance(page, int):
                entry["page"] = page + 1  # 0-based → 1-based
            to = spec.get("to")
            if to is not None:
                try:
                    entry["to"] = [float(to[0]), float(to[1])]
                except (TypeError, ValueError, IndexError):
                    pass
            if entry:
                evidence.named_destinations[str(name)] = entry

    def _extract_links(self, document: fitz.Document, evidence: NavigationEvidence) -> None:
        """Record every link annotation verbatim, capped at _MAX_LINKS.

        Internal GOTO links whose target page failed to resolve (``page`` is
        -1) but that carry a named destination are kept as ``named`` links so
        the reconstruction layer can resolve them against the recorded
        named-destination table instead of silently dropping the jump.
        """
        recorded = 0
        truncated = False
        # Iterate by index: pymupdf's Document supports runtime iteration,
        # but the type stubs do not declare __iter__; range+__getitem__ has
        # identical semantics and is statically checkable.
        for page_index in range(int(document.page_count)):
            page = document[page_index]
            try:
                raw_links = page.get_links()
            except Exception as error:  # One broken page must not kill the rest.
                evidence.notes.append(
                    f"links unreadable on page {page.number + 1}: {error}"
                )
                continue
            # Per-page word cache: extracting anchor text via
            # page.get_textbox() costs one full-page character scan per
            # link (O(links × page chars) — 74% of parse time on
            # link-dense papers). One get_text("words") pass per page
            # plus rect intersection is orders of magnitude cheaper.
            words: list | None = None
            for raw in raw_links:
                if recorded >= _MAX_LINKS:
                    truncated = True
                    break
                try:
                    if words is None:
                        words = page.get_text("words")
                    fact = self._serialize_link(page, raw, words=words)
                except Exception as error:
                    evidence.notes.append(
                        f"link unreadable on page {page.number + 1}: {error}"
                    )
                    continue
                if fact is not None:
                    evidence.links.append(fact)
                    recorded += 1
            if truncated:
                break
        if truncated:
            evidence.notes.append(
                f"link annotations truncated at {_MAX_LINKS}"
            )

    @staticmethod
    def _anchor_text_from_words(
        words: list, rect: list[float],
    ) -> str:
        """Reconstruct anchor text from the page's word list.

        Words are ``get_text("words")`` tuples ``(x0, y0, x1, y1, word,
        block_no, line_no, word_no)``; those intersecting the link rect
        are grouped back into lines (newline-joined, space-joined within
        a line) to mirror ``get_textbox`` output.
        """
        x0, y0, x1, y1 = rect
        hits = [
            w for w in words
            if w[0] < x1 and w[2] > x0 and w[1] < y1 and w[3] > y0
        ]
        if not hits:
            return ""
        lines: dict[tuple[int, int], list] = {}
        for w in hits:
            lines.setdefault((w[5], w[6]), []).append(w)
        parts = []
        for key in sorted(lines):
            line_words = sorted(
                lines[key],
                key=lambda w: (w[7] if len(w) > 7 else 0, w[0]),
            )
            parts.append(" ".join(w[4] for w in line_words))
        return "\n".join(parts).strip()

    @staticmethod
    def _serialize_link(
        page: "fitz.Page",
        raw: dict[str, Any],
        words: list | None = None,
    ) -> dict[str, Any] | None:
        kind_code = raw.get("kind")
        kind = _LINK_KIND_NAMES.get(kind_code, f"unknown_{kind_code}")
        fact: dict[str, Any] = {"page": page.number + 1, "kind": kind}

        rect = raw.get("from")
        if rect is not None:
            try:
                fact["rect"] = [
                    round(float(rect.x0), 2),
                    round(float(rect.y0), 2),
                    round(float(rect.x1), 2),
                    round(float(rect.y1), 2),
                ]
            except (AttributeError, TypeError, ValueError):
                pass

        nameddest = raw.get("nameddest") or raw.get("name")
        target_page = raw.get("page")
        has_valid_target = isinstance(target_page, int) and target_page >= 0

        if kind == "internal" and not has_valid_target:
            # GOTO with an unresolved page but a destination name: classify
            # as named so it can be resolved against named_destinations.
            if nameddest:
                kind = "named"
                fact["kind"] = kind
            else:
                fact["broken"] = True
        if kind == "internal" and isinstance(target_page, int) and target_page >= 0:
            fact["target_page"] = target_page + 1  # 0-based → 1-based
        if nameddest:
            fact["nameddest"] = str(nameddest)
        if raw.get("uri"):
            fact["uri"] = str(raw["uri"])
        if raw.get("file"):
            fact["file"] = str(raw["file"])
        to = raw.get("to")
        if to is not None:
            try:
                fact["to"] = [round(float(to.x), 2), round(float(to.y), 2)]
            except AttributeError:
                try:
                    fact["to"] = [round(float(v), 2) for v in to]
                except (TypeError, ValueError):
                    pass

        # Anchor text under the clickable rect: read-only, never used to
        # rewrite blocks — it makes the link auditable against the narrative.
        if "rect" in fact:
            try:
                if words is None:
                    words = page.get_text("words")
                anchor_text = NavigationExtractor._anchor_text_from_words(
                    words, fact["rect"]
                )
                if anchor_text:
                    fact["anchor_text"] = anchor_text[:_MAX_ANCHOR_TEXT_CHARS]
            # anchor_text is omitted when anchor-word lookup fails (an
            # enhancement field, not core evidence).
            except Exception:
                pass
        return fact
