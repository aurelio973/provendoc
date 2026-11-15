"""Reading-order reconstruction for native PDF evidence blocks."""

from __future__ import annotations

from provendoc.core.parsers.narrative.pdf.contracts import PDFBlock, PDFPage


class ReadingOrderReconstructor:
    """Order blocks page-by-page while retaining explicit layout warnings.

    Native PDF block order is not a trustworthy reading order.  For ordinary
    pages we use top-to-bottom, left-to-right ordering.  If probe evidence
    suggests multiple columns, Phase 1 takes a conservative column-first pass
    and records the condition rather than pretending it is semantically solved.

    B5 generalizes the ordering from a hardcoded left/right split to N
    columns: the probe's ``column_boundaries`` (x split points between
    adjacent durable anchor clusters) drive the bucketing, so three-column
    and irregular layouts order column-by-column too.  Pages without
    boundary evidence keep the historical two-bucket midpoint fallback.

    Registry #175 hole 3: block-level column sorting cannot rescue row-level
    interleaving — a single block may mix rows (even spans within one row)
    from both columns. For multicolumn pages, cross-column mixed blocks are
    first split per-column from raw_payload span geometry, then sorted by
    column; the split is conservative (a block is kept whole if any span
    crosses a column boundary) and the facts are registered in
    column_line_split_pages.
    """

    def reconstruct(self, pages: list[PDFPage], layout_signals: dict) -> tuple[list[PDFBlock], dict]:
        """Order blocks page-by-page and record multicolumn warnings."""
        page_signal_by_number = {
            signal["page_number"]: signal for signal in layout_signals.get("pages", [])
        }
        ordered: list[PDFBlock] = []
        multi_column_pages: list[int] = []
        multi_column_details: dict[int, dict] = {}
        # Registry #175 hole 3: pages where line/span-level splitting occurred
        # and the extra sub-blocks produced — registered facts only, not a
        # semantic-repair claim.
        line_split_pages: dict[int, int] = {}

        for page in pages:
            signal = page_signal_by_number.get(page.page_number, {})
            is_multicolumn = bool(signal.get("likely_multicolumn"))
            if is_multicolumn:
                multi_column_pages.append(page.page_number)
                boundaries = signal.get("column_boundaries") or []
                if not isinstance(boundaries, list):
                    boundaries = []
                try:
                    boundaries = sorted(
                        float(value)
                        for value in boundaries
                        if 0.0 < float(value) < page.width
                    )
                except (TypeError, ValueError):
                    boundaries = []
                page_blocks = page.blocks
                # Hole 3: split only with real boundary evidence (midpoint
                # guessing risks more than it gains); only blocks whose spans
                # fall cleanly into single-column intervals are split.
                if boundaries:
                    split_blocks = self._split_column_mixed_blocks(
                        page.blocks, page.width, boundaries
                    )
                    if len(split_blocks) != len(page.blocks):
                        line_split_pages[page.page_number] = (
                            len(split_blocks) - len(page.blocks)
                        )
                    page_blocks = split_blocks
                ordered.extend(
                    self._column_first_order(page_blocks, page.width, boundaries)
                )
                if boundaries:
                    multi_column_details[page.page_number] = {
                        "column_count": len(boundaries) + 1,
                        "column_boundaries": [round(value, 2) for value in boundaries],
                    }
            else:
                ordered.extend(self._top_to_bottom_order(page.blocks))

        return ordered, {
            "reading_order": "column_first" if multi_column_pages else "top_to_bottom_left_to_right",
            "multicolumn_suspected_pages": multi_column_pages,
            "multicolumn_suspected": bool(multi_column_pages),
            # B5: the actual column structure used per page when the probe
            # provided boundaries (pages with only the legacy midpoint
            # fallback stay out of this detail map).
            "multicolumn_structure": multi_column_details,
            # Registry #175 hole 3: line/span-level column split registered as
            # {page: extra sub-blocks produced}.
            "column_line_split_pages": line_split_pages,
        }

    @staticmethod
    def _top_to_bottom_order(blocks: list[PDFBlock]) -> list[PDFBlock]:
        return sorted(blocks, key=lambda block: (block.bbox[1], block.bbox[0], block.evidence_id or ""))

    def _column_first_order(
        self,
        blocks: list[PDFBlock],
        page_width: float,
        boundaries: list[float] | None = None,
    ) -> list[PDFBlock]:
        """Order column-by-column using probe boundaries or a midpoint fallback.

        ``boundaries`` are the x split points between adjacent columns
        (N-1 values for N columns).  Without them, the historical two-bucket
        midpoint split is used, which is the honest legacy behaviour for
        signals that predate boundary evidence.
        """
        if len(blocks) < 2 or page_width <= 0:
            return self._top_to_bottom_order(blocks)

        split_points = sorted(boundaries) if boundaries else [page_width / 2]

        def column_index(block: PDFBlock) -> int:
            """Locate a block's column by its x center."""
            center = self._center_x(block)
            for index, split in enumerate(split_points):
                if center < split:
                    return index
            return len(split_points)

        # Full-width blocks are normally section headings. Keep one above the
        # columns when its top edge precedes both column groups; otherwise rely
        # on its y-coordinate and retain a deterministic, conservative order.
        full_width = [
            block for block in blocks if (block.bbox[2] - block.bbox[0]) >= page_width * 0.72
        ]
        column_blocks = [block for block in blocks if block not in full_width]
        columns: list[list[PDFBlock]] = [[] for _ in range(len(split_points) + 1)]
        for block in column_blocks:
            columns[column_index(block)].append(block)

        before = [
            block for block in full_width
            if not column_blocks or block.bbox[1] <= min(item.bbox[1] for item in column_blocks)
        ]
        after = [block for block in full_width if block not in before]
        ordered = self._top_to_bottom_order(before)
        for column in columns:
            ordered.extend(self._top_to_bottom_order(column))
        ordered.extend(self._top_to_bottom_order(after))
        return ordered

    @staticmethod
    def _center_x(block: PDFBlock) -> float:
        return (block.bbox[0] + block.bbox[2]) / 2

    def _split_column_mixed_blocks(
        self,
        blocks: list[PDFBlock],
        page_width: float,
        boundaries: list[float],
    ) -> list[PDFBlock]:
        """Registry #175 hole 3: split cross-column mixed text blocks per column.

        The split is conservative: only when a block's spans fall cleanly into
        >=2 column intervals (any span crossing a column boundary keeps the
        block whole — such a span is most likely a full-width heading or a
        table row, and splitting would tear it apart) and raw_payload carries
        span geometry. Blocks without span evidence stay whole (honest
        degradation, no guessed geometry). Sub-block lineage is preserved via
        raw_payload.split_from_evidence_id and a ":col{n}" suffix id (same
        precedent as borderless_tables.py's ":rotated").
        """
        result: list[PDFBlock] = []
        for block in blocks:
            result.extend(self._split_one_block_by_columns(block, boundaries))
        return result

    def _split_one_block_by_columns(
        self,
        block: PDFBlock,
        boundaries: list[float],
    ) -> list[PDFBlock]:
        if block.block_type != "text" or not block.text.strip():
            return [block]
        spans = (block.raw_payload or {}).get("spans") or []
        if len(spans) < 2:
            return [block]

        # Column intervals: (-inf, b1), (b1, b2), ..., (bn, +inf).
        intervals: list[tuple[float, float]] = []
        previous = float("-inf")
        for split in sorted(boundaries):
            intervals.append((previous, split))
            previous = split
        intervals.append((previous, float("inf")))

        buckets: list[list[dict]] = [[] for _ in intervals]
        for span in spans:
            if not isinstance(span, dict) or "bbox" not in span:
                return [block]
            x0, _y0, x1, _y1 = span["bbox"]
            placed = False
            for index, (low, high) in enumerate(intervals):
                if low <= x0 and x1 <= high:
                    buckets[index].append(span)
                    placed = True
                    break
            if not placed:
                # Span crosses a column boundary: keep whole, conservatively.
                return [block]

        occupied = [index for index, bucket in enumerate(buckets) if bucket]
        if len(occupied) < 2:
            return [block]

        parts: list[PDFBlock] = []
        for order, column in enumerate(occupied):
            column_spans = buckets[column]
            lines_by_index: dict[int, list[dict]] = {}
            for span in column_spans:
                lines_by_index.setdefault(int(span.get("line_index", 0)), []).append(span)
            line_texts: list[str] = []
            xs0: list[float] = []
            ys0: list[float] = []
            xs1: list[float] = []
            ys1: list[float] = []
            for line_index in sorted(lines_by_index):
                line_spans = sorted(
                    lines_by_index[line_index], key=lambda item: item.get("span_index", 0)
                )
                line_text = "".join(str(span.get("text", "")) for span in line_spans).strip()
                if line_text:
                    line_texts.append(line_text)
                for span in line_spans:
                    x0, y0, x1, y1 = span["bbox"]
                    xs0.append(float(x0))
                    ys0.append(float(y0))
                    xs1.append(float(x1))
                    ys1.append(float(y1))
            if not line_texts:
                continue
            parts.append(
                PDFBlock(
                    block_type="text",
                    text="\n".join(line_texts),
                    page_number=block.page_number,
                    bbox=(
                        min(xs0),
                        min(ys0),
                        max(xs1),
                        max(ys1),
                    ),
                    source_kind=block.source_kind,
                    confidence=block.confidence,
                    raw_payload_ref=(
                        f"{block.raw_payload_ref}:col{order}"
                        if block.raw_payload_ref
                        else None
                    ),
                    raw_payload={
                        "split_from_evidence_id": block.evidence_id,
                        "column_index": column,
                        "spans": column_spans,
                    },
                    evidence_id=(
                        f"{block.evidence_id}:col{order}" if block.evidence_id else None
                    ),
                )
            )
        return parts or [block]
