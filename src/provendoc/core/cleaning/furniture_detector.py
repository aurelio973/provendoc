"""Structural detector for page furniture — cross-page periodic furniture
lines / bare page-number lines / TOC dot-leader lines.

Design positioning:
    - plain-text-line shape detection, explainable; no embedding /
      perplexity / latent metrics (continuing the cleaning_policy
      philosophy);
    - handles only high-confidence non-empty-line artifacts; blank
      regions remain the responsibility of SemanticCleaner's existing
      three-level policy — this detector never touches blank lines,
      and page furniture sits at the start/end of blank regions;
    - all decisions are emitted in the original line-number space and
      merged with the blank-region decisions (no duplicate decision
      per line: this detector takes precedence);
    - page-boundary anchors: normalization already turns ``\\x0c``
      into blank regions (PDF pipeline joins pages with ``\\n\\n``;
      docx joins paragraphs with ``\\n\\n``) — so a page boundary
      uniformly shows up as the start/end of a blank region.

Three levels of gating:
    - D1 cross-page periodic furniture lines: CONSERVATIVE/STANDARD
      need >=3 occurrences AND (anchor ratio >=2/3 OR periodicity
      holds); AGGRESSIVE removes at >=2 occurrences.
    - D2 bare page-number lines: CONSERVATIVE requires a page-boundary
      anchor; STANDARD/AGGRESSIVE remove on shape alone.
    - D3 TOC dot-leader lines: CONSERVATIVE/STANDARD annotate only
      (preserved); AGGRESSIVE removes.
"""

import re
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from provendoc.core.cleaning.cleaning_policy import (
    BlankLineSemantics,
    CleaningDecision,
    CleaningLevel,
)

# Single source of truth for the TOC dot-leader regex: heading_patterns
# is calibrated across the whole corpus (dot-leader candidates were 100%
# TOC lines, zero false positives); this detector reuses the same rules.
from provendoc.core.structure.heading_patterns import _TOC_LEADER_RE

# D1 extra gate: minimum line length (guards against short repeated
# refrain lines being misdetected).
_MIN_FURNITURE_LEN = 8

# D1 guard: single-character run lines ("=====" underline family) stay
# out — deleting repeated underline lines would destroy setext/RST
# structure.
_SINGLE_CHAR_RUN_RE = re.compile(r"^(.)\1+$")

# D1 guard: pipe table rows ("|...") stay out — deleting repeated
# headers/rows inside a table would destroy the table structure.
_PIPE_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

# D1 guard: ASCII table border rows (+-+ family) stay out — such
# borders are naturally periodic.
_ASCII_TABLE_BORDER_RE = re.compile(r"^[+\-=| ]+$")

# D1 guard: code fence markers (```/~~~ family) stay out — fences come
# in pairs with short periods; deleting them would destroy code blocks.
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

# D1 guard: ATX heading lines (# family) stay out — headings are the
# document skeleton; same-named section titles repeating across chapters
# is normal writing.
_ATX_HEADING_RE = re.compile(r"^#{1,6}(\s|$)")

# D1 span gate: hit lines' span ((last-first+1)/total lines) must reach
# this ratio — page furniture spans from the first to the last page;
# short-span periodic repetition is local repeated content (dialogue
# refrains, telegram-header clusters, license blocks).
_COVERAGE_MIN_RATIO = 0.5

# D1 cap on furniture groups: real headers/footers come in few distinct
# shapes; too many groups means the document itself is built from
# repeated content blocks, and removing them group by group destroys
# the text.
_MAX_FURNITURE_GROUPS = 3

# D2 page-number shapes (a line matching any of these after strip
# counts as a hit).
# Explicit shapes (unambiguous page-number forms): removable at
# all levels.
_EXPLICIT_PAGE_NUMBER_RES = (
    re.compile(r"^Page\s+\d{1,4}$", re.IGNORECASE),
    re.compile(r"^\[\s*Page\s+\d{1,4}\s*\]$", re.IGNORECASE),
    re.compile(r"^第\s*\d{1,4}\s*页$"),
    re.compile(r"^\d{1,4}\s*/\s*\d{1,4}$"),
    re.compile(r"^[-–—]\s*\d{1,4}\s*[-–—]$"),
)
# Bare digits carry no shape semantics and are indistinguishable from
# protocol status codes / numbered lists / poem ordinals on their own
# line — removed only at AGGRESSIVE level and only when anchored to a
# page boundary.
_BARE_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")

