"""Table reconstruction with auditable multi-signal cross-page continuation joins."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

_CONTINUATION_RE = re.compile(r"(?:\bcontinued\b|\bcontinuation\b|续表|续页|接上页)", re.IGNORECASE)
_TABLE_CAPTION_RE = re.compile(r"^(?:table|表)\s*\d*[:.：\-]", re.IGNORECASE)

# Spec §8: the representation carries table_status / columns_confidence.
# The values are *derived from recorded facts only* (detection source, row
# grid consistency, continuation joins) — never an invented score.
_DETECTION_METHOD_BY_SOURCE_KIND = {
    "native_table": "geometry",
    "borderless_table": "alignment",
    "borderless_table_ocr": "alignment_ocr",
}


class TableReconstructor:
    """Keep tables atomic and join only explainable, high-confidence continuations.

    A continuation must always connect adjacent pages and retain sufficiently
    similar headers. Explicit continuation markers allow a lower score because
    they are direct source evidence. In their absence, matching columns plus a
    page-boundary transition (previous table near the page bottom and current
    table near the page top) are required to avoid joining unrelated tables that
    happen to reuse the same generic headers.
    """

    _MIN_HEADER_SIMILARITY = 0.70
    _EXPLICIT_THRESHOLD = 0.70
    _INFERRED_THRESHOLD = 0.75

    def reconstruct(self, blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Stitch high-confidence cross-page table continuations."""
        result: list[dict[str, Any]] = []
        continuation_decisions: list[dict[str, Any]] = []
        continued_table_count = 0
        inferred_continued_table_count = 0

        captions = self._captions_by_evidence_id(blocks)
        for index, block in enumerate(blocks):
            if block.get("type") != "table":
                result.append(block)
                continue

            previous_index = self._find_previous_table(result)
            if previous_index is None:
                result.append(block)
                continue

            assessment = self._assess_continuation(
                previous=result[previous_index],
                current=block,
                source_blocks=blocks,
                current_index=index,
                captions=captions,
            )
            continuation_decisions.append(assessment)
            if not assessment["merged"]:
                result.append(block)
                continue

            result[previous_index] = self._merge_tables(
                result[previous_index],
                block,
                assessment=assessment,
            )
            continued_table_count += 1
            inferred_continued_table_count += int(not assessment["signals"]["explicit_marker"])

        # Spec §8 (representation fields): every table block carries its
        # derived structure facts — additive keys, blocks are never deleted
        # or rewritten.
        status_counts: dict[str, int] = {}
        for block in result:
            if block.get("type") != "table":
                continue
            self._annotate_table_structure(block)
            status = str(block.get("table_status"))
            status_counts[status] = status_counts.get(status, 0) + 1
        return result, {
            "table_count": sum(block.get("type") == "table" for block in result),
            "table_status_counts": status_counts,
            "continued_table_count": continued_table_count,
            "inferred_continued_table_count": inferred_continued_table_count,
            "continuation_decisions": continuation_decisions,
            "caption_associations": self._caption_associations(blocks, captions),
        }

    @staticmethod
    def _annotate_table_structure(block: dict[str, Any]) -> None:
        """Attach spec-§8 table structure facts, derived from evidence only.

        ``table_status`` and ``columns_confidence`` summarize recorded grid
        facts; the derivation inputs (source kind, ragged rows, continuation)
        stay visible so consumers can distrust the summary.
        """
        payload = block.get("raw_payload") or {}
        headers = list(payload.get("headers") or [])
        rows = list(payload.get("rows") or [])
        column_count = max([len(headers), *(len(row) for row in rows), 0])
        ragged_row_count = sum(1 for row in rows if len(row) != column_count)
        columns_confidence = (
            round((len(rows) - ragged_row_count) / len(rows), 2) if rows else None
        )
        detection_method = _DETECTION_METHOD_BY_SOURCE_KIND.get(
            str(block.get("source_kind")), "unknown"
        )
        joined_continuation = bool(
            block.get("continued_table") or int(block.get("merged_from", 1)) > 1
        )
        reasons: list[str] = []
        if detection_method in {"alignment", "alignment_ocr"}:
            reasons.append("columns_inferred_from_alignment")
        if ragged_row_count:
            reasons.append("ragged_rows")
        if joined_continuation:
            reasons.append("cross_page_continuation_joined")
        # A complete status needs positive evidence: a geometry-detected grid
        # with a consistent cell matrix and no inferred joins.
        table_status = (
            "complete"
            if detection_method == "geometry"
            and bool(rows)
            and not ragged_row_count
            and not joined_continuation
            else "partial"
        )
        block["table_status"] = table_status
        block["table_status_reasons"] = reasons
        block["table_detection_method"] = detection_method
        block["column_count"] = column_count
        block["ragged_row_count"] = ragged_row_count
        block["columns_confidence"] = columns_confidence
        block["rows_recovered"] = len(rows)
        # The raw region (bbox) and immutable table evidence always survive
        # in the block/metadata — the field states the fact explicitly.
        block["raw_region_preserved"] = True

    @staticmethod
    def _find_previous_table(blocks: list[dict[str, Any]]) -> int | None:
        for index in range(len(blocks) - 1, -1, -1):
            if blocks[index].get("type") == "table":
                return index
        return None

    def _assess_continuation(
        self,
        *,
        previous: dict[str, Any],
        current: dict[str, Any],
        source_blocks: list[dict[str, Any]],
        current_index: int,
        captions: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        previous_pages = [page for page in previous.get("pages") or [previous.get("page")] if page is not None]
        previous_page = max(previous_pages, default=None)
        current_page = current.get("page")
        evidence_ids = self._all_evidence_ids(previous) + self._all_evidence_ids(current)
        decision: dict[str, Any] = {
            "previous_pages": previous_pages,
            "current_page": current_page,
            "previous_evidence_ids": self._all_evidence_ids(previous),
            "current_evidence_ids": self._all_evidence_ids(current),
            "merged": False,
            "reason": "",
            "score": 0.0,
            "threshold": self._INFERRED_THRESHOLD,
            "signals": {},
        }
        if previous_page is None or current_page != previous_page + 1:
            decision["reason"] = "tables_are_not_on_adjacent_pages"
            decision["evidence_ids"] = evidence_ids
            return decision

        header_similarity = self._header_similarity(previous, current)
        if header_similarity < self._MIN_HEADER_SIMILARITY:
            decision.update(
                {
                    "reason": "header_similarity_below_minimum",
                    "evidence_ids": evidence_ids,
                    "signals": {"header_similarity": header_similarity},
                }
            )
            return decision

        explicit_marker = self._has_explicit_continuation(source_blocks, current_index)
        column_similarity = self._column_similarity(previous, current)
        boundary_transition = self._boundary_transition(previous, current)
        caption_similarity = self._caption_similarity(previous, current, captions)
        column_edges = self._column_edge_stability(previous, current)
        threshold = self._EXPLICIT_THRESHOLD if explicit_marker else self._INFERRED_THRESHOLD
        score = min(
            1.0,
            header_similarity * 0.40
            + column_similarity * 0.10
            + boundary_transition * 0.25
            + caption_similarity * 0.10
            + (0.25 if explicit_marker else 0.0),
        )
        signals = {
            "header_similarity": header_similarity,
            "column_similarity": column_similarity,
            "boundary_transition": boundary_transition,
            "caption_similarity": caption_similarity,
            "explicit_marker": explicit_marker,
            # Evidence only: column geometry never changes the score, it is
            # recorded so consumers can see when a continuation re-flows
            # column widths.
            "column_edges_stable": column_edges["stable"],
            "column_edges_max_shift_pt": column_edges["max_shift_pt"],
        }
        decision.update(
            {
                "evidence_ids": evidence_ids,
                "score": round(score, 3),
                "threshold": threshold,
                "signals": signals,
                "merged": score >= threshold,
                "reason": "high_confidence_continuation"
                if score >= threshold
                else "continuation_score_below_threshold",
            }
        )
        return decision

    @staticmethod
    def _all_evidence_ids(block: dict[str, Any]) -> list[str]:
        values = block.get("evidence_ids") or [block.get("evidence_id")]
        return [str(value) for value in values if value]

    @classmethod
    def _header_similarity(cls, previous: dict[str, Any], current: dict[str, Any]) -> float:
        previous_headers = cls._header_signature(previous)
        current_headers = cls._header_signature(current)
        if not previous_headers or not current_headers:
            return 0.0
        max_columns = max(len(previous_headers), len(current_headers))
        paired = zip(previous_headers, current_headers)
        similarity = sum(
            SequenceMatcher(a=prior, b=following).ratio() for prior, following in paired
        ) / max_columns
        return round(similarity, 3)

    @staticmethod
    def _header_signature(block: dict[str, Any]) -> tuple[str, ...]:
        payload = block.get("raw_payload") or {}
        headers = payload.get("headers") or []
        return tuple(" ".join(str(header).casefold().split()) for header in headers)

    @classmethod
    def _column_similarity(cls, previous: dict[str, Any], current: dict[str, Any]) -> float:
        previous_count = cls._column_count(previous)
        current_count = cls._column_count(current)
        if not previous_count or not current_count:
            return 0.0
        return round(min(previous_count, current_count) / max(previous_count, current_count), 3)

    @staticmethod
    def _column_count(block: dict[str, Any]) -> int:
        payload = block.get("raw_payload") or {}
        headers = payload.get("headers") or []
        rows = payload.get("rows") or []
        return max([len(headers), *(len(row) for row in rows), 0])

    @staticmethod
    def _boundary_transition(previous: dict[str, Any], current: dict[str, Any]) -> float:
        previous_bbox = previous.get("bboxes", [previous.get("bbox")])[-1]
        current_bbox = current.get("bbox")
        previous_height = (previous.get("raw_payload") or {}).get("page_height")
        current_height = (current.get("raw_payload") or {}).get("page_height")
        if not previous_bbox or not current_bbox or not previous_height or not current_height:
            return 0.0
        previous_bottom_ratio = float(previous_bbox[3]) / float(previous_height)
        current_top_ratio = float(current_bbox[1]) / float(current_height)
        previous_score = min(1.0, max(0.0, (previous_bottom_ratio - 0.55) / 0.20))
        current_score = min(1.0, max(0.0, (0.45 - current_top_ratio) / 0.20))
        return round((previous_score + current_score) / 2, 3)

    @classmethod
    def _captions_by_evidence_id(cls, blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        captions: dict[str, dict[str, Any]] = {}
        for index, block in enumerate(blocks):
            if block.get("type") != "table":
                continue
            caption = cls._nearest_caption(block, blocks, index)
            if not caption:
                continue
            for evidence_id in cls._all_evidence_ids(block):
                captions[evidence_id] = caption
        return captions

    @staticmethod
    def _nearest_caption(
        table: dict[str, Any], blocks: list[dict[str, Any]], table_index: int
    ) -> dict[str, Any] | None:
        """Find the caption nearest a table, same page first, then ±1 page.

        A caption stranded on the previous page (bottom) or the next page
        (top) is still the table's caption; the association is recorded with
        an explicit page offset so consumers see the mismatch.
        """
        page = table.get("page")
        if not isinstance(page, int):
            # Table blocks without a page number cannot do ±1-page association; give up honestly
            return None

        # Same page, directly above the table.
        for candidate in reversed(blocks[max(0, table_index - 4):table_index]):
            if candidate.get("page") != page or candidate.get("type") not in {"text", "heading"}:
                continue
            text = str(candidate.get("text", "")).strip()
            if _TABLE_CAPTION_RE.match(text):
                return {
                    "text": TableReconstructor._normalize_caption(text),
                    "caption_page": page,
                    "page_offset": 0,
                }

        # Caption stranded at the bottom of the previous page.
        for candidate in reversed(blocks[max(0, table_index - 10):table_index]):
            if candidate.get("page") != page - 1 or candidate.get("type") not in {"text", "heading"}:
                continue
            text = str(candidate.get("text", "")).strip()
            if _TABLE_CAPTION_RE.match(text):
                return {
                    "text": TableReconstructor._normalize_caption(text),
                    "caption_page": page - 1,
                    "page_offset": -1,
                }

        # Caption pushed to the top of the next page (caption-below style).
        for candidate in blocks[table_index + 1:table_index + 6]:
            if candidate.get("page") != page + 1 or candidate.get("type") not in {"text", "heading"}:
                continue
            text = str(candidate.get("text", "")).strip()
            if _TABLE_CAPTION_RE.match(text):
                return {
                    "text": TableReconstructor._normalize_caption(text),
                    "caption_page": page + 1,
                    "page_offset": 1,
                }
        return None

    @staticmethod
    def _caption_associations(
        blocks: list[dict[str, Any]],
        captions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Surface every caption↔table association, including wrong-page ones."""
        associations: list[dict[str, Any]] = []
        for index, block in enumerate(blocks):
            if block.get("type") != "table":
                continue
            caption = captions.get(str(block.get("evidence_id")))
            if not caption:
                continue
            associations.append(
                {
                    "evidence_id": block.get("evidence_id"),
                    "table_page": block.get("page"),
                    "caption": caption["text"],
                    "caption_page": caption["caption_page"],
                    "page_offset": caption["page_offset"],
                }
            )
        return associations

    @staticmethod
    def _normalize_caption(caption: str) -> str:
        return " ".join(caption.casefold().split())

    @classmethod
    def _caption_similarity(
        cls,
        previous: dict[str, Any],
        current: dict[str, Any],
        captions: dict[str, dict[str, Any]],
    ) -> float:
        previous_caption = next(
            (captions[evidence_id]["text"] for evidence_id in cls._all_evidence_ids(previous) if evidence_id in captions),
            None,
        )
        current_caption = next(
            (captions[evidence_id]["text"] for evidence_id in cls._all_evidence_ids(current) if evidence_id in captions),
            None,
        )
        if not previous_caption or not current_caption:
            return 0.0
        return round(SequenceMatcher(a=previous_caption, b=current_caption).ratio(), 3)

    @staticmethod
    def _column_edge_stability(
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare column boundary geometry between two tables.

        Evidence only: a continuation whose columns shift is recorded, never
        re-aligned.  ``stable`` is True when both tables expose the same
        number of column edges within a 2pt tolerance.
        """
        previous_edges = (previous.get("raw_payload") or {}).get("column_edges") or []
        current_edges = (current.get("raw_payload") or {}).get("column_edges") or []
        if not previous_edges or not current_edges:
            return {"stable": None, "max_shift_pt": None}
        if len(previous_edges) != len(current_edges):
            return {"stable": False, "max_shift_pt": None}
        max_shift = max(
            abs(float(a) - float(b)) for a, b in zip(previous_edges, current_edges)
        )
        return {"stable": max_shift <= 2.0, "max_shift_pt": round(max_shift, 2)}

    @staticmethod
    def _has_explicit_continuation(blocks: list[dict[str, Any]], table_index: int) -> bool:
        current = blocks[table_index]
        current_page = current.get("page")
        for block in blocks[max(0, table_index - 4):table_index]:
            if block.get("page") != current_page:
                continue
            if _CONTINUATION_RE.search(str(block.get("text", ""))):
                return True
        return False

    def _merge_tables(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
        *,
        assessment: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(previous)
        previous_payload = dict(previous.get("raw_payload") or {})
        current_payload = dict(current.get("raw_payload") or {})
        headers = list(previous_payload.get("headers") or current_payload.get("headers") or [])
        current_rows = list(current_payload.get("rows") or [])
        repeated_header_dropped = False
        if current_rows and headers and self._rows_match_header(current_rows[0], headers):
            # The continuation repeats the header row; keep the first-page
            # header and drop the duplicate instead of duplicating it as data.
            current_rows = current_rows[1:]
            repeated_header_dropped = True
        elif (
            not repeated_header_dropped
            and current_payload.get("headers")
            and headers
            and self._rows_match_header(list(current_payload["headers"]), headers)
        ):
            # The extractor already kept the continuation page's repeated
            # header out of ``rows`` (header detected in-table). Record that
            # the duplicate header was dropped from the merged output.
            repeated_header_dropped = True
        rows = list(previous_payload.get("rows") or []) + current_rows
        previous_links = list(previous_payload.get("continuation_links") or [])
        merged_payload = {
            **previous_payload,
            "headers": headers,
            "rows": rows,
            "continued_from_pages": list(previous.get("pages") or [previous.get("page")]),
            "continued_to_page": current.get("page"),
            "continuation_links": previous_links + [assessment],
            "repeated_header_dropped": repeated_header_dropped,
        }
        if assessment["signals"].get("column_edges_stable") is False:
            merged_payload["column_width_changed"] = True
            merged_payload["column_edges_max_shift_pt"] = assessment["signals"].get(
                "column_edges_max_shift_pt"
            )
        merged["raw_payload"] = merged_payload
        merged["text"] = self._render_searchable_table(headers, rows)
        merged["pages"] = list(previous.get("pages") or [previous.get("page")]) + [current.get("page")]
        merged["bboxes"] = list(previous.get("bboxes") or [previous.get("bbox")]) + [current.get("bbox")]
        merged["evidence_ids"] = list(
            dict.fromkeys(
                list(previous.get("evidence_ids") or [previous.get("evidence_id")])
                + list(current.get("evidence_ids") or [current.get("evidence_id")])
            )
        )
        merged["continued_table"] = True
        merged["continuation_confidence"] = assessment["score"]
        merged["continuation_method"] = (
            "explicit_marker"
            if assessment["signals"]["explicit_marker"]
            else "multi_signal_inference"
        )
        merged["merged_from"] = int(previous.get("merged_from", 1)) + int(current.get("merged_from", 1))
        return merged

    @staticmethod
    def _rows_match_header(row: list[Any], headers: list[str]) -> bool:
        """Case/space-insensitive equality used to spot a repeated header row."""
        def normalize(values: list[Any]) -> list[str]:
            """Fold case and whitespace value by value."""
            return [" ".join(str(value).casefold().split()) for value in values]

        return normalize(row) == normalize(headers)

    @staticmethod
    def _render_searchable_table(headers: list[str], rows: list[list[str]]) -> str:
        columns = max(len(headers), *(len(row) for row in rows), 1)
        normalized_headers = headers + [""] * (columns - len(headers))
        lines = ["[Table]"]
        if any(normalized_headers):
            lines.extend(
                [
                    "| " + " | ".join(normalized_headers) + " |",
                    "| " + " | ".join("---" for _ in range(columns)) + " |",
                ]
            )
        for row in rows:
            normalized_row = row + [""] * (columns - len(row))
            lines.append("| " + " | ".join(normalized_row) + " |")
        return "\n".join(lines)
