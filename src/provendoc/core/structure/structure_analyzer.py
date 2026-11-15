import re as _re

from provendoc.core.structure.document_tree import (
    DocumentNode,
    DocumentTree,
    NodeType,
)
from provendoc.core.structure.heading_patterns import (
    _ASCII_ART_CONTENT_RE,
)

# Leading section number of a markdown heading ("2." / "2.6" / "1.1.1." /
# "5 "): each segment <= 2 digits, optional trailing dot; a negative
# lookahead rejects truncated big numbers like "2026 年度报告" (backtracking
# to 1-2 digits still followed by a digit → no match).
_SECTION_NUMBER_RE = _re.compile(r"^(\d{1,2}(?:\.\d{1,2})*\.?)(?!\d)")


def _parse_section_number(raw):
    """Extract the leading section number from a raw markdown heading.

    "## 2. 架构设计" → "2";  "## 2.6 导出" → "2.6";
    "###2.5分块器" → "2.5";  "##5 监控" → "5";  no number → None.
    """
    content = raw.lstrip("#").lstrip()
    match = _SECTION_NUMBER_RE.match(content)
    if not match:
        return None
    return match.group(1).rstrip(".")



class _RawHeadingCandidate:
    """Minimal heading candidate used by the metadata/text fallback."""

    __slots__ = ("raw", "level", "depth_hint", "start", "end", "provenance", "pattern")

    def __init__(self, title, level, start, end=None, provenance="inferred", pattern="fallback"):
        """Build a candidate from title text, level, and char range."""
        self.raw = title
        self.level = level
        self.depth_hint = level
        self.start = start
        self.end = end if end is not None else start + len(title)
        self.provenance = provenance
        self.pattern = pattern


