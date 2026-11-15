"""SemanticCleaner — policy-based semantic blank-line cleaner.

Core principles:
    - no embedding / perplexity / entropy or other latent metrics
    - every decision is based on structural patterns (run of blank
      lines, position, surrounding structure)
    - every decision carries an explainable reason

Processing flow:
    1. split into lines
    2. identify blank regions (runs of blank lines)
    3. classify each region per CleaningLevel policy
    4. clean (remove / collapse / keep)
    5. emit a CleaningResult with the full trace

Extension points:
    - new domain-specific detectors (code blanks, table blanks, ...)
    - new CleaningLevel policy rules
"""

import re
from typing import List, Tuple

from provendoc.core.cleaning.cleaning_policy import (
    BlankLineSemantics,
    CleaningDecision,
    CleaningLevel,
    CleaningResult,
)
from provendoc.core.cleaning.furniture_detector import detect_page_furniture


class SemanticCleaner:
    """Distinguishes semantic blanks from artifact blanks."""

    def __init__(
        self,
        level: CleaningLevel = CleaningLevel.STANDARD,
        max_consecutive_blanks: int = 2,
        preserve_leading_blank: bool = False,
        preserve_trailing_blank: bool = False,
    ):
        """Create a cleaner.

        Args:
            level: cleaning level (see CleaningLevel; default STANDARD).
            max_consecutive_blanks: maximum run of consecutive blank
                lines to keep; anything longer is compressed per policy.
            preserve_leading_blank: keep blanks at the start of the text.
            preserve_trailing_blank: keep blanks at the end of the text.
        """
        self.level = level
        self.max_consecutive_blanks = max_consecutive_blanks
        self.preserve_leading_blank = preserve_leading_blank
        self.preserve_trailing_blank = preserve_trailing_blank

    # ======================================
    # public API
    # ======================================

    def clean(self, text: str) -> CleaningResult:
        """Clean text and return a CleaningResult (with trace).

        Args:
            text: raw text (newlines already normalized)

        Returns:
            CleaningResult: cleaned text + decision record
        """
        if not text:
            return CleaningResult(
                text="",
                decisions=[],
                stats={
                    "original_lines": 0,
                    "cleaned_lines": 0,
                    "blank_lines_removed": 0,
                    "furniture_lines_removed": 0,
                    "page_number_lines_removed": 0,
                    "toc_lines_annotated": 0,
                    "toc_lines_removed": 0,
                },
            )

        lines = text.split("\n")
        original_line_count = len(lines)

        # Identify blank regions
        blank_regions = self._find_blank_regions(lines)

        # Page-furniture detection: non-empty-line artifacts
        # (cross-page periodic furniture / bare page-number / TOC
        # dot-leader lines); its decisions share the original
        # line-number space with the blank-region decisions and take
        # precedence.
        furniture = detect_page_furniture(lines, blank_regions, self.level)

        # Decide each region per policy
        decisions = list(furniture["decisions"])
        lines_to_remove = set()

        for d in furniture["decisions"]:
            if d.decision == "removed":
                start, end = d.line_range
                for i in range(start, end):
                    lines_to_remove.add(i)

        for region in blank_regions:
            decision = self._decide_region(lines, region)
            decisions.append(decision)

            if decision.decision == "removed":
                start, end = decision.line_range
                for i in range(start, end):
                    lines_to_remove.add(i)
            elif decision.decision == "collapsed":
                # keep the first line, drop the rest
                start, end = decision.line_range
                for i in range(start + 1, end):
                    lines_to_remove.add(i)
            # "preserved" → drop nothing

        # Rebuild the text
        cleaned_lines = [
            line for i, line in enumerate(lines)
            if i not in lines_to_remove
        ]

        cleaned_text = "\n".join(cleaned_lines)

        # Stats (furniture removals do not count into
        # blank_lines_removed; existing keys unchanged)
        blank_lines_removed = sum(
            1 for d in decisions if d.decision == "removed"
            and d.semantics != BlankLineSemantics.PAGE_FURNITURE
            for _ in range(d.line_range[0], d.line_range[1])
        )
        blank_lines_collapsed = sum(
            (d.line_range[1] - d.line_range[0] - 1)
            for d in decisions if d.decision == "collapsed"
        )

        # Sort the trace by original line number (furniture and
        # blank-region decisions interleaved)
        decisions.sort(key=lambda d: (d.line_range[0], d.line_range[1]))

        stats = {
            "original_lines": original_line_count,
            "cleaned_lines": len(cleaned_lines),
            "blank_regions_found": len(blank_regions),
            "blank_lines_removed": blank_lines_removed,
            "blank_lines_collapsed": blank_lines_collapsed,
            "blank_lines_preserved": sum(
                (d.line_range[1] - d.line_range[0])
                for d in decisions
                if d.decision == "preserved"
                and d.semantics != BlankLineSemantics.PAGE_FURNITURE
            ),
            "cleaning_level": self.level.value,
        }
        stats.update(furniture["stats"])

        return CleaningResult(
            text=cleaned_text,
            decisions=decisions,
            stats=stats,
        )

    # ======================================
    # region detection
    # ======================================

    def _find_blank_regions(self, lines: List[str]) -> List[Tuple[int, int]]:
        """Find all runs of consecutive blank lines.

        Returns:
            List of (start_index, end_index) where range is [start, end)
        """
        regions = []
        i = 0
        n = len(lines)

        while i < n:
            if lines[i].strip() == "":
                start = i
                while i < n and lines[i].strip() == "":
                    i += 1
                regions.append((start, i))
            else:
                i += 1

        return regions

    # ======================================
    # decision engine
    # ======================================

    def _decide_region(
        self,
        lines: List[str],
        region: Tuple[int, int],
    ) -> CleaningDecision:
        """Decide one blank region."""
        start, end = region
        length = end - start
        original_text = "\n".join(lines[start:end])

        # Position analysis
        is_leading = (start == 0)
        is_trailing = (end == len(lines))
        context_before = lines[start - 1] if start > 0 else ""
        context_after = lines[end] if end < len(lines) else ""

        # ====== CONSERVATIVE policy ======
        if self.level == CleaningLevel.CONSERVATIVE:
            # Mildest level: only obvious artifacts are cleaned —
            # leading/trailing blanks (unless explicitly configured
            # to keep them) and runs exceeding max_consecutive_blanks
            # (collapsed but the paragraph boundary is kept); ordinary
            # paragraph-separator blanks are always kept.
            if is_leading or is_trailing:
                if (is_leading and self.preserve_leading_blank) or (
                    is_trailing and self.preserve_trailing_blank
                ):
                    return CleaningDecision(
                        line_range=region,
                        original_text=original_text,
                        decision="preserved",
                        semantics=BlankLineSemantics.STRUCTURAL,
                        reason="首尾空行按配置保留（conservative 策略）",
                        confidence="high",
                    )
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="removed",
                    semantics=BlankLineSemantics.ARTIFACT,
                    reason="首尾空白行无语义价值（conservative 策略）",
                    confidence="high",
                )

            # Exception for blanks between list items / code blocks
            # (including long runs)
            if self._is_between_list_items(context_before, context_after):
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="preserved",
                    semantics=BlankLineSemantics.DISCOURSE,
                    reason="列表项之间的空行是话语标记，保留",
                    confidence="high",
                )
            if self._is_between_code_blocks(context_before, context_after):
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="preserved",
                    semantics=BlankLineSemantics.STRUCTURAL,
                    reason="代码块之间的空行是结构分隔，保留",
                    confidence="high",
                )

            # Overly long blank runs are artifacts: collapse to a single
            # blank line — the same paragraph-boundary contract as standard
            # (downstream splits on \n\n), ordinary blanks untouched.
            if length > self.max_consecutive_blanks:
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="collapsed",
                    semantics=BlankLineSemantics.STRUCTURAL,
                    reason=(
                        f"连续 {length} 个空行超出 "
                        f"max_consecutive_blanks={self.max_consecutive_blanks}"
                        "，折叠为单个空行（conservative 策略：保留段落边界）"
                    ),
                    confidence="high",
                )

            # Ordinary paragraph-separator blanks: keep (only obvious
            # artifacts get cleaned).
            return CleaningDecision(
                line_range=region,
                original_text=original_text,
                decision="preserved",
                semantics=BlankLineSemantics.STRUCTURAL,
                reason="段落分隔空行保留（conservative 策略：只清理明显 artifact）",
                confidence="high",
            )

        # ====== STANDARD policy ======
        if self.level == CleaningLevel.STANDARD:
            # 1. Trim leading/trailing blanks (unless configured to
            #    keep them)
            if is_leading or is_trailing:
                if (is_leading and self.preserve_leading_blank) or (
                    is_trailing and self.preserve_trailing_blank
                ):
                    return CleaningDecision(
                        line_range=region,
                        original_text=original_text,
                        decision="preserved",
                        semantics=BlankLineSemantics.STRUCTURAL,
                        reason="首尾空行按配置保留",
                        confidence="high",
                    )
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="removed",
                    semantics=BlankLineSemantics.ARTIFACT,
                    reason="首尾空白行无语义价值",
                    confidence="high",
                )

            # 2. Structure-aware: keep blanks between list items /
            #    code blocks (including 3+ blank runs)
            if self._is_between_list_items(context_before, context_after):
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="preserved",
                    semantics=BlankLineSemantics.DISCOURSE,
                    reason="列表项之间的空行是话语标记，保留",
                    confidence="high",
                )

            if self._is_between_code_blocks(context_before, context_after):
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="preserved",
                    semantics=BlankLineSemantics.STRUCTURAL,
                    reason="代码块之间的空行是结构分隔，保留",
                    confidence="high",
                )

            # 3. Long blank runs: collapse to a single blank line
            #    (paragraph-boundary contract)
            if length >= 3:
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="collapsed",
                    semantics=BlankLineSemantics.STRUCTURAL,
                    reason=f"连续 {length} 个空行，折叠为单个空行（段落边界契约：下游按 \\n\\n 切段）",
                    confidence="high",
                )

            # 4. Default: 1-2 blanks collapse to a single blank line —
            #    paragraph boundaries are split explicitly downstream
            #    (TxtNarrativeRepresentation._add_paragraph_units)
            #    on \n\n, so the cleaning layer must keep the boundary
            #    mark.
            return CleaningDecision(
                line_range=region,
                original_text=original_text,
                decision="collapsed",
                semantics=BlankLineSemantics.STRUCTURAL,
                reason="段落分隔空行折叠为单个空行（standard 策略：下游按 \\n\\n 切段）",
                confidence="high",
            )

        # ====== AGGRESSIVE policy ======
        # standard plus more inference (still no embedding/entropy),
        # based on discourse patterns
        if self.level == CleaningLevel.AGGRESSIVE:
            # 1. Trim leading/trailing blanks (unless configured to
            #    keep them)
            if is_leading or is_trailing:
                if (is_leading and self.preserve_leading_blank) or (
                    is_trailing and self.preserve_trailing_blank
                ):
                    return CleaningDecision(
                        line_range=region,
                        original_text=original_text,
                        decision="preserved",
                        semantics=BlankLineSemantics.STRUCTURAL,
                        reason="首尾空行按配置保留",
                        confidence="high",
                    )
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="removed",
                    semantics=BlankLineSemantics.ARTIFACT,
                    reason="首尾空白行无语义价值",
                    confidence="high",
                )

            # 2. Long blank runs: remove entirely (downstream splits on
            #    structure explicitly)
            if length > self.max_consecutive_blanks:
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="removed",
                    semantics=BlankLineSemantics.ARTIFACT,
                    reason=f"连续 {length} 个空行，全部移除",
                    confidence="high",
                )

            # 3. Short lines on both sides (<20 chars): likely list
            #    items or headings — keep the blank
            if len(context_before.strip()) < 20 and len(context_after.strip()) < 20:
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="preserved",
                    semantics=BlankLineSemantics.DISCOURSE,
                    reason="前后都是短行，空行可能是列表/标题分隔，保留",
                    confidence="medium",
                )

            # 4. Long paragraphs on both sides: blank is likely a
            #    paragraph separator — keep
            if len(context_before.strip()) > 100 and len(context_after.strip()) > 100:
                return CleaningDecision(
                    line_range=region,
                    original_text=original_text,
                    decision="preserved",
                    semantics=BlankLineSemantics.STRUCTURAL,
                    reason="前后都是长段落，空行是段落分隔，保留",
                    confidence="medium",
                )

            # 5. Default: remove blanks of unclear semantics
            return CleaningDecision(
                line_range=region,
                original_text=original_text,
                decision="removed",
                semantics=BlankLineSemantics.ARTIFACT,
                reason="激进策略：不确定语义的空行清理",
                confidence="low",
            )

        # fallback
        return CleaningDecision(
            line_range=region,
            original_text=original_text,
            decision="preserved",
            semantics=BlankLineSemantics.PRESERVATION_CANDIDATE,
            reason="默认保留",
            confidence="low",
        )

    # ======================================
    # structural detectors (explainable)
    # ======================================

    def _is_between_list_items(self, before: str, after: str) -> bool:
        """Whether both neighbors are list items."""
        before_stripped = before.strip()
        after_stripped = after.strip()

        # Markdown lists: -, *, +, 1.
        list_patterns = [
            r"^[-*+]\s+",
            r"^\d+\.\s+",
        ]

        before_is_list = any(
            re.search(p, before_stripped) for p in list_patterns
        )
        after_is_list = any(
            re.search(p, after_stripped) for p in list_patterns
        )

        return before_is_list and after_is_list

    def _is_between_code_blocks(self, before: str, after: str) -> bool:
        """Whether either neighbor carries a code-block marker."""
        before_stripped = before.strip()
        after_stripped = after.strip()

        # Code-block end/start markers
        code_markers = ["```", "~~~"]

        return (
            any(before_stripped.startswith(m) for m in code_markers)
            or any(after_stripped.startswith(m) for m in code_markers)
        )

    # ======================================
    # convenience methods
    # ======================================

    @classmethod
    def conservative(cls) -> "SemanticCleaner":
        """Create a conservative cleaner."""
        return cls(level=CleaningLevel.CONSERVATIVE)

    @classmethod
    def standard(cls) -> "SemanticCleaner":
        """Create a standard cleaner (default)."""
        return cls(level=CleaningLevel.STANDARD)

    @classmethod
    def aggressive(cls) -> "SemanticCleaner":
        """Create an aggressive cleaner."""
        return cls(level=CleaningLevel.AGGRESSIVE)