# D1 minimum gap period (median gap): real page furniture repeats once
# per page (dozens of lines); a smaller period indicates short-period
# repeated content blocks.
_MIN_PERIOD_GAP = 10


def _norm_line(line: str) -> str:
    """Normalize: strip + collapse inner whitespace (column-aligned
    headers may drift in alignment across pages)."""
    return " ".join(line.split())


def _page_anchor_indices(
    lines: List[str],
    blank_regions: List[Tuple[int, int]],
) -> Set[int]:
    """Set of page-anchor line indices: non-empty lines adjacent to the
    start/end of a blank region.

    Only blank-region-adjacent anchors count — the document's very
    first and last non-empty lines do **not** count: including them
    would let opening/closing refrains that happen to sit at the edges
    ride the 2/3 anchor ratio and get removed by mistake. A first-page
    header does not rely on the first-line anchor (anchors on later
    pages suffice for D1).
    """
    n = len(lines)
    anchors: Set[int] = set()
    for start, end in blank_regions:
        if start > 0:
            anchors.add(start - 1)  # 区域前的最后一个非空行（页脚位）
        if end < n:
            anchors.add(end)  # 区域后的第一个非空行（页眉位）
    return anchors


def detect_page_furniture(
    lines: List[str],
    blank_regions: List[Tuple[int, int]],
    level: CleaningLevel,
) -> Dict:
    """Detect page-furniture lines; returns {"decisions": [...], "stats": {...}}.

    All decision line_ranges point into the pre-cleaning original
    line-number space; at most one decision per line (priority
    D1 > D2 > D3).

    Args:
        lines: raw line list already split on "\\n".
        blank_regions: output of SemanticCleaner._find_blank_regions
            (half-open [start, end) intervals), used as page-anchor
            evidence.
        level: cleaning level (three-level gating per module docstring).
    """
    decisions: List[CleaningDecision] = []
    stats = {
        "furniture_lines_removed": 0,
        "page_number_lines_removed": 0,
        "toc_lines_annotated": 0,
        "toc_lines_removed": 0,
    }
    claimed: Set[int] = set()
    anchors = _page_anchor_indices(lines, blank_regions)
    aggressive = level == CleaningLevel.AGGRESSIVE

    # ---- D1 cross-page periodic furniture lines ----
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, line in enumerate(lines):
        norm = _norm_line(line)
        if norm:
            groups[norm].append(i)

    # (norm, idxs, occ, anchored, anchor_hit, periodic, coverage)
    furniture_groups: List[Tuple[str, List[int], int, int, bool, bool, float]] = []
    for norm, idxs in groups.items():
        if (
            len(norm) < _MIN_FURNITURE_LEN
            or _SINGLE_CHAR_RUN_RE.match(norm)
            or _PIPE_TABLE_ROW_RE.match(norm)
            or _ASCII_TABLE_BORDER_RE.match(norm)
            or _FENCE_RE.match(norm)
            or _ATX_HEADING_RE.match(norm)
        ):
            continue
        occ = len(idxs)
        anchored = sum(1 for i in idxs if i in anchors)
        anchor_hit = anchored * 3 >= occ * 2  # 页界锚比例 >= 2/3
        gaps = [b - a for a, b in zip(idxs, idxs[1:])]
        periodic = bool(gaps) and (max(gaps) - min(gaps) <= 2)
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0
        # Span: hit lines' first-to-last extent as a fraction of the
        # whole document (page furniture spans first to last page).
        coverage = (idxs[-1] - idxs[0] + 1) / len(lines)
        if aggressive:
            if occ < 2:
                continue
        else:
            # Four gates must hold **simultaneously**: anchor, periodicity,
            # span, minimum page height. In md/docx-style text where every
            # paragraph break is a blank region, almost every line counts
            # as "anchored" — the anchor alone would delete repeated
            # section titles; periodicity alone would delete local
            # short-period repetition (dialogue/telegram headers/license
            # blocks/repeated-content documents — the latter caught by
            # the minimum page-height gate).
            if occ < 3 or not (
                anchor_hit
                and periodic
                and coverage >= _COVERAGE_MIN_RATIO
                and median_gap >= _MIN_PERIOD_GAP
            ):
                continue
        furniture_groups.append((norm, idxs, occ, anchored, anchor_hit, periodic, coverage))

    # Furniture-group cap: too many groups means the document is built
    # from repeated content blocks — suppress entirely.
    if not aggressive and len(furniture_groups) > _MAX_FURNITURE_GROUPS:
        stats["furniture_groups_suppressed"] = len(furniture_groups)
        furniture_groups = []

    for norm, idxs, occ, anchored, anchor_hit, periodic, coverage in furniture_groups:
        confidence = "high" if (anchor_hit or periodic) else "medium"
        anchor_parts = []
        if anchor_hit:
            anchor_parts.append(f"页界锚 {anchored}/{occ}")
        if periodic:
            anchor_parts.append("行距周期成立")
        if coverage >= _COVERAGE_MIN_RATIO:
            anchor_parts.append(f"跨幅 {coverage:.2f}")
        reason = (
            f"跨页周期家具行：归一行出现 {occ} 次"
            f"（{'、'.join(anchor_parts) if anchor_parts else 'AGGRESSIVE 档无锚点放宽'}），"
            f"样例 {norm[:40]!r}"
        )
        for i in idxs:
            decisions.append(CleaningDecision(
                line_range=(i, i + 1),
                original_text=lines[i],
                decision="removed",
                semantics=BlankLineSemantics.PAGE_FURNITURE,
                reason=reason,
                confidence=confidence,
            ))
            claimed.add(i)
        stats["furniture_lines_removed"] += occ

    # ---- D2 bare page-number lines ----
    for i, line in enumerate(lines):
        if i in claimed:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        explicit = any(rx.search(stripped) for rx in _EXPLICIT_PAGE_NUMBER_RES)
        bare = _BARE_PAGE_NUMBER_RE.search(stripped) is not None
        if not (explicit or bare):
            continue
        if explicit:
            # Explicit shape: CONSERVATIVE requires a page anchor;
            # other levels remove outright.
            if level == CleaningLevel.CONSERVATIVE and i not in anchors:
                continue
        else:
            # Bare digits: removed only at AGGRESSIVE level and only
            # when page-anchored (anchored bare digits still hit poem
            # ordinals / status codes at default levels; no true
            # positives missed).
            if not aggressive or i not in anchors:
                continue
        decisions.append(CleaningDecision(
            line_range=(i, i + 1),
            original_text=line,
            decision="removed",
            semantics=BlankLineSemantics.PAGE_FURNITURE,
            reason="页码独占行：整行仅为页码构型，分页 artifact",
            confidence="high",
        ))
        claimed.add(i)
        stats["page_number_lines_removed"] += 1

    # ---- D3 TOC dot-leader lines ----
    for i, line in enumerate(lines):
        if i in claimed:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if not _TOC_LEADER_RE.search(stripped):
            continue
        if aggressive:
            decisions.append(CleaningDecision(
                line_range=(i, i + 1),
                original_text=line,
                decision="removed",
                semantics=BlankLineSemantics.PAGE_FURNITURE,
                reason="目录点导行（点导符+尾页码，全语料校准 100% 目录行）；AGGRESSIVE 档删除",
                confidence="high",
            ))
            stats["toc_lines_removed"] += 1
        else:
            decisions.append(CleaningDecision(
                line_range=(i, i + 1),
                original_text=line,
                decision="preserved",
                semantics=BlankLineSemantics.PAGE_FURNITURE,
                reason="目录点导行（点导符+尾页码，全语料校准 100% 目录行）；默认档不删，仅标注",
                confidence="high",
            ))
            stats["toc_lines_annotated"] += 1
        claimed.add(i)

    return {"decisions": decisions, "stats": stats}
