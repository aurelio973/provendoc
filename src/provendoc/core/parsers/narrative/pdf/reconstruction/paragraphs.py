"""Paragraph reconstruction from ordered, classified PDF blocks."""

from __future__ import annotations

_TERMINAL_PUNCTUATION = (".", "?", "!", "。", "！", "？", ";", "；", ":")


class ParagraphReconstructor:
    """Merge consecutive text blocks that continue the same paragraph."""

    def __init__(self, merge_blocks: bool = True):
        """Configure whether consecutive text blocks are merged."""
        self.merge_blocks = merge_blocks

    def merge(self, recovered_blocks: list[dict]) -> list[dict]:
        """Merge consecutive text blocks of the same paragraph."""
        if not self.merge_blocks:
            return list(recovered_blocks)

        merged: list[dict] = []
        for block in recovered_blocks:
            previous = merged[-1] if merged else None
            if previous and self._should_merge(previous, block):
                previous["text"] = previous["text"] + " " + block["text"]
                previous["merged_from"] = previous.get("merged_from", 1) + 1
                previous["bbox"] = [
                    min(previous["bbox"][0], block["bbox"][0]),
                    min(previous["bbox"][1], block["bbox"][1]),
                    max(previous["bbox"][2], block["bbox"][2]),
                    max(previous["bbox"][3], block["bbox"][3]),
                ]
                previous["evidence_ids"] = previous.get(
                    "evidence_ids", [previous.get("evidence_id")]
                ) + [block.get("evidence_id")]
                source_evidence_ids = block.get("source_evidence_ids")
                if isinstance(source_evidence_ids, list):
                    previous["source_evidence_ids"] = previous.get(
                        "source_evidence_ids", []
                    ) + source_evidence_ids
                continue
            merged.append(dict(block))
        return merged

    @staticmethod
    def _should_merge(previous: dict, current: dict) -> bool:
        # Tables and figures are structural atoms, never paragraph material.
        if previous.get("type") != "text" or current.get("type") != "text":
            return False
        if previous.get("page") != current.get("page"):
            return False

        previous_text = previous.get("text", "").strip()
        current_text = current.get("text", "").strip()
        if not previous_text or not current_text:
            return False

        if previous_text[-1] in _TERMINAL_PUNCTUATION:
            return False
        return current_text[0].islower()
