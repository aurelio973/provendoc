# ============================================================
# Code Domain
# Source-preserving cleaning policy
# ============================================================
"""Code-domain cleaner.

The generic SemanticCleaner targets natural-language paragraphs; its
standard mode reshapes whitespace per narrative semantics. Code has no
chunking at this stage, so it gets a dedicated fidelity-preserving
policy:

    - never delete or rewrite code, strings, comments, indentation,
      or in-line whitespace;
    - only drop purely blank leading/trailing lines, and collapse 3+
      consecutive *purely blank* lines down to two;
    - never attempt syntax repair; AST spans always reference the
      pre-cleaning parse snapshot;
    - every whitespace transformation is auditable via the
      CleaningResult trace.
"""

from __future__ import annotations

from provendoc.core.cleaning.cleaning_policy import (
    BlankLineSemantics,
    CleaningDecision,
    CleaningResult,
)


class CodeCleaner:
    """Minimal whitespace cleaner that preserves code semantics and comments."""

    mode = "code_preserving"

    def clean(self, text: str) -> CleaningResult:
        """Clean whitespace under the code-fidelity policy.

        Only purely blank leading/trailing lines are removed and 3+
        consecutive purely blank lines are collapsed to two; code,
        strings, comments, indentation, and in-line whitespace are
        never touched. Every whitespace transformation is recorded
        in decisions for audit.

        Args:
            text: the raw code text from the parse stage (unmodified).

        Returns:
            CleaningResult: cleaned text + decision trace + stats.
        """
        if not text:
            return CleaningResult(
                text="",
                stats={
                    "cleaning_mode": self.mode,
                    "original_lines": 0,
                    "cleaned_lines": 0,
                    "blank_lines_removed": 0,
                },
            )

        lines = text.split("\n")
        original_count = len(lines)
        decisions: list[CleaningDecision] = []
        start = 0
        end = len(lines)

        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1

        if start:
            decisions.append(
                CleaningDecision(
                    line_range=(0, start),
                    original_text="\n".join(lines[:start]),
                    decision="removed",
                    semantics=BlankLineSemantics.ARTIFACT,
                    reason="代码文件开头的纯空行不承载代码或注释语义",
                    confidence="high",
                )
            )
        if end < len(lines):
            decisions.append(
                CleaningDecision(
                    line_range=(end, len(lines)),
                    original_text="\n".join(lines[end:]),
                    decision="removed",
                    semantics=BlankLineSemantics.ARTIFACT,
                    reason="代码文件末尾的纯空行不承载代码或注释语义",
                    confidence="high",
                )
            )

        kept = lines[start:end]
        output: list[str] = []
        cursor = 0
        collapsed = 0
        while cursor < len(kept):
            if kept[cursor].strip():
                output.append(kept[cursor])
                cursor += 1
                continue

            region_start = cursor
            while cursor < len(kept) and not kept[cursor].strip():
                cursor += 1
            region_length = cursor - region_start
            if region_length > 2:
                output.extend(kept[region_start:region_start + 2])
                decisions.append(
                    CleaningDecision(
                        line_range=(start + region_start, start + cursor),
                        original_text="\n".join(kept[region_start:cursor]),
                        decision="collapsed",
                        semantics=BlankLineSemantics.STRUCTURAL,
                        reason=(
                            "连续纯空行压缩至两个；不影响代码、注释、缩进或行内空白"
                        ),
                        confidence="high",
                    )
                )
                collapsed += region_length - 2
            else:
                output.extend(kept[region_start:cursor])

        result = "\n".join(output)
        removed = start + (len(lines) - end)
        return CleaningResult(
            text=result,
            decisions=decisions,
            stats={
                "cleaning_mode": self.mode,
                "original_lines": original_count,
                "cleaned_lines": len(output),
                "blank_lines_removed": removed,
                "blank_lines_collapsed": collapsed,
                "comments_preserved": True,
                "code_tokens_rewritten": 0,
            },
        )
