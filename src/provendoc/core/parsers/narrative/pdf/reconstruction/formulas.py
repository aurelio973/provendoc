"""Formula reconstruction over immutable PDF evidence.

Three evidence sources converge here:

1. MinerU layout regions typed ``formula`` reach reconstruction as
   ``type="formula"`` blocks (the overlay preserves that semantics).  This
   reconstructor records their equation numbers without rewriting text.
2. Native spans whose fonts are known math glyph fonts (Symbol, Computer
   Modern math, etc.) mark the owning block as a *math font candidate*.
   Font evidence alone never proves a formula, so the mark is evidence-only:
   the block keeps its original type and the mark lands in ``raw_payload``.
3. Formulas split across lines or pages are the classic silent-loss case:
   line-wrapped fragments are merged only under tight geometric adjacency,
   and page-boundary continuations are reported as facts (never merged),
   because a cross-page merge would fabricate one formula from two evidence
   regions.
"""

from __future__ import annotations

from typing import Any

from provendoc.core.structure.equation_numbers import (
    EquationNumberMatch,
    match_equation_number,
)

# Known math glyph font families. Matched case-insensitively as substrings of
# the span font name reported by PyMuPDF. Conservative on purpose: plain
# Computer Modern roman/italic (cmr/cmti) are body-text fonts and excluded.
_MATH_FONT_TOKENS = (
    "symbol",
    "cmsy",
    "cmmi",
    "cmex",
    "msam",
    "msbm",
    "eufm",
    "euler",
    "euclid",
    "mtmi",
    "math",
)

# Line-wrap merge tolerance: a wrap fragment sits within this multiple of the
# fragment height below the previous formula region.
_LINE_WRAP_GAP_FACTOR = 1.5
_DEFAULT_FRAGMENT_HEIGHT = 12.0

# A formula region whose bottom edge is within this fraction of the page
# height is eligible for the cross-page continuation report.
_PAGE_BOTTOM_FRACTION = 0.18
# A continuation fragment on the next page starts within this fraction of
# the page height from the top.
_PAGE_TOP_FRACTION = 0.18


def _is_formula_like(block: dict[str, Any]) -> bool:
    """A real formula block or an evidence-only math-font glyph candidate."""
    return block.get("type") == "formula" or bool(block.get("math_font_candidate"))


def _continuation_kind(block: dict[str, Any], follower: dict[str, Any]) -> str:
    if block.get("type") == "formula" and follower.get("type") == "formula":
        return "formula"
    if block.get("type") == "formula" or follower.get("type") == "formula":
        return "mixed_formula_and_math_font"
    return "math_font_candidate"


def _is_math_font(font: str) -> bool:
    lowered = font.strip().casefold()
    return any(token in lowered for token in _MATH_FONT_TOKENS)


