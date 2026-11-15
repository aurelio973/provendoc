"""Native-table overlap suppression for the narrative sequence (registry #125 L2).

Ruled-table detection (``find_tables``) renders a ``[Table]`` markdown block
whose cells already carry the region's text — but the underlying native text
blocks of the same region stay in the sequence too, so the invoice header
card lands in ``narrative_text`` twice (once as pipe rows, once as raw text
lines).  This module removes the duplicate text *fragments* under a ruled
table before visual-row banding sees them.

Discipline (frozen prereg ``specs/layout_backend_prereg_v1.md`` §1.B):

* this is a content-deletion red line, so suppression is conservative and
  evidence-shaped: a text line fragment is suppressed only when its bbox is
  geometrically covered by a ``native_table`` block (area coverage >= 0.8)
  **and** its whitespace-normalized token multiset closes against that
  table's cell token multiset (every token occurrence is accounted for by
  the table's own cells);
* a partially matching line (an address sharing a visual row with table
  headers) is never suppressed — an honest duplicate beats an invented
  deletion;
* suppressed fragments never enter visual-row banding (the caller runs this
  before ``VisualRowAssembler``), so cross-region band mixing disappears
  with the fragment;
* the evidence stays: fully suppressed blocks are removed from the sequence
  but their texts are recorded in the ledger; partially suppressed blocks
  keep their source evidence id and gain a ``table_overlap_suppression``
  raw-payload key with the suppressed line texts;
* zero-difference discipline: files without suppression produce no new
  keys anywhere.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from provendoc.core.parsers.narrative.pdf.contracts import PDFBlock

# Fragment area share that must fall inside the table bbox for the fragment
# to be a table-region duplicate (a line straddling the table edge keeps an
# outside share that the table's cells cannot account for).
_COVER_SHARE = 0.80
# Ledger sample cap — the full suppressed-text list lives on the block's
# raw_payload; the repetitive-info ledger keeps a bounded sample.
_MAX_SAMPLES = 8


def _bbox_intersection_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0.0 or height <= 0.0:
        return 0.0
    return width * height


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


class TableOverlapSuppressor:
    """Suppress text fragments duplicated by a ruled table's rendered cells."""

    def suppress(
        self, blocks: list[PDFBlock]
    ) -> tuple[list[PDFBlock], dict[str, Any]]:
        """Return (filtered blocks, ledger info).

        The ledger dict is empty when nothing was suppressed (zero-difference
        discipline — the caller adds no key in that case).
        """
        tables = [
            block
            for block in blocks
            if block.block_type == "table"
            and block.source_kind == "native_table"
        ]
        if not tables:
            return blocks, {}

        table_tokens = {
            id(block): self._cell_token_counter(block) for block in tables
        }
        # Fast pre-filter: only text blocks whose bbox touches a table can
        # hold covered fragments.
        tables_by_page: dict[int, list[PDFBlock]] = {}
        for table in tables:
            tables_by_page.setdefault(table.page_number, []).append(table)

        output: list[PDFBlock] = []
        suppressed_fragment_count = 0
        removed_block_count = 0
        involved_table_ids: set[str] = set()
        samples: list[dict[str, str]] = []

        for block in blocks:
            page_tables = tables_by_page.get(block.page_number, [])
            if block.block_type != "text" or not page_tables:
                output.append(block)
                continue
            if not any(
                _bbox_intersection_area(block.bbox, table.bbox) > 0.0
                for table in page_tables
            ):
                output.append(block)
                continue

            fragments = self._line_fragments(block)
            if not fragments:
                output.append(block)
                continue

            kept_fragments: list[tuple[str, tuple[float, float, float, float]]] = []
            suppressed_texts: list[str] = []
            suppressed_table_ids: set[str] = set()
            for text, bbox in fragments:
                covering = self._covering_table(
                    bbox, page_tables, text, table_tokens
                )
                if covering is None:
                    kept_fragments.append((text, bbox))
                    continue
                suppressed_texts.append(text)
                suppressed_table_ids.add(covering.evidence_id or "")
            if not suppressed_texts:
                output.append(block)
                continue

            suppressed_fragment_count += len(suppressed_texts)
            involved_table_ids |= suppressed_table_ids
            for text in suppressed_texts:
                if len(samples) >= _MAX_SAMPLES:
                    break
                samples.append(
                    {
                        "block_evidence_id": block.evidence_id or "",
                        "text": text[:60],
                    }
                )
            if not kept_fragments:
                removed_block_count += 1
                continue

            output.append(self._rebuild_block(block, kept_fragments, suppressed_texts))

        if not suppressed_fragment_count:
            return blocks, {}
        info: dict[str, Any] = {
            "suppressed_fragment_count": suppressed_fragment_count,
            "suppressed_block_count": removed_block_count,
            "table_count": len(involved_table_ids),
            "note": (
                "text line fragments covered by a native_table bbox whose "
                "token multiset closes against the table cells are "
                "suppressed from the narrative sequence; evidence texts "
                "remain on each block's raw_payload "
                "(table_overlap_suppression)"
            ),
        }
        if samples:
            info["samples"] = samples
        return output, info

    # ------------------------------------------------------------------
    # evidence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cell_token_counter(table: PDFBlock) -> Counter[str]:
        payload = table.raw_payload or {}
        counter: Counter[str] = Counter()
        for cell in list(payload.get("headers") or []) + [
            item for row in (payload.get("rows") or []) for item in row
        ]:
            counter.update(str(cell).split())
        return counter

    def _covering_table(
        self,
        fragment_bbox: tuple[float, float, float, float],
        tables: list[PDFBlock],
        text: str,
        table_tokens: dict[int, Counter[str]],
    ) -> PDFBlock | None:
        fragment_area = _area(fragment_bbox)
        if fragment_area <= 0.0:
            return None
        tokens = Counter(text.split())
        for table in tables:
            coverage = _bbox_intersection_area(fragment_bbox, table.bbox) / fragment_area
            if coverage < _COVER_SHARE:
                continue
            remainder = tokens - table_tokens[id(table)]
            if not remainder:
                return table
        return None

    @staticmethod
    def _line_fragments(
        block: PDFBlock,
    ) -> list[tuple[str, tuple[float, float, float, float]]]:
        """Explode a text block into (text, bbox) line fragments.

        Blocks without span evidence degrade to a single whole-block
        fragment — the fragment rule still applies, just coarser.
        """
        spans = (block.raw_payload or {}).get("spans") or []
        lines: dict[int, list[dict[str, Any]]] = {}
        for span in spans:
            lines.setdefault(int(span.get("line_index", 0) or 0), []).append(span)
        if not lines:
            text = " ".join(block.text.split())
            if not text:
                return []
            return [
                (
                    text,
                    (
                        block.bbox[0],
                        block.bbox[1],
                        block.bbox[2],
                        block.bbox[3],
                    ),
                )
            ]

        fragments: list[tuple[str, tuple[float, float, float, float]]] = []
        for line_index in sorted(lines):
            line_spans = lines[line_index]
            ordered = sorted(line_spans, key=lambda span: float(span["bbox"][0]))
            text = " ".join("".join(str(s.get("text", "")) for s in ordered).split())
            if not text:
                continue
            bbox = (
                min(float(s["bbox"][0]) for s in line_spans),
                min(float(s["bbox"][1]) for s in line_spans),
                max(float(s["bbox"][2]) for s in line_spans),
                max(float(s["bbox"][3]) for s in line_spans),
            )
            fragments.append((text, bbox))
        return fragments

    @staticmethod
    def _rebuild_block(
        block: PDFBlock,
        kept_fragments: list[tuple[str, tuple[float, float, float, float]]],
        suppressed_texts: list[str],
    ) -> PDFBlock:
        """Rebuild a partially suppressed block with full provenance."""
        text = "\n".join(fragment_text for fragment_text, _ in kept_fragments)
        x0 = min(bbox[0] for _, bbox in kept_fragments)
        y0 = min(bbox[1] for _, bbox in kept_fragments)
        x1 = max(bbox[2] for _, bbox in kept_fragments)
        y1 = max(bbox[3] for _, bbox in kept_fragments)
        raw_payload = dict(block.raw_payload or {})
        raw_payload["table_overlap_suppression"] = {
            "suppressed_line_texts": suppressed_texts,
            "kept_line_count": len(kept_fragments),
        }
        return PDFBlock(
            block_type=block.block_type,
            text=text,
            page_number=block.page_number,
            bbox=(x0, y0, x1, y1),
            source_kind=block.source_kind,
            confidence=block.confidence,
            raw_payload_ref=block.raw_payload_ref,
            raw_payload=raw_payload,
            evidence_id=block.evidence_id,
        )
