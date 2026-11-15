"""Visual-row assembly for native text evidence (registry #125 fix).

PDF native extraction emits each drawn text run as its own block/line in
*draw order*, so a label/value form row rendered as two fragments
(``Salesperson:`` at x=354, ``Mesa County Republicans`` at x=414, same
baseline) enters the narrative as two stacked lines — sometimes with the
value before the label (draw order is not visual order).  Every
line-start consumer downstream (KIE anchors, scenario anchors, domain
signals, chunking) then misses the pair.

This module assembles *visual rows*: fragments whose baselines coincide
(one y-band) and that come from **different source blocks** are one
visual line, read left-to-right and joined with a single space — exactly
what a human sees as one line.

The "different source blocks" rule is what keeps prose safe: a normal
paragraph block owns all of its own lines, no band of its lines ever
mixes with another block, so the block is emitted intact with its
original multi-line text, evidence id, and payload — byte-identical to
today's output.  Only blocks that genuinely share a visual line with
another block (label/value grids, tab-separated letterhead fields) are
rewritten into row blocks.

Strategy is decided per page from probe evidence only:

* single-column pages → rows are assembled (a same-baseline fragment
  set on a single-column page is one visual line);
* multicolumn pages whose lines mostly fill their columns (prose) →
  NO assembly, the existing column-first ordering is correct and
  cross-column joining would interleave two prose columns;
* multicolumn pages dominated by short fragments (label/value grids,
  line-item sheets) → rows are assembled across the full page width so
  each grid row pairs its fragments;
* rotated pages / vertical writing → no assembly (honest fallback);
* layout-overlay (MinerU) pages → the caller skips assembly entirely
  because the semantic region order already supersedes geometry.

Registry #125 L1 (intra-block reassembly): OCR evidence often arrives as a
single monolith block holding an entire label/value form or line-item
table — every cell its own line in draw order.  The "different source
blocks" safety rule cannot fire inside one block, so an OCR block with a
band of >= 2 horizontally separated own fragments (the grid signature)
joins the reassembly too.  D-A (frozen): only ``source_kind == "ocr"``
blocks are eligible — native monoliths keep the historical rule because
no native evidence exists and reordering risk is unquantified.  Pure
prose blocks (every band holds a single own line) never explode.

Every assembled row keeps full provenance: all member spans in
``raw_payload`` plus the member evidence ids, so audits can trace each
row character to its native source block.  Row text is the member
fragment texts joined by one space — nothing more is invented.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from provendoc.core.parsers.narrative.pdf.contracts import PDFBlock

# --- thresholds (evidence-shape calibrated, documented in-place) -----------
# A line "fills" its column at >= this width share — the prose signature.
_FILL_SHARE = 0.60
# A multicolumn page is prose when >= this share of its lines fill their
# column; below it, short fragments dominate and the page is a grid.
_PROSE_SHARE = 0.50
# Band tolerance: baselines within this distance of the band anchor join
# the band.  0.30 x median line height keeps different-size fonts on the
# same baseline together while separating tight table rows (pitch ~1.5x
# line height) — see registry #125 evidence (business_records 00136a2 spot grid).
_BAND_TOLERANCE_FACTOR = 0.30
_BAND_TOLERANCE_FLOOR = 1.5
# Vertical-writing share above which the page opts out entirely.
_VERTICAL_SHARE_LIMIT = 0.10


def _norm(text: str) -> str:
    return " ".join(text.split())


class _Fragment:
    """One native text line participating in row assembly."""

    __slots__ = ("text", "x0", "y0", "x1", "y1", "source_block", "vertical")

    def __init__(
        self,
        text: str,
        bbox: tuple[float, float, float, float],
        source_block: PDFBlock,
        *,
        vertical: bool = False,
    ):
        self.text = text
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.source_block = source_block
        self.vertical = vertical

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)


class VisualRowAssembler:
    """Assemble same-baseline native text fragments into visual rows."""

    def assemble(
        self,
        blocks: list[PDFBlock],
        *,
        layout_signals: dict[str, Any],
    ) -> tuple[list[PDFBlock], dict[str, Any]]:
        """Return the block sequence with visual rows assembled per page.

        Non-text blocks pass through.  Pages that opt out keep their
        original sequence (minus nothing).  The input is the post-filter
        content sequence, so suppressed evidence cannot be resurrected.
        """
        page_signal = {
            signal["page_number"]: signal
            for signal in layout_signals.get("pages", [])
            if isinstance(signal, dict) and "page_number" in signal
        }
        text_by_page: dict[int, list[PDFBlock]] = {}
        other: list[PDFBlock] = []
        for block in blocks:
            if block.block_type == "text":
                text_by_page.setdefault(block.page_number, []).append(block)
            else:
                other.append(block)

        page_plans: dict[int, dict[str, Any]] = {}
        assembled_by_page: dict[int, list[PDFBlock]] = {}
        for page_number, text_blocks in text_by_page.items():
            plan = self._plan_page(
                text_blocks, signal=page_signal.get(page_number, {})
            )
            page_plans[page_number] = plan
            if plan["assemble"]:
                assembled_by_page[page_number], intra_count = self._assemble_rows(
                    text_blocks, page_number
                )
                plan["intra_grid_block_count"] = intra_count

        output = self._resequence(blocks, assembled_by_page)
        info = {
            "pages": {
                str(page_number): {
                    "strategy": plan["strategy"],
                    "fragment_count": plan["fragment_count"],
                    "row_count": len(assembled_by_page.get(page_number, [])),
                    **(
                        {"intra_grid_block_count": plan["intra_grid_block_count"]}
                        if "intra_grid_block_count" in plan
                        else {}
                    ),
                }
                for page_number, plan in page_plans.items()
            },
            "assembled_page_count": len(assembled_by_page),
            "note": (
                "same-baseline fragments from different blocks joined into "
                "visual rows (label/value grids, draw-order inversions); "
                "OCR monolith blocks with an own-fragment grid band join the "
                "reassembly (#125 L1, ocr source only); single-source blocks "
                "keep their original multi-line form; prose multicolumn pages "
                "keep column-first ordering; member spans and evidence ids "
                "preserved per row"
            ),
        }
        return output, info

    # ------------------------------------------------------------------
    # per-page strategy
    # ------------------------------------------------------------------

    def _plan_page(
        self, text_blocks: list[PDFBlock], *, signal: dict[str, Any]
    ) -> dict[str, Any]:
        fragments, skipped = self._fragments(text_blocks)
        plan = {
            "assemble": False,
            "strategy": "skipped_no_evidence",
            "fragment_count": len(fragments),
        }
        if not fragments:
            return plan
        if int(signal.get("rotation", 0) or 0):
            plan["strategy"] = "skipped_rotation"
            return plan
        total_lines = len(fragments) + len(skipped)
        vertical = sum(1 for fragment in skipped if fragment.vertical)
        if total_lines and vertical / total_lines > _VERTICAL_SHARE_LIMIT:
            plan["strategy"] = "skipped_vertical_writing"
            return plan

        if not signal.get("likely_multicolumn"):
            plan.update(assemble=True, strategy="rowwise_single_column")
            return plan

        page_width = float(signal.get("width") or 0.0)
        boundaries = self._boundaries(signal, page_width)
        if self._is_prose_columns(fragments, boundaries, page_width):
            plan["strategy"] = "column_first_prose"
            return plan
        plan.update(assemble=True, strategy="rowwise_grid")
        return plan

    @staticmethod
    def _boundaries(signal: dict[str, Any], page_width: float) -> list[float]:
        raw = signal.get("column_boundaries")
        if not isinstance(raw, list) or page_width <= 0:
            return []
        try:
            return sorted(
                float(value)
                for value in raw
                if isinstance(value, (int, float)) and 0.0 < float(value) < page_width
            )
        except (TypeError, ValueError):
            return []

    @staticmethod
    def _column_of(x_center: float, boundaries: list[float]) -> int:
        for index, split in enumerate(boundaries):
            if x_center < split:
                return index
        return len(boundaries)

    def _is_prose_columns(
        self,
        fragments: list[_Fragment],
        boundaries: list[float],
        page_width: float,
    ) -> bool:
        """Prose pages fill their columns; grids carry short fragments."""
        if not boundaries or page_width <= 0:
            # Multicolumn verdict without usable boundary evidence: join
            # nothing, keep the column-first fallback honest.
            return True
        edges = [0.0, *boundaries, page_width]
        widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
        filled = 0
        for fragment in fragments:
            column = self._column_of((fragment.x0 + fragment.x1) / 2.0, boundaries)
            column_width = widths[column] if column < len(widths) else page_width
            if column_width > 0 and fragment.width / column_width >= _FILL_SHARE:
                filled += 1
        return filled / len(fragments) >= _PROSE_SHARE

    # ------------------------------------------------------------------
    # fragment extraction, banding, and row assembly
    # ------------------------------------------------------------------

    def _fragments(
        self, text_blocks: list[PDFBlock]
    ) -> tuple[list[_Fragment], list[_Fragment]]:
        """Explode blocks into horizontal LTR line fragments.

        Rotated/vertical lines are returned in ``skipped`` (they keep
        their own rows; y-banding is meaningless for them).  Blocks
        without span evidence contribute no fragments and are therefore
        never exploded.
        """
        fragments: list[_Fragment] = []
        skipped: list[_Fragment] = []
        for block in text_blocks:
            spans = (block.raw_payload or {}).get("spans") or []
            lines: dict[int, list[dict[str, Any]]] = {}
            for span in spans:
                lines.setdefault(int(span.get("line_index", 0) or 0), []).append(span)
            if not lines:
                continue
            for line_index in sorted(lines):
                line_spans = lines[line_index]
                wmode = any(int(s.get("line_wmode", 0) or 0) for s in line_spans)
                direction = line_spans[0].get("line_dir", [1.0, 0.0])
                rotated = not (
                    isinstance(direction, (list, tuple))
                    and len(direction) >= 2
                    and float(direction[0]) > 0
                    and abs(float(direction[1])) < 1e-6
                )
                ordered = sorted(line_spans, key=lambda s: float(s["bbox"][0]))
                text = _norm("".join(str(s.get("text", "")) for s in ordered))
                if not text:
                    continue
                x0 = min(float(s["bbox"][0]) for s in line_spans)
                y0 = min(float(s["bbox"][1]) for s in line_spans)
                x1 = max(float(s["bbox"][2]) for s in line_spans)
                y1 = max(float(s["bbox"][3]) for s in line_spans)
                fragment = _Fragment(
                    text, (x0, y0, x1, y1), block, vertical=bool(wmode)
                )
                if wmode or rotated:
                    skipped.append(fragment)
                else:
                    fragments.append(fragment)
        return fragments, skipped

    def _assemble_rows(
        self, text_blocks: list[PDFBlock], page_number: int
    ) -> tuple[list[PDFBlock], int]:
        """Assemble rows; return (row blocks, intra-grid block count).

        Registry #125 L1: besides cross-block bands, an OCR block whose own
        fragments form a multi-fragment horizontally separated band (the
        grid signature of an OCR monolith) is exploded into row blocks too.
        """
        fragments, skipped = self._fragments(text_blocks)
        bands = self._bands(fragments)
        # Only blocks that share at least one band with another block are
        # rewritten; everything else keeps its original block form.
        mixed_blocks: set[int] = set()
        for band in bands:
            members = {id(fragment.source_block) for fragment in band}
            if len(members) > 1:
                mixed_blocks.update(members)

        # Registry #125 L1 (D-A frozen): OCR blocks only — a band holding
        # >= 2 horizontally separated fragments of the SAME block is the
        # grid signature; the block joins the reassembly.  Native blocks
        # keep the historical cross-block rule (no evidence, unquantified
        # reordering risk).
        intra_grid_blocks: set[int] = set()
        for band in bands:
            by_block: dict[int, list[_Fragment]] = {}
            for fragment in band:
                by_block.setdefault(id(fragment.source_block), []).append(fragment)
            for own_fragments in by_block.values():
                if (
                    len(own_fragments) >= 2
                    and own_fragments[0].source_block.source_kind == "ocr"
                    and self._horizontally_separated(own_fragments)
                ):
                    intra_grid_blocks.add(id(own_fragments[0].source_block))
        mixed_blocks |= intra_grid_blocks

        output: list[PDFBlock] = []
        emitted_intact: set[int] = set()
        band_counter = 0
        for band in bands:
            if any(id(fragment.source_block) in mixed_blocks for fragment in band):
                output.append(
                    self._row_block(band, page_number, band_counter)
                )
                band_counter += 1
                continue
            for fragment in band:
                block = fragment.source_block
                if id(block) not in emitted_intact:
                    output.append(block)
                    emitted_intact.add(id(block))
        for fragment in skipped:
            block = fragment.source_block
            if id(block) in mixed_blocks:
                # The block is exploded; its rotated/vertical lines stay
                # as standalone rows rather than vanishing.
                output.append(
                    self._row_block([fragment], page_number, band_counter)
                )
                band_counter += 1
            elif id(block) not in emitted_intact:
                output.append(block)
                emitted_intact.add(id(block))
        for block in text_blocks:
            # Blocks without span evidence never explode.
            if (
                not (block.raw_payload or {}).get("spans")
                and id(block) not in emitted_intact
            ):
                output.append(block)
                emitted_intact.add(id(block))
        output.sort(
            key=lambda block: (block.bbox[1], block.bbox[0], block.evidence_id or "")
        )
        return output, len(intra_grid_blocks)

    @staticmethod
    def _horizontally_separated(fragments: list[_Fragment]) -> bool:
        """True when the fragments' x-ranges are pairwise disjoint.

        Same-baseline same-block fragments that overlap horizontally are
        not side-by-side cells — conservatively not a grid signature.
        """
        ordered = sorted(fragments, key=lambda fragment: fragment.x0)
        for previous, following in zip(ordered, ordered[1:]):
            if following.x0 < previous.x1 - 1.0:
                return False
        return True

    def _bands(self, fragments: list[_Fragment]) -> list[list[_Fragment]]:
        """Group fragments into baseline bands (visual rows)."""
        if not fragments:
            return []
        heights = [fragment.height for fragment in fragments if fragment.height > 0]
        median_height = median(heights) if heights else 0.0
        tolerance = max(_BAND_TOLERANCE_FLOOR, _BAND_TOLERANCE_FACTOR * median_height)
        ordered = sorted(
            fragments, key=lambda fragment: (fragment.center_y, fragment.x0)
        )
        bands: list[list[_Fragment]] = []
        for fragment in ordered:
            if bands:
                anchor = sum(item.center_y for item in bands[-1]) / len(bands[-1])
                if abs(fragment.center_y - anchor) <= tolerance:
                    bands[-1].append(fragment)
                    continue
            bands.append([fragment])
        return bands

    @staticmethod
    def _row_block(
        band: list[_Fragment], page_number: int, band_index: int
    ) -> PDFBlock:
        ordered = sorted(band, key=lambda fragment: fragment.x0)
        text = " ".join(fragment.text for fragment in ordered)
        x0 = min(fragment.x0 for fragment in ordered)
        y0 = min(fragment.y0 for fragment in ordered)
        x1 = max(fragment.x1 for fragment in ordered)
        y1 = max(fragment.y1 for fragment in ordered)
        member_ids: list[str] = []
        member_refs: list[str] = []
        spans: list[dict[str, Any]] = []
        source_kind = ordered[0].source_block.source_kind
        confidence = min(
            (fragment.source_block.confidence for fragment in ordered), default=1.0
        )
        for fragment in ordered:
            if fragment.source_block.evidence_id:
                member_ids.append(fragment.source_block.evidence_id)
            if fragment.source_block.raw_payload_ref:
                member_refs.append(fragment.source_block.raw_payload_ref)
            spans.extend(
                (fragment.source_block.raw_payload or {}).get("spans") or []
            )
        return PDFBlock(
            block_type="text",
            text=text,
            page_number=page_number,
            bbox=(x0, y0, x1, y1),
            source_kind=source_kind,
            confidence=confidence,
            raw_payload_ref=f"page:{page_number}:row:{band_index}",
            raw_payload={
                "row_assembly": True,
                "member_evidence_ids": list(dict.fromkeys(member_ids)),
                "member_payload_refs": list(dict.fromkeys(member_refs)),
                "spans": spans,
            },
            evidence_id=f"row:p{page_number}:r{band_index}",
        )

    # ------------------------------------------------------------------
    # resequencing
    # ------------------------------------------------------------------

    @staticmethod
    def _resequence(
        blocks: list[PDFBlock],
        assembled_by_page: dict[int, list[PDFBlock]],
    ) -> list[PDFBlock]:
        """Rebuild the block sequence with assembled pages replaced.

        Assembled pages: row output + non-text blocks merged by (y, x);
        text fragments of exploded blocks are fully replaced by their
        rows.  Other pages keep their original sequence.  Output page
        order follows each page's first appearance in the input.
        """
        if not assembled_by_page:
            return list(blocks)
        page_order: list[int] = []
        seen_pages: set[int] = set()
        plain_by_page: dict[int, list[PDFBlock]] = {}
        nontext_by_page: dict[int, list[PDFBlock]] = {}
        for block in blocks:
            page = block.page_number
            if page not in seen_pages:
                seen_pages.add(page)
                page_order.append(page)
            if page in assembled_by_page:
                if block.block_type != "text":
                    nontext_by_page.setdefault(page, []).append(block)
                continue
            plain_by_page.setdefault(page, []).append(block)
        output: list[PDFBlock] = []
        for page in page_order:
            if page in assembled_by_page:
                output.extend(
                    sorted(
                        assembled_by_page[page] + nontext_by_page.get(page, []),
                        key=lambda block: (
                            block.bbox[1],
                            block.bbox[0],
                            block.evidence_id or "",
                        ),
                    )
                )
            else:
                output.extend(plain_by_page.get(page, []))
        return output