def _span_index(evidence: list[Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Index native span evidence by evidence ID for payload-free blocks."""
    index: dict[str, list[dict[str, Any]]] = {}
    for block in evidence or []:
        evidence_id = getattr(block, "evidence_id", None)
        raw_payload = getattr(block, "raw_payload", None)
        spans = raw_payload.get("spans") if isinstance(raw_payload, dict) else None
        if evidence_id and isinstance(spans, list) and spans:
            index[evidence_id] = spans
    return index


def _collect_spans(
    evidence_ids: list[Any],
    spans_by_evidence_id: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]] | None:
    for evidence_id in evidence_ids:
        spans = spans_by_evidence_id.get(str(evidence_id))
        if spans:
            return spans
    return None


class FormulaReconstructor:
    """Record formula facts over evidence; never relabel text as formula."""

    def reconstruct(
        self,
        blocks: list[dict[str, Any]],
        *,
        page_heights: dict[int, float] | None = None,
        evidence: list[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Annotate formula facts (never reclassify text as formulas)."""
        page_heights = page_heights or {}
        spans_by_evidence_id = _span_index(evidence)

        blocks, merged_line_wrap_count = self._merge_line_wraps(blocks)
        result: list[dict[str, Any]] = []
        formula_count = 0
        mineru_formula_count = 0
        equation_numbers: list[str] = []
        math_font_candidate_count = 0

        for block in blocks:
            if block.get("type") == "formula":
                block, eq_match = self._record_formula(block)
                formula_count += 1
                if (block.get("raw_payload") or {}).get("mineru_type") is not None:
                    mineru_formula_count += 1
                if eq_match is not None:
                    equation_numbers.append(eq_match.number)
                result.append(block)
                continue
            marked, math_evidence = self._mark_math_font_evidence(
                block, spans_by_evidence_id
            )
            if math_evidence is not None:
                math_font_candidate_count += 1
            result.append(marked)

        cross_page = self._cross_page_continuations(result, page_heights)
        return result, {
            "formula_count": formula_count,
            "mineru_formula_count": mineru_formula_count,
            "equation_numbers": equation_numbers,
            "math_font_candidate_count": math_font_candidate_count,
            "math_font_marking": "evidence_only",
            "merged_line_wrap_formula_count": merged_line_wrap_count,
            "cross_page_formula_continuations": cross_page,
        }

    # ------------------------------------------------------------------
    # MinerU formula blocks
    # ------------------------------------------------------------------

    @staticmethod
    def _record_formula(block: dict[str, Any]) -> tuple[dict[str, Any], EquationNumberMatch | None]:
        """Attach equation-number facts to a formula block, text untouched."""
        text = str(block.get("text", "")).strip()
        eq_match = match_equation_number(text)
        raw_payload = dict(block.get("raw_payload") or {})
        raw_payload["formula_text_verbatim"] = text
        if eq_match is not None:
            raw_payload["equation_number"] = eq_match.number
            raw_payload["equation_number_source"] = eq_match.source
            raw_payload["equation_number_pattern"] = eq_match.pattern
        block = {**block, "raw_payload": raw_payload}
        if eq_match is not None:
            block["equation_number"] = eq_match.number
        return block, eq_match

    # ------------------------------------------------------------------
    # Line-wrap merging
    # ------------------------------------------------------------------

    def _merge_line_wraps(
        self,
        blocks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Merge tightly adjacent same-page formula fragments.

        Two consecutive formula blocks merge only when they share a page,
        sit within a small vertical gap of each other, and the earlier
        fragment carries no equation number (a numbered formula is complete).
        """
        merged: list[dict[str, Any]] = []
        merge_count = 0
        for block in blocks:
            previous = merged[-1] if merged else None
            if previous is not None and self._is_line_wrap_pair(previous, block):
                merged[-1] = self._merge_pair(previous, block)
                merge_count += 1
                continue
            merged.append(dict(block))
        return merged, merge_count

    @staticmethod
    def _is_line_wrap_pair(previous: dict[str, Any], current: dict[str, Any]) -> bool:
        if previous.get("type") != "formula" or current.get("type") != "formula":
            return False
        if previous.get("page") != current.get("page"):
            return False
        # A numbered formula is complete; never absorb a following region.
        # (Numbers are recorded later, so judge from the text directly.)
        if match_equation_number(str(previous.get("text", ""))):
            return False
        prev_bbox = previous.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        curr_bbox = current.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        fragment_height = max(prev_bbox[3] - prev_bbox[1], curr_bbox[3] - curr_bbox[1])
        if fragment_height <= 0:
            fragment_height = _DEFAULT_FRAGMENT_HEIGHT
        gap = curr_bbox[1] - prev_bbox[3]
        return -fragment_height <= gap <= fragment_height * _LINE_WRAP_GAP_FACTOR

    @staticmethod
    def _merge_pair(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        previous = dict(previous)
        current = dict(current)
        previous["text"] = f"{previous.get('text', '').strip()} {current.get('text', '').strip()}".strip()
        previous["merged_from"] = previous.get("merged_from", 1) + 1
        previous_bbox = previous.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        current_bbox = current.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        previous["bbox"] = [
            min(previous_bbox[0], current_bbox[0]),
            min(previous_bbox[1], current_bbox[1]),
            max(previous_bbox[2], current_bbox[2]),
            max(previous_bbox[3], current_bbox[3]),
        ]
        merged_ids = list(previous.get("evidence_ids") or [previous.get("evidence_id")])
        merged_ids.append(current.get("evidence_id"))
        previous["evidence_ids"] = [str(value) for value in merged_ids if value]
        raw_payload = dict(previous.get("raw_payload") or {})
        merged_from_payload = dict(current.get("raw_payload") or {})
        raw_payload["line_wrap_merged_evidence_ids"] = [
            str(value) for value in (current.get("evidence_ids") or [current.get("evidence_id")]) if value
        ]
        raw_payload["line_wrap_merged"] = True
        raw_payload["formula_source_evidence"] = merged_from_payload.get("mineru_type")
        previous["raw_payload"] = raw_payload
        return previous

    # ------------------------------------------------------------------
    # Math-font glyph evidence (native spans)
    # ------------------------------------------------------------------

    @staticmethod
    def _mark_math_font_evidence(
        block: dict[str, Any],
        spans_by_evidence_id: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Mark native blocks whose spans use known math glyph fonts.

        The mark is evidence-only: block type, text, and geometry stay
        untouched so glyph-drawn formulas without a text representation
        remain visible without being asserted as formulas.  Span evidence
        comes from the block's own ``raw_payload`` when present, otherwise
        from the immutable evidence index (classified blocks lose their
        payload during heading classification).
        """
        raw_payload = block.get("raw_payload")
        spans: list[dict[str, Any]] | None = None
        if isinstance(raw_payload, dict) and isinstance(raw_payload.get("spans"), list):
            spans = raw_payload["spans"]
        else:
            evidence_ids = list(block.get("source_evidence_ids") or [])
            if block.get("evidence_id"):
                evidence_ids.insert(0, block["evidence_id"])
            spans = _collect_spans(evidence_ids, spans_by_evidence_id)
        if not spans:
            return block, None

        math_spans = [
            span
            for span in spans
            if isinstance(span, dict) and _is_math_font(str(span.get("font", "")))
        ]
        if not math_spans:
            return block, None

        total_chars = sum(len(str(span.get("text", ""))) for span in spans if isinstance(span, dict))
        math_chars = sum(len(str(span.get("text", ""))) for span in math_spans)
        evidence = {
            "math_font_candidate": True,
            "math_font_span_count": len(math_spans),
            "math_fonts": sorted(
                {
                    str(span.get("font", ""))
                    for span in math_spans
                }
            ),
            "math_font_char_coverage": round(math_chars / total_chars, 3) if total_chars else 0.0,
            "sample_math_spans": [
                {
                    "text": str(span.get("text", "")),
                    "font": str(span.get("font", "")),
                    "bbox": span.get("bbox"),
                }
                for span in math_spans[:5]
            ],
            "marking": "evidence_only_font_signal",
        }
        marked = dict(block)
        marked["math_font_candidate"] = True
        payload_base = raw_payload if isinstance(raw_payload, dict) else {}
        marked["raw_payload"] = {**payload_base, "math_font_evidence": evidence}
        return marked, evidence

    # ------------------------------------------------------------------
    # Cross-page continuation report
    # ------------------------------------------------------------------

    @staticmethod
    def _cross_page_continuations(
        blocks: list[dict[str, Any]],
        page_heights: dict[int, float],
    ) -> list[dict[str, Any]]:
        """Report formula regions that continue onto the next page.

        Report-only by design: merging across a page boundary would combine
        two evidence regions into one fabricated formula.  Downstream
        consumers get the page pair, evidence IDs, and text snippets and can
        decide for themselves.  Both real formula blocks (MinerU) and
        evidence-only math-font candidates (native glyph rows) are eligible;
        the ``kind`` field tells them apart.
        """
        continuations: list[dict[str, Any]] = []
        for index, block in enumerate(blocks[:-1]):
            page = block.get("page")
            if not isinstance(page, int):
                continue
            page_height = page_heights.get(page)
            if page_height is None:
                continue
            if not _is_formula_like(block):
                continue
            bbox = block.get("bbox") or [0.0, 0.0, 0.0, 0.0]
            if (page_height - bbox[3]) > page_height * _PAGE_BOTTOM_FRACTION:
                continue  # Not near the page bottom.
            if (block.get("raw_payload") or {}).get("equation_number"):
                continue  # Numbered formulas are complete.
            follower_height = page_heights.get(page + 1)
            for follower in blocks[index + 1 :]:
                if follower.get("page") != page + 1:
                    break
                if not _is_formula_like(follower):
                    continue
                follower_bbox = follower.get("bbox") or [0.0, 0.0, 0.0, 0.0]
                if follower_height is not None and follower_bbox[1] > follower_height * _PAGE_TOP_FRACTION:
                    continue  # Not near the page top; keep looking.
                continuations.append(
                    {
                        "kind": _continuation_kind(block, follower),
                        "from_page": page,
                        "to_page": page + 1,
                        "from_evidence_id": block.get("evidence_id"),
                        "to_evidence_id": follower.get("evidence_id"),
                        "from_text": str(block.get("text", ""))[:80],
                        "to_text": str(follower.get("text", ""))[:80],
                        "action": "reported_not_merged",
                    }
                )
                break
        return continuations


__all__ = ["FormulaReconstructor"]