class StructureAnalyzer:
    """
        Responsibilities:

        ✅ hierarchy inference
        ✅ tree building
        ✅ parent-child structure
        ✅ content node creation (body/code/table/list/quote)
        ✅ depth_hint → level mapping (stable per-scan)

        ❌ semantic NLP
        ❌ chunking
        """

    # ========================================================
    # public
    # ========================================================

    def analyze(self, document):
        """Analyze the document and build a DocumentTree.

        Args:
            document: parsed document object (must have text; metadata may
                supply heading_candidates / special_modules).

        Returns:
            DocumentTree: the structure tree after hierarchy inference.
        """
        text = document.text
        metadata = getattr(document, "metadata", None) or {}
        heading_candidates = metadata.get("heading_candidates", [])
        special_modules = metadata.get("special_modules", [])

        # Backward-compatible fallback: raw heading dicts (level + title) or
        # plain "headings" lists come from hand-written fixtures and simple
        # parsers.  StructureAnalyzer infers the level from the title when it
        # matches Markdown/numbered heading syntax, mirroring the regex used
        # for content-node previews.  This keeps analyze() useful without a
        # dedicated heading-candidate extractor.
        if not heading_candidates:
            heading_candidates = self._heading_candidates_from_metadata(
                metadata, text
            )

        # Similarly, when no parser-provided special_modules exist, scan the
        # raw text for fenced code blocks, tables, lists, and block quotes so
        # StructureMap / MetadataEnricher can still see content types.
        if not special_modules:
            special_modules = self._special_modules_from_text(text)

        tree = DocumentTree()
        root = DocumentNode(
            node_id="root",
            node_type=NodeType.HEADING,
            level=0,
            title="root",
            text_start=0,
            text_end=len(text)
        )
        tree.set_root(root)
        stack = [root]
        # pattern family per stack entry, kept in sync with ``stack``
        # ("root" for the synthetic root).
        stack_families = ["root"]

        # depth_hint → level map (Bug L fix):
        # Ensures the same depth_hint always maps to the same
        # level within a single scan, preventing level drift
        # when headings skip intermediate depths.
        #
        # The map is keyed PER PATTERN FAMILY: a numeric depth_hint of 1
        # ("1. xxx") and a markdown depth_hint of 1 ("# xxx") must not
        # share a slot, otherwise whichever family is seen first hijacks
        # the level for the other (list-item-as-heading bug).
        depth_level_map: dict = {}

        # section number → resolved level ("2" → 2, "2.6" → 3):
        # dotted numbers inside markdown headings are a stronger level
        # signal than hash counts — AI output often inserts a "## 2.6"
        # (level jump) amid a "### 2.5" series; numbering anchoring pulls
        # "2.x" back under the common parent number "2" one level below.
        number_levels: dict = {}

        for idx,candidate in enumerate(heading_candidates):
            family = getattr(candidate, "pattern", "fallback")
            # Structure-evidence provenance (declared/inferred): parser
            # candidates carry their own (HeadingCandidate.provenance);
            # fallback candidates are graded by source in
            # _heading_candidates_from_metadata.
            # When uncertain, default conservatively to inferred —
            # guessed structure and declared structure must not flow
            # into the assessment pipeline as equals.
            provenance = getattr(
                candidate, "provenance", None
            ) or "inferred"
            # Level of the nearest markdown ancestor (0 if none): the
            # anchor for the numeric numbering system
            markdown_floor = 0
            for k in range(len(stack) - 1, -1, -1):
                if stack_families[k] == "markdown":
                    markdown_floor = stack[k].level
                    break
            # Numbering anchoring: when a markdown heading carries a
            # dotted number ("2.6") and its ancestor number ("2") is
            # registered, the level is decided by the numbering relation,
            # overriding the hash-count inference.
            # Single-segment number ("3") / no ancestor / no number →
            # keep the original depth_hint path.
            section_number = (
                _parse_section_number(candidate.raw)
                if family == "markdown" else None
            )
            anchored_level = self._numbering_anchored_level(
                section_number, number_levels
            )
            if anchored_level is not None:
                inferred_level = anchored_level
            else:
                inferred_level = self._infer_level(
                    stack, candidate, depth_level_map, family, markdown_floor
                )

            # Defense (list-item-as-heading bug): a non-markdown heading
            # may never escape its nearest markdown ancestor — it stays
            # nested at least one level below it.
            if family != "markdown":
                for k in range(len(stack) - 1, 0, -1):
                    if stack_families[k] == "markdown":
                        floor = stack[k].level + 1
                        if inferred_level < floor:
                            inferred_level = floor
                        break

            while len(stack) > 1 and stack[-1].level >= inferred_level:
                stack.pop()
                stack_families.pop()
            parent = stack[-1]
            node = DocumentNode(
                node_id=f"heading_{idx}",
                node_type=NodeType.HEADING,
                level=inferred_level,
                title=candidate.raw,
                text_start=candidate.start,
                text_end=candidate.end,
                parent_id=parent.node_id,
                metadata={
                    "structure_provenance": provenance,
                    "pattern_family": family,
                },
            )
            tree.add_node(node)
            stack.append(node)
            stack_families.append(family)
            # Register section number → final level, for anchoring later
            # descendant numbers like "2.6"/"2.6.1".
            # The final level after defensive unwinding is registered, so
            # anchors stay consistent with the tree.
            if section_number:
                number_levels[section_number] = inferred_level

        # ====================================================
        # content nodes (Bug J fix):
        # Create BODY / CODE / TABLE / LIST / QUOTE nodes
        # from special_modules so StructureMap and
        # MetadataEnricher can query content types.
        # ====================================================
        self._add_content_nodes(tree, text, special_modules, heading_candidates)

        return tree

    @staticmethod
    def _numbering_anchored_level(section_number, number_levels):
        """Anchored level for dotted numbers: the numbering relation takes
        priority over hash counts.

        For "2.6": find the nearest registered ancestor ("2.6" itself
        cannot be registered yet — try "2" in order);
        level = ancestor level + segment difference. E.g. under
        "2. 架构设计"@2, "## 2.6 导出" (hash-count level jump in the source)
        anchors to 3, aligned with the "### 2.5" series. Missing ancestor
        (incomplete document numbering) or single-segment number → None;
        the original depth_hint inference stands, no loss of strictness.
        """
        if not section_number or "." not in section_number:
            return None
        segments = section_number.split(".")
        for k in range(len(segments) - 1, 0, -1):
            prefix = ".".join(segments[:k])
            if prefix in number_levels:
                return max(2, min(6, number_levels[prefix] + (len(segments) - k)))
        return None

    def _infer_level(self, stack, candidate, depth_level_map, family="fallback", markdown_floor=0):
        """
        Infer the actual heading level from depth_hint.

        depth_hint != actual level — we infer relative hierarchy
        from local continuity while keeping the same depth_hint
        mapped to a stable level across the entire scan.

        depth_level_map is keyed by (family, depth_hint) so that hints
        from different pattern families never collide: a numeric
        depth_hint of 1 and a markdown depth_hint of 1 are independent.
        Within one family, if depth_hint=3 first resolves to level 2
        (due to skip), subsequent depth_hint=3 headings also get level 2
        instead of drifting to 3.

        Numeric-family exception: the depth_hint of a numbering scheme
        (1. / 1.1. / 1.1.1.) is in-section depth relative to the enclosing
        markdown section, not part of the global cache —
        level = markdown_floor + depth_hint (starting from 1 without a
        markdown ancestor).
        This nests "# 标题(1) → 1.(2) → 1.1.(3) → 1.1.1.(4)" naturally and
        avoids cross-section cache pollution.
        """
        depth_hint = candidate.depth_hint

        if family == "numeric":
            return max(1, markdown_floor + depth_hint)

        key = (family, depth_hint)

        # If we've seen this (family, depth_hint) before, reuse the level.
        if key in depth_level_map:
            return depth_level_map[key]

        current_level = stack[-1].level

        if depth_hint == current_level:
            resolved = current_level
        elif depth_hint > current_level:
            resolved = current_level + 1
        else:
            resolved = max(1, depth_hint)

        depth_level_map[key] = resolved
        return resolved

    # ========================================================
    # heading candidate fallback
    # ========================================================

    _HEADING_RE_FALLBACK = (
        r"^(#{1,6})\s+(.+)$"
    )
    _NUMBERED_RE_FALLBACK = (
        r"^(?:(\d+)[.\s]+|[A-Z][.\s]+)(\S.*)$"
    )
    _UPPER_RE_FALLBACK = r"^[A-Z][A-Z\s\-]{3,}$"

    @classmethod
    def _heading_candidates_from_metadata(cls, metadata, text):
        """Derive heading candidates from raw metadata or scanned text.

        Two supported sources, applied in order:

        1. ``heading_candidates``-style dicts lacking positional fields
           (``level`` and ``title`` or ``raw`` only).
        2. Plain ``headings`` lists produced by simple parsers.

        When neither exists, the document text itself is scanned for
        Markdown and numbered heading lines.  Candidates produced here carry
        positional bounds so the downstream tree builder can attach them
        without further parsing.
        """
        import re

        candidates = []
        seen_offsets = set()

        for raw_heading in metadata.get("heading_candidates", []) or []:
            if not isinstance(raw_heading, dict):
                continue
            if "start" in raw_heading and "depth_hint" in raw_heading:
                continue
            title = (
                raw_heading.get("raw")
                or raw_heading.get("title")
                or ""
            ).strip()
            level = int(raw_heading.get("level") or 1)
            offset = raw_heading.get("start")
            if offset is None:
                offset = cls._find_text_offset(text, title)
            if title and offset is not None:
                candidates.append(
                    _RawHeadingCandidate(
                        title, level, offset,
                        # parser/fixture heading dict — structure is
                        # explicitly declared upstream (markdown # / HTML
                        # tags / hand-written fixture); a dict carrying
                        # provenance wins.
                        provenance=raw_heading.get("provenance") or "declared",
                        pattern=raw_heading.get("pattern", "fallback"),
                    )
                )
                seen_offsets.add(offset)

        for plain_heading in metadata.get("headings", []) or []:
            if not isinstance(plain_heading, dict):
                continue
            title = (
                plain_heading.get("raw")
                or plain_heading.get("title")
                or ""
            ).strip()
            level = int(plain_heading.get("level") or 1)
            offset = plain_heading.get("start")
            if offset is None:
                offset = cls._find_text_offset(text, title)
            if title and offset is not None and offset not in seen_offsets:
                candidates.append(
                    _RawHeadingCandidate(
                        title, level, offset,
                        # plain headings lists come from the parser's
                        # explicit structure extraction
                        # (markdown # / HTML h1-h6) → declared.
                        provenance="declared",
                    )
                )
                seen_offsets.add(offset)

        if not candidates:
            for offset, line in enumerate(text.splitlines()):
                stripped = line.strip()
                if not stripped:
                    continue
                match = re.match(cls._HEADING_RE_FALLBACK, stripped)
                if match:
                    # ASCII-art guard (same rule as parser candidate
                    # extraction): symbol-only lines containing # (e.g.
                    # "#   #  #   #") are not headings; unblocked, fake
                    # headings would swallow body text line by line, and
                    # empty sections are dropped under untrusted fallback
                    # trees → content loss.
                    if (
                        "#" in match.group(2)
                        and _ASCII_ART_CONTENT_RE.fullmatch(match.group(2))
                    ):
                        continue
                    candidates.append(
                        _RawHeadingCandidate(
                            stripped, len(match.group(1)), offset,
                            # markdown # markers are explicit structure
                            # declarations → declared
                            provenance="declared", pattern="markdown",
                        )
                    )
                    continue
                match = re.match(cls._NUMBERED_RE_FALLBACK, stripped)
                if match:
                    # Body-line guard: lines starting with a number are
                    # mostly list items / data rows, not headings. Real
                    # numbered headings are short and lack sentence-final
                    # punctuation; any body-like trait disqualifies the
                    # line (it stays in section body, content preserved):
                    #   - overly long heading content (>50 chars):
                    #     sentence / data row
                    #     (韩非子 "6 存韓: 秦遂遣斯使韓也。" / invoice
                    #     lines)
                    #   - ends with a period: complete sentence
                    #     (drug-manual "1. 本品…患者。")
                    #   - contains a tab: TSV / tabular data row
                    #     ("1\tHello World\t…")
                    #   - contains >= 2 " | ": pipe-table data row
                    #     ("001 | zhangsan | … | 在职 | …")
                    content = match.group(2)
                    if (
                        len(content) > 50
                        or content.endswith(("。", ".", "．"))
                        or "\t" in line
                        or stripped.count(" | ") >= 2
                    ):
                        continue
                    # Letter branch ("A. Title") leaves group(1) unset (None),
                    # so guard before calling .isdigit() on it.
                    numbering = match.group(1)
                    depth = (
                        len(numbering)
                        if numbering and numbering.isdigit()
                        else 1
                    )
                    candidates.append(
                        _RawHeadingCandidate(
                            stripped, depth, offset,
                            # numbering heuristics are guessed structure →
                            # inferred
                            provenance="inferred", pattern="numeric",
                        )
                    )
                    continue
                if re.match(cls._UPPER_RE_FALLBACK, stripped):
                    candidates.append(
                        _RawHeadingCandidate(
                            stripped, 1, offset,
                            # the all-caps convention is guessed structure →
                            # inferred
                            provenance="inferred", pattern="caps",
                        )
                    )

        if not candidates:
            return []

        # Line offsets → char offsets, ordered by document position.  Skip
        # titles that could not be located in the text at all; without a real
        # position the tree would order nodes incorrectly.
        lines = text.splitlines()
        line_starts = []
        position = 0
        for line in lines:
            line_starts.append(position)
            position += len(line) + 1

        ordered = []
        for candidate in sorted(candidates, key=lambda item: item.start):
            if candidate.start < len(line_starts):
                char_start = line_starts[candidate.start]
            else:
                char_start = candidate.start
            ordered.append((char_start, candidate))

        # Each heading node spans only its own line — the same contract as
        # extract_heading_candidates.  Downstream (representation builder)
        # treats node.text_end as the START of the section body; spanning
        # to the next heading made every fallback section body empty, and
        # empty sections are dropped under untrusted fallback trees →
        # total content loss after the first fallback heading.
        resolved = []
        for char_start, candidate in ordered:
            if candidate.start < len(lines):
                char_end = char_start + len(lines[candidate.start])
            else:
                char_end = char_start + len(candidate.raw)
            resolved.append(
                _RawHeadingCandidate(
                    candidate.raw,
                    candidate.level,
                    char_start,
                    char_end,
                    provenance=candidate.provenance,
                    pattern=candidate.pattern,
                )
            )
        return resolved

    @staticmethod
    def _find_text_offset(text, title):
        if not title:
            return None
        index = text.find(title)
        if index == -1:
            return None
        # Translate the absolute char offset to a line offset so the
        # later line_starts conversion remains consistent.
        return text.count("\n", 0, index)

    @staticmethod
    def _special_modules_from_text(text):
        """Derive special_modules from raw text as a parser fallback.

        Scans line by line and records char-offset spans for fenced code
        blocks, tables, lists, and block quotes.  Only used when metadata
        carries no ``special_modules``; parser-provided spans always win.
        """
        fence_re = _re.compile(r"^\s*(```|~~~)")
        table_re = _re.compile(r"^\s*\|")
        list_re = _re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
        quote_re = _re.compile(r"^\s*>")

        lines = text.splitlines()
        line_starts = []
        position = 0
        for line in lines:
            line_starts.append(position)
            position += len(line) + 1

        def line_end(index):
            """End char offset of line index (0-based)."""
            return line_starts[index] + len(lines[index])

        modules = []
        idx = 0
        total = len(lines)
        while idx < total:
            line = lines[idx]
            if fence_re.match(line):
                start = line_starts[idx]
                idx += 1
                while idx < total and not fence_re.match(lines[idx]):
                    idx += 1
                end_index = min(idx, total - 1)
                modules.append(
                    {"type": "code", "start": start, "end": line_end(end_index)}
                )
                if idx < total:
                    idx += 1  # consume the closing fence
                continue

            block_type = None
            pattern = None
            if table_re.match(line):
                block_type, pattern = "table", table_re
            elif list_re.match(line):
                block_type, pattern = "list", list_re
            elif quote_re.match(line):
                block_type, pattern = "quote", quote_re
            if block_type is None:
                idx += 1
                continue

            start = line_starts[idx]
            while idx < total and pattern.match(lines[idx]):
                idx += 1
            modules.append(
                {"type": block_type, "start": start, "end": line_end(idx - 1)}
            )
        return modules

    # ========================================================
    # content node creation
    # ========================================================

    @staticmethod
    def _add_content_nodes(tree, text, special_modules, heading_candidates):
        """Create BODY / CODE / TABLE / LIST / QUOTE nodes.

        special_modules is a list of dicts with keys:
            type, start, end (char offsets in text).

        We create content nodes for each detected special
        module.  Text between headings that is NOT covered
        by any special module becomes implicit BODY content
        (we skip implicit body nodes here — the representation
        layer already handles unflagged text as narrative
        paragraphs).
        """
        for idx, sm in enumerate(special_modules):
            sm_type = sm.get("type", "body")
            try:
                node_type = NodeType(sm_type)
            except ValueError:
                node_type = NodeType.BODY

            start = sm.get("start", 0)
            end = sm.get("end", 0)
            if end <= start:
                continue

            # Find the nearest heading to use as parent.
            nearest = tree.find_nearest_heading(start)
            parent_id = nearest.node_id if nearest else "root"

            # Preview text (first ~60 chars, stripped)
            preview = text[start:end].strip()[:60]

            node = DocumentNode(
                node_id=f"content_{idx}",
                node_type=node_type,
                level=0,
                title=preview,
                text_start=start,
                text_end=end,
                parent_id=parent_id,
            )
            tree.add_node(node)




