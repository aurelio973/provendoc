# ============================================================
# TXT REPRESENTATION
# ============================================================
"""
Representation responsibilities:

✅ base units
✅ coarse merge
✅ fine refinement
✅ fallback packaging
✅ preamble (text before first heading)
✅ no-heading paragraph fallback

❌ no structure recovery
"""


import re
from typing import List

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SourceContext,
    StructuralInfo,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation
from provendoc.core.representations.narrative.granularity import (
    CoarseGrainedConfig,
    FineGrainedConfig,
    MultiGranularitySplitter,
)
from provendoc.core.representations.narrative.sentence_boundaries import (
    find_sentence_boundary,
)
from provendoc.core.representations.narrative.unit_position_annotator import (
    annotate_unit_positions as _annotate_positions_impl,
)
from provendoc.core.representations.narrative.unit_position_annotator import (
    locate_unit as _locate_unit_impl,
)
from provendoc.core.strategies.representation_policy import RepresentationPolicy
from provendoc.core.structure.document_tree import NodeType
from provendoc.core.structure.heading_patterns import (
    _CODE_LINE_RE,
    _CODE_STMT_RE,
    _is_code_feature_line,
)
from provendoc.core.structure.special_modules import CODE_FENCE_RE
from provendoc.core.structure.structure_analyzer import StructureAnalyzer

# Unfenced bare-code-block features (extended forms reused from
# the heading_patterns guard heuristics):
#   - stack frames: "  at com.example..." lines in Java/Python tracebacks
#   - shebang: #!/bin/bash script first lines
#   - JS/TS: const/let/var declarations, function declarations,
#     arrow-opened blocks, call statements, closing-bracket lines
#   - SQL: uppercase statement heads (multi-line clause heads count too)
#   - shell: $VAR variables, redirects, && chains, control structures
#     (if..then/fi, for..do/done, case..in/esac)
# Deliberately excluded: leading-# comments (clash with txt topic
# lines), // and -- comments (prose dash-signature risk) — the
# ≥50% threshold lets other feature lines in the same paragraph
# carry them.
# Bare pipes | are deliberately excluded too: markdown table
# rows are also |-separated and would be hurt by it.
# Page furniture detection: short lines whose digit-normalized
# form recurs ≥ _PAGE_FURNITURE_REPEATS times across the
# document are pagination artifacts (headers like
# "RFC 2616 ... June 1999", footers like
# "Fielding, et al. [Page 12]"); each standing as its own
# paragraph/block produces hundreds of sub-100-char noise
# blocks. After detection they are merged into an adjacent
# prose paragraph (text preserved, line-level c4 integrity
# unchanged). Threshold 8: a short 3-4 page document never
# triggers (too few repeats, low confidence, low harm); a
# hundred-page document triggers on every page. Repeated short
# quote-marker lines in classical CJK texts are the same
# shape and are likewise merged into the following verse.
_DIGIT_NORM_RE = re.compile(r"\d+")
_PAGE_FURNITURE_REPEATS = 8
_PAGE_FURNITURE_MAX_LEN = 120

# Sentence-tail punctuation shape: furniture paragraphs ending
# with these marks are wrapped tails of the preceding sentence
# and merge into the previous paragraph; all others (headers,
# quote markers) merge into the following one.
_SENTENCE_TAIL_RE = re.compile(
    r"[。．.!？?…”』」)）]\s*$"
)


def _detect_page_furniture(text):
    """Detect the set of page-furniture signatures.

    Signature = line stripped with digits replaced by "#"
    (page numbers vary page by page; everything else in
    furniture does not). The line-length limit rules out
    coincidental repeats of prose paragraphs.
    """
    counts = {}
    for ln in text.splitlines():
        s = ln.strip()
        if not s or len(s) > _PAGE_FURNITURE_MAX_LEN:
            continue
        sig = _DIGIT_NORM_RE.sub("#", s)
        counts[sig] = counts.get(sig, 0) + 1
    return frozenset(
        sig for sig, n in counts.items()
        if n >= _PAGE_FURNITURE_REPEATS
    )


def _is_furniture_paragraph(para, furniture_sigs):
    """Whether the paragraph consists entirely of furniture lines.

    Fenced-code paragraphs never take part in merging (furniture
    lines inside a code block would break fence pairing); when a
    heading line shares the paragraph (heading_line merges into
    the section start), the paragraph necessarily contains
    non-furniture lines and misses by construction.
    """
    first = para.lstrip()
    if first.startswith(("```", "~~~")):
        return False
    lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
    if not lines:
        return False
    return all(
        len(ln) <= _PAGE_FURNITURE_MAX_LEN
        and _DIGIT_NORM_RE.sub("#", ln) in furniture_sigs
        for ln in lines
    )


def _merge_page_furniture(paragraphs, furniture_sigs):
    """Merge furniture lines that stand as their own paragraph
    into an adjacent prose paragraph.

    Direction rule: sentence-tail shapes (a lone sentence mark,
    "[Page 12]" footers, chapter-end formulas) merge into the
    previous paragraph (they are wrapped tails of the preceding
    sentence or page-end footers); everything else (headers,
    quote markers) merges into the following one. Text is kept
    verbatim and document order unchanged — only paragraph
    membership merges, so line-level c4 integrity is unaffected
    and the chunk-level noise blocks disappear.

    Returns: list[(text, has_furniture)]
    """
    if not furniture_sigs:
        return [(p, False) for p in paragraphs]
    out = []
    pending = []
    for para in paragraphs:
        if not _is_furniture_paragraph(para, furniture_sigs):
            if pending:
                out.append(("\n".join(pending + [para]), True))
                pending = []
            else:
                out.append((para, False))
            continue
        if _SENTENCE_TAIL_RE.search(para) and out:
            prev_text, _prev_flag = out[-1]
            out[-1] = (prev_text + "\n" + para, True)
        else:
            pending.append(para)
    if pending:
        if out:
            prev_text, _prev_flag = out[-1]
            out[-1] = (prev_text + "\n" + "\n".join(pending), True)
        else:
            out.extend((p, True) for p in pending)
    return out


_STACK_FRAME_RE = re.compile(r"^at\s+[\w.$]+\(.*\d+\)$")
_SHEBANG_RE = re.compile(r"^#!")
_JS_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:const\s+\w|let\s+\w|var\s+\w|function\b)"
)
_ARROW_OPEN_RE = re.compile(r"=>\s*\{$")
_CALL_STMT_RE = re.compile(r"^[A-Za-z_$][\w.$]*\([^;]*\)\s*[;,]?$")
_CLOSER_RE = re.compile(r"^[\}\)\]]+[;,]?\s*$")
_SQL_HEAD_RE = re.compile(
    r"^(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|WITH|FROM|WHERE|"
    r"GROUP\s+BY|ORDER\s+BY|LIMIT)\b"
)
_SHELL_FEATURE_RE = re.compile(r"\$[A-Za-z_{(]|\s[12]*>\s*\S|&&|^set\s+-")
_SHELL_CTRL_RE = re.compile(
    r"^(?:(?:if|for|while|until)\b.*;\s*(?:then|do)\s*$"
    r"|case\b.*\bin\s*$"
    r"|(?:fi|done|esac|then|else)\s*$)"
)

# Script detection (same shape as markdown_exporter): a line
# whose dominant script is CJK cannot be code; guard for bare
# code detection.
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _dominant_script(line: str):
    """Dominant script of the line: 'cjk' / 'latin' / None (balanced or no letters)."""
    cjk = len(_CJK_RE.findall(line))
    latin = len(_LATIN_RE.findall(line))
    if cjk > latin:
        return "cjk"
    if latin > cjk:
        return "latin"
    return None


def _bare_code_line_strength(line: str) -> int:
    """Feature strength of a bare code line.

    2 = strong feature (structural signal): statement heads,
        imports, assignments, declarations, calls, stack
        frames, shebangs, SQL heads, shell variables, etc.;
    1 = weak feature: indentation only (hanging-indented prose
        also matches; auxiliary evidence only);
    0 = no feature.
    """
    stripped = line.strip()
    if not stripped:
        return 0
    # CJK guard: a line whose dominant script is CJK cannot be
    # code — the shell redirect feature `\s[12]*>\s*\S` would
    # misread CJK comparison chains as redirects. Latin-dominant
    # lines are unaffected (mixed lines where `_dominant_script`
    # returns 'latin' or None go through feature detection as
    # usual).
    if _dominant_script(stripped) == "cjk":
        return 0
    if (
        _SHEBANG_RE.match(stripped)
        or _STACK_FRAME_RE.match(stripped)
        or _JS_DECL_RE.match(stripped)
        or _ARROW_OPEN_RE.search(stripped)
        or _CALL_STMT_RE.match(stripped)
        or _CLOSER_RE.match(stripped)
        or _SQL_HEAD_RE.match(stripped)
        or _SHELL_FEATURE_RE.search(stripped)
        or _SHELL_CTRL_RE.match(stripped)
    ):
        return 2
    if _CODE_STMT_RE.match(stripped) or _CODE_LINE_RE.match(stripped):
        return 2
    if _is_code_feature_line(line):
        return 1  # indentation-only — weak evidence
    return 0


def _looks_like_code_paragraph(para: str) -> bool:
    """Whether the paragraph is a bare code block.

    Double threshold: ≥50% of non-blank lines carry code
    features (strong or weak), and at least one line is a
    strong feature — hanging-indented legal prose and quoted
    text (weak features only) are not misjudged, while
    chaotically indented files stay rendered as prose.
    """
    lines = [ln for ln in para.split("\n") if ln.strip()]
    if not lines:
        return False
    strengths = [_bare_code_line_strength(ln) for ln in lines]
    hits = sum(1 for s in strengths if s > 0)
    strong = sum(1 for s in strengths if s == 2)
    return hits * 2 >= len(lines) and strong >= 1


# ASCII-art / character-drawing features (same path as bare
# code: once detected, wrap in a ``` fence):
#   Bare art lines get chewed up by markdown renderers —
#   "#..." lines parse as ATX headings, "|||" lines are
#   swallowed into tables, inter-word spaces collapse, and the
#   whole drawing collapses into one line. Inside a fence
#   every character renders verbatim.
#   Line level: art characters (# | \ / = _ ~ ^ + * o O @ -)
#   make up ≥ 0.7 of non-whitespace characters — the
#   denominator excludes spaces (art lines are mostly spaces;
#   an all-character ratio would never pass the threshold);
#   table content rows and prose rows are dominated by letters
#   and CJK and fall far short. Paragraph level: ≥2 non-blank
#   lines and ≥80% of them art lines — single "===="/"----"
#   separators and underlined headings never trigger; in a
#   markdown table paragraph only the "|---|" separator row is
#   pure art characters, so content rows drag the ratio below
#   the 80% threshold.
_ASCII_ART_CHARS = frozenset("#|\\/=~^+*oO@_-")

# Pure separator lines ("----------" / "==========" / "***",
# ≥3 repeats): horizontal rules / noise, not art lines —
# excluded from the art statistics entirely (neither numerator
# nor denominator), otherwise runs of separators would get
# wrongly fenced. "#" is not excluded: "#####..." canvas bottom
# lines count on it.
_SEPARATOR_LINE_RE = re.compile(r"^[-=*_]{3,}$")


def _ascii_art_line_ratio(line: str) -> float:
    """Ratio of art characters among non-whitespace characters in the line."""
    non_space = [ch for ch in line if not ch.isspace()]
    if not non_space:
        return 0.0
    hits = sum(1 for ch in non_space if ch in _ASCII_ART_CHARS)
    return hits / len(non_space)


def _looks_like_ascii_art_paragraph(para: str) -> bool:
    """Whether the paragraph is ASCII art / a character drawing.

    Double threshold: ≥2 non-blank non-separator lines, and
    ≥80% of them art lines (art-character ratio ≥0.7); pure
    separator lines count toward neither side (an
    all-separator paragraph → 0/0 → not art).
    """
    lines = [
        ln for ln in para.split("\n")
        if ln.strip() and not _SEPARATOR_LINE_RE.match(ln.strip())
    ]
    if len(lines) < 2:
        return False
    art_lines = sum(
        1 for ln in lines if _ascii_art_line_ratio(ln) >= 0.7
    )
    return art_lines * 5 >= len(lines) * 4


def _is_splittable_prose(para: str) -> bool:
    """Whether the paragraph can be split safely under the hard cap.

    Splitting a fenced structural block (wrapped code or
    character art) would break fence pairing (c1) —
    RuntimePackagingPolicy explicitly allows structural blocks
    to stay whole; splitting a paragraph containing pipe-table
    rows would cut one table-row paragraph into two chunks
    (c2) — table atomicity wins over the size cap (c2 primary
    > c5b auxiliary contract).

    Fence markers are detected on **any line** (same scope as
    contracts._FENCE_RE): page-furniture merging
    (_merge_page_furniture) can place furniture lines before
    a fence opener, so the paragraph no longer starts with ```
    — checking only the first line would admit paragraphs
    containing fenced regions and let an opener/closer land
    in separate chunks.
    """
    for ln in para.split("\n"):
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            return False
        if stripped.startswith("|"):
            return False
    return True


def _coalesce_tiny_units(units, floor, cap):
    """Coalesce small paragraphs/dialogue lines at base granularity.

    Without a minimum block size at the base layer,
    dialogue-heavy fiction produces one base unit per
    blank-line paragraph — swarms of sub-10-char fragment
    blocks (the source of c5c/c5a penalties downstream).

    Rules (same shape as the md-side chunk-layer orphan
    merge; floor reuses packaging.orphan_threshold, cap
    reuses size_soft_limit):
    - merge only adjacent units sharing a heading_path whose
      text is splittable prose — fenced code/table paragraphs
      (c1/c2 atomic blocks), size_split slices (hard-cap
      slices must not be glued back) and outline-only heading
      units are merge barriers;
    - a run whose accumulated length stays < floor keeps
      absorbing the next unit and closes once floor is
      reached (floor is a lower bound, not a target: large
      paragraphs are not packed by this pass);
    - the merged total length stays within cap (upstream c5b
      protection);
    - text is joined with "\n\n" and kept verbatim (c4 only
      penalizes loss); heading/structure/position facts are
      taken from the first member (same path, same section);
      metadata records coalesced provenance.

    Returns: a new unit list (the input list and unmerged
    units are not modified).
    """
    if floor <= 0 or len(units) <= 1:
        return units

    def _mergeable(unit):
        meta = unit.metadata or {}
        if unit.unit_type != UnitType.NARRATIVE:
            return False
        if (
            meta.get("size_split")
            or meta.get("outline_only")
            or meta.get("fallback_heading_line")
            # tex display-math blocks: math atoms must not be
            # absorbed into adjacent paragraphs — an extension
            # of the same merge-barrier family
            or meta.get("chunking") == "display_math"
        ):
            return False
        return _is_splittable_prose(unit.text or "")

    def _path(unit):
        return list(
            unit.heading_path
            or (unit.structural_info and unit.structural_info.heading_path)
            or []
        )

    result = []
    run = []  # current accumulating run (list of member units)
    run_len = 0

    def _close_run():
        nonlocal run, run_len
        if not run:
            return
        if len(run) == 1:
            result.append(run[0])
        else:
            first = run[0]
            merged_meta = dict(first.metadata or {})
            merged_meta["coalesced"] = True
            merged_meta["coalesced_members"] = len(run)
            merged_meta["coalesced_member_range"] = [
                run[0].sequence_index,
                run[-1].sequence_index,
            ]
            result.append(
                RepresentationUnit(
                    text="\n\n".join(u.text for u in run),
                    unit_type=UnitType.NARRATIVE,
                    granularity=first.granularity,
                    sequence_index=first.sequence_index,
                    heading=first.heading,
                    heading_path=list(first.heading_path or []),
                    structural_info=first.structural_info,
                    source_context=first.source_context,
                    semantic_primitives=first.semantic_primitives,
                    metadata=merged_meta,
                )
            )
        run = []
        run_len = 0

    for unit in units:
        if (
            run
            and run_len < floor
            and _mergeable(run[-1])
            and _mergeable(unit)
            and _path(unit) == _path(run[0])
            and run_len + 2 + len(unit.text) <= cap
        ):
            run.append(unit)
            run_len += 2 + len(unit.text)
            continue
        _close_run()
        run = [unit]
        run_len = len(unit.text)
    _close_run()
    return result


def _split_at_line_boundary(para: str, limit: int) -> List[str]:
    """Greedy split at line boundaries down to ≤ limit; a single
    line longer than limit becomes its own block.

    Never cut inside a line: c4 line-level integrity requires
    every non-blank source line to appear whole in some
    chunk, and an in-line cut would leave it incomplete in
    every chunk (c4 primary contract over the c5b auxiliary
    size contract).
    """
    parts: List[str] = []
    buf: List[str] = []
    buf_len = 0
    for line in para.split("\n"):
        add = len(line) + (1 if buf else 0)
        if buf and buf_len + add > limit:
            parts.append("\n".join(buf))
            buf = [line]
            buf_len = len(line)
        else:
            buf.append(line)
            buf_len += add
    if buf:
        parts.append("\n".join(buf))
    return parts


def _split_at_hard_fallback(text: str, limit: int) -> List[str]:
    """Final hard-cap fallback: \n\n → \n → sentence end → space →
    hard cut, never leaving an over-limit prose block.

    Line-boundary splitting is powerless against "single
    giant lines" (overlong lines with no newlines) — unsplit
    they stay over the limit forever and overflow downstream
    embedding / LLM windows. Here the c5b hard size contract
    outweighs c4 line-level integrity (in-line cutting), and
    slices record split_reason=hard_limit_fallback in
    metadata for provenance.
    In-line cut-point priority: when a sentence-end boundary
    exists within the window (sentence-final punctuation
    followed by whitespace), cut there — never mid-sentence;
    otherwise fall back to a space (never mid-word); then to
    a hard cut. A giant line without sentence punctuation
    degrades to the plain hard cut.
    The logic matches the coarse-layer
    CoarseGrainedSplitter._split_at_hard_max (sentence-boundary
    detection shares the sentence_boundaries module).
    """
    fragments: List[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        # Priority: \n\n/\n > sentence end > space > hard cut.
        # When a sentence end exists the space drops out of
        # the race — a later space near the window tail must
        # not jump past the sentence end and cut mid-sentence.
        sentence_cut = find_sentence_boundary(remaining, limit)
        if sentence_cut > 0:
            boundary = max(
                remaining.rfind("\n\n", 0, limit + 1),
                remaining.rfind("\n", 0, limit + 1),
                sentence_cut,
            )
        else:
            boundary = max(
                remaining.rfind("\n\n", 0, limit + 1),
                remaining.rfind("\n", 0, limit + 1),
                remaining.rfind(" ", 0, limit + 1),
            )
        if boundary <= 0:
            boundary = limit
        fragment = remaining[:boundary].strip()
        if fragment:
            fragments.append(fragment)
        remaining = remaining[boundary:].lstrip()
    if remaining:
        fragments.append(remaining)
    return fragments


def _split_with_furniture(
    para: str, limit: int, furniture_sigs
) -> List[str]:
    """Furniture-aware over-limit splitting: leading/trailing
    furniture lines never stand as their own piece.

    After furniture lines have been merged into prose
    paragraphs (c4), an over-limit paragraph split at line
    boundaries may drop its cut exactly between furniture
    lines or at a prose/furniture seam — trailing furniture
    lines end up isolated as a pure-furniture piece and the
    sub-100-char noise units come back. This wrapper first
    strips the leading/trailing furniture runs, splits the
    prose body at (limit - max(leading/trailing furniture
    length)), then glues the furniture lines back onto the
    two edge pieces — pieces stay ≤ limit (c5b) and
    furniture lines never stand alone.

    With no furniture signatures, or none at the paragraph
    edges, this degrades to a plain line-boundary split with
    unchanged behavior.
    """
    if not furniture_sigs:
        return _split_at_line_boundary(para, limit)

    def _is_furn_line(ln: str) -> bool:
        s = ln.strip()
        return (
            bool(s)
            and len(s) <= _PAGE_FURNITURE_MAX_LEN
            and _DIGIT_NORM_RE.sub("#", s) in furniture_sigs
        )

    lines = para.split("\n")
    head_end = 0
    while head_end < len(lines) and _is_furn_line(lines[head_end]):
        head_end += 1
    tail_start = len(lines)
    while tail_start > head_end and _is_furn_line(lines[tail_start - 1]):
        tail_start -= 1
    if head_end == 0 and tail_start == len(lines):
        return _split_at_line_boundary(para, limit)
    middle = lines[head_end:tail_start]
    if not middle:
        # Whole paragraph is furniture (nothing to attach to,
        # e.g. a document of purely repeated short lines):
        # plain line-boundary split — behavior matches the
        # no-furniture case; the hard-fallback path is not
        # entered.
        return _split_at_line_boundary(para, limit)

    head = "\n".join(lines[:head_end])
    tail = "\n".join(lines[tail_start:])
    eff_limit = limit - max(len(head), len(tail)) - 1  # room for the glue newline
    if eff_limit < 1:
        # The furniture run itself is near the limit (an
        # extreme shape under the 8-repeat threshold):
        # degrade to a plain split; size wins.
        return _split_at_line_boundary(para, limit)

    mid_pieces = _split_at_line_boundary("\n".join(middle), eff_limit)
    pieces = []
    last = len(mid_pieces) - 1
    for k, mp in enumerate(mid_pieces):
        piece = mp
        if k == 0 and head:
            piece = head + "\n" + mp
        if k == last and tail:
            piece = (piece + "\n" + tail) if piece else tail
        if piece:
            pieces.append(piece)
    return pieces


class TxtNarrativeRepresentation(BaseRepresentation):
    """Plain-text narrative representation — structure-tree driven section/paragraph units."""

    def __init__(
            self,
            strategy=None
    ):
        """Initialize the representation.

        Args:
            strategy: RepresentationPolicy or a compatible object
                (coerced automatically).
        """
        self.policy = RepresentationPolicy.coerce_to_policy(strategy)
        self.structure_analyzer = StructureAnalyzer()

    # ========================================================
    # public
    # ========================================================

    def build(self, document):
        """Build the whole document into narrative units from
        the structure tree.

        The tree comes from parser-provided heading candidates
        (trusted) or a fallback scan; the fallback tree can
        misjudge headings, so outline-only units are emitted
        only when the candidates are trusted.

        Args:
            document: Document (text is the cleaned working text).

        Returns:
            RepresentationResult: representation_type="narrative".
        """
        units = self._build_base_units(document)
        # Small-paragraph/dialogue coalescing: base-layer
        # minimum block size (c5c fragment defense). Must run
        # before _run_granularity — coarse sections by
        # heading_path, and merges that stay within a path
        # leave section convergence unaffected; fine splits
        # by sentence budget, so oversized merged units still
        # produce fine blocks as usual.
        units = _coalesce_tiny_units(
            units,
            floor=getattr(
                self.policy.packaging, "orphan_threshold", 120
            ),
            cap=getattr(
                self.policy.packaging, "size_soft_limit", 500
            ),
        )
        output = self._run_granularity(units)
        self._annotate_unit_positions(output.base, document)

        return RepresentationResult(
            representation_type="narrative",
            content=output,
            metadata={
                "base_units": len(output.base),
                "coarse_units": len(output.coarse),
                "fine_units": len(output.fine),
                "structure_runtime": True
            }
        )

    # ========================================================
    # subclass hooks (reused by pptx/epub container-aware representations)
    # ========================================================

    def _coarse_config(self):
        """Coarse-config hook: converted from policy by default;
        container-aware subclasses append representation-specific
        toggles such as container_locality here."""
        return CoarseGrainedConfig.from_policy(self.policy.coarse)

    def _run_granularity(self, units):
        """Three-granularity runtime hook: base units → MultiGranularityOutput."""
        splitter = MultiGranularitySplitter(
            coarse_config=self._coarse_config(),
            fine_config=FineGrainedConfig.from_policy(self.policy.fine),
            enable_fine_grained=self.policy.enable_fine_grained,
        )
        return splitter.process(units)

    def _build_base_units(self, document):
        """Build the whole document into base-granularity units
        from the structure tree (no granularity runtime).

        Same section/paragraph logic as build(), factored out
        so container-aware subclasses (pptx slide / epub
        chapter) build container-provenance base units
        with the same machinery.
        """
        tree = self.structure_analyzer.analyze(document)

        text = document.text
        units = []
        unit_index = 0

        # Does the tree come from parser-provided position
        # candidates (real pipeline) or a fallback scan? The
        # fallback tree can misread prose lines as headings
        # (empty sections are untrustworthy), so outline-only
        # units are emitted only when the candidates are
        # trusted.
        parser_candidates = bool(
            (document.metadata or {}).get("heading_candidates")
        )

        # Page-furniture signature set (one detection pass
        # over the whole text): repeated short lines standing
        # as their own paragraphs — headers/footers, quote
        # markers — are merged into adjacent prose
        # paragraphs, eliminating hundred-level noise blocks.
        furniture_sigs = _detect_page_furniture(text)

        # ====================================================
        # heading sections (exclude root: level=0)
        # ====================================================
        heading_nodes = [
            n
            for n in tree.nodes.values()
            if n.node_type == NodeType.HEADING and n.level > 0
        ]
        heading_nodes = sorted(
            heading_nodes,
            key=lambda n: n.text_start
        )

        src_ctx = SourceContext(
            file_name=document.metadata.get("file_name"),
            file_type=document.metadata.get("file_type"),
        )

        # ====================================================
        # preamble: text before first heading
        # ====================================================
        if heading_nodes and heading_nodes[0].text_start > 0:
            preamble_text = text[:heading_nodes[0].text_start].strip()
            if preamble_text:
                unit_index = self._add_paragraph_units(
                    preamble_text, units, unit_index,
                    heading="", heading_level=0,
                    heading_path=[],
                    src_ctx=src_ctx,
                    size_hard_limit=self.policy.packaging.size_hard_limit,
                    page_furniture=furniture_sigs,
                )

        # ====================================================
        # heading sections
        # ====================================================
        if heading_nodes:
            for idx, node in enumerate(heading_nodes):
                start = node.text_end
                if idx + 1 < len(heading_nodes):
                    end = heading_nodes[idx + 1].text_start
                else:
                    end = len(text)

                section_text = text[start:end].strip()

                heading_path = [
                    h.title for h in tree.get_heading_ancestors(node.node_id)
                    if h.level > 0
                ] + [node.title]

                if not section_text:
                    # Outline-only / transition heading (no
                    # body under the heading line): emit one
                    # outline_only unit so the hierarchy stays
                    # visible at both the chunk and markdown
                    # layers. text keeps the original heading
                    # line for the chunk layer; the markdown
                    # exporter renders outline_only units as
                    # the heading line only, no duplication.
                    # Trusted trees (parser-provided position
                    # candidates) only; under a fallback tree
                    # the empty section's "heading line" is
                    # very likely a misread prose line (numbered
                    # list item / all-caps line) — structurally
                    # untrustworthy, but the content must not
                    # be lost: emit it as an ordinary
                    # prose-paragraph unit (no heading, outside
                    # the hierarchy) rather than skipping
                    # (skipping = the line evaporates from the
                    # representation layer).
                    if parser_candidates:
                        units.append(
                            RepresentationUnit(
                                text=node.title,
                                unit_type=UnitType.NARRATIVE,
                                sequence_index=unit_index,
                                heading=node.title,
                                heading_path=list(heading_path),
                                structural_info=StructuralInfo(
                                    heading_level=node.level,
                                    heading_path=list(heading_path),
                                    parent_section=(
                                        heading_path[-1] if heading_path else None
                                    ),
                                ),
                                source_context=src_ctx,
                                metadata={
                                    "heading": node.title,
                                    "heading_level": node.level,
                                    "outline_only": True,
                                    "structure_preserved": True,
                                    # Structure evidence source
                                    # (declared/inferred): whether
                                    # the heading identity is the
                                    # author's own or a heuristic
                                    # guess.
                                    "structure_provenance": node.metadata.get(
                                        "structure_provenance"
                                    ),
                                },
                            )
                        )
                    else:
                        # Fallback tree misread a prose line as
                        # a heading: the content must not be
                        # lost, so emit it as an ordinary prose
                        # unit; the hard size cap still
                        # applies — a misread giant line goes
                        # through the same fallback.
                        line_limit = self.policy.packaging.size_hard_limit
                        line_pieces = [node.title]
                        if line_limit > 0 and len(node.title) > line_limit:
                            line_pieces = _split_at_hard_fallback(
                                node.title, line_limit
                            )
                        for piece in line_pieces:
                            piece_metadata = {
                                "heading": "",
                                "heading_level": 0,
                                "fallback_heading_line": True,
                                "structure_preserved": False,
                            }
                            if len(line_pieces) > 1:
                                piece_metadata["size_split"] = True
                                piece_metadata["split_reason"] = (
                                    "hard_limit_fallback"
                                )
                            units.append(
                                RepresentationUnit(
                                    text=piece,
                                    unit_type=UnitType.NARRATIVE,
                                    sequence_index=unit_index,
                                    heading="",
                                    heading_path=[],
                                    structural_info=StructuralInfo(
                                        heading_level=0,
                                        heading_path=[],
                                        parent_section=None,
                                    ),
                                    source_context=src_ctx,
                                    metadata=piece_metadata,
                                )
                            )
                            unit_index += 1
                        continue
                    unit_index += 1
                    continue

                # The heading line itself is document content —
                # merge it into the section start so it enters
                # the chunk text stream with the body (joined
                # to the first paragraph by a single newline,
                # not as a separate tiny unit, to avoid new
                # c5c small blocks). Previously the section
                # body started at node.text_end, leaving the
                # heading line alive only in unit metadata —
                # whole-line evaporation at the chunk text
                # level, the main cause of c4 line-integrity
                # violations. Falls back to node.title when
                # the span looks off.
                heading_line = (
                    text[node.text_start:node.text_end].strip()
                    or node.title
                )
                if heading_line:
                    section_text = heading_line + "\n" + section_text

                unit_index = self._add_paragraph_units(
                    section_text, units, unit_index,
                    heading=node.title,
                    heading_level=node.level,
                    heading_path=heading_path,
                    src_ctx=src_ctx,
                    structure_provenance=node.metadata.get(
                        "structure_provenance"
                    ),
                    size_hard_limit=self.policy.packaging.size_hard_limit,
                    page_furniture=furniture_sigs,
                )

        # ====================================================
        # no-heading fallback: entire document as paragraphs
        # ====================================================
        if not units and text.strip():
            unit_index = self._add_paragraph_units(
                text.strip(), units, unit_index,
                heading="", heading_level=0,
                heading_path=[],
                src_ctx=src_ctx,
                size_hard_limit=self.policy.packaging.size_hard_limit,
                page_furniture=furniture_sigs,
            )

        return units

    # ========================================================
    # private
    # ========================================================

    def _annotate_unit_positions(self, units, document):
        """Locate each base unit in document.text and stamp positions.

        Every unit carries line/char/byte positions (basis
        ``document_text_utf8``), the source file sha256, and a
        continuation flag for size-split pieces. The algorithm
        lives in a shared module; this method is a thin
        delegation with unchanged behavior — contract and
        coordinate conventions see
        unit_position_annotator.annotate_unit_positions.
        """
        _annotate_positions_impl(units, document)

    @staticmethod
    def _locate_unit(unit, lines, cursor):
        """Find the unit's (start_line, end_line) 0-based span, or None.

        Thin delegation to the shared module (single source of
        truth for the algorithm); the contract is unchanged.
        """
        return _locate_unit_impl(unit, lines, cursor)

    @staticmethod
    def _add_paragraph_units(
        section_text,
        units,
        unit_index,
        heading,
        heading_level,
        heading_path,
        src_ctx,
        structure_provenance=None,
        size_hard_limit=0,
        extra_metadata=None,
        page_furniture=None,
    ):
        """Split section text into paragraphs and append as RepresentationUnits.

        Fence-aware: blank lines inside a fenced code block
        (```/~~~ pairs) are part of the code structure, not
        paragraph boundaries — the whole block is one unit, so
        docstring blank lines cannot shatter the block (each
        shard would have an odd fence count, and the export
        layer's closing fences would spray stray ```).

        Unclosed fences: close at the first blank line inside
        the block (or the section end if none) — an unclosed
        code fragment is usually short, and swallowing to the
        section end would bury the following prose body inside
        a code block; after truncating, the export layer adds
        the closing fence.

        Bare-code detection: a paragraph with no fences but
        ≥50% lines carrying code features (block statement
        heads, imports, assignments, indented bodies, stack
        frames, shebangs) is treated as code; adjacent code
        paragraphs merge across blank lines into one unit and
        get ``` fences — the markdown layer renders a code
        block and the chunk layer keeps the code atomic.

        ASCII-art detection: drawing paragraphs (≥2 lines and
        ≥80% art lines) go through the same path as bare code
        and get ``` fences — bare art lines would be chewed
        up by renderers (leading # into headings, ||| into
        tables) while inside a fence they render verbatim.

        Page-furniture merging: when page_furniture (the
        signature set detected over the whole text by
        _build_base_units) is provided, furniture lines
        standing as their own paragraph merge into adjacent
        prose paragraphs (direction and text-preservation
        contract see _merge_page_furniture).

        Returns the next unit_index.
        """
        lines = section_text.split("\n")
        fence_positions = [
            i for i, ln in enumerate(lines)
            if CODE_FENCE_RE.match(ln.strip())
        ]
        next_fence = {
            fence_positions[k]: fence_positions[k + 1]
            for k in range(len(fence_positions) - 1)
        }
        paragraphs = []
        buf = []
        code_lines = []  # bare-code accumulator; non-empty = an unclosed code unit
        n = len(lines)

        def _close_code():
            """Flush the accumulated bare-code unit (wrapped in ``` fences) and close the accumulator."""
            if code_lines:
                paragraphs.append("```\n" + "\n".join(code_lines) + "\n```")
                code_lines.clear()

        def _flush():
            if buf:
                para = "\n".join(buf).strip()
                buf.clear()
                if not para:
                    return
                if (
                    _looks_like_code_paragraph(para)
                    or _looks_like_ascii_art_paragraph(para)
                ):
                    # Bare code / character art: append to the
                    # accumulator (adjacent code paragraphs merge
                    # across blank lines into one unit; blank
                    # lines between def blocks are code layout,
                    # not semantic boundaries; a blank line is
                    # re-inserted on merge to keep the vertical
                    # layout)
                    if code_lines:
                        code_lines.append("")
                    code_lines.extend(para.split("\n"))
                else:
                    _close_code()
                    paragraphs.append(para)

        i = 0
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if CODE_FENCE_RE.match(stripped):
                if i in next_fence:
                    # Paired close: the whole block (including internal blank lines) is one unit
                    close_idx = next_fence[i]
                    _flush()
                    _close_code()
                    paragraphs.append(
                        "\n".join(lines[i : close_idx + 1]).strip()
                    )
                    i = close_idx + 1
                    continue
                # Unclosed: truncate at the first blank line inside the block (or the section end)
                _flush()
                _close_code()
                j = i + 1
                while j < n and lines[j].strip():
                    j += 1
                paragraphs.append("\n".join(lines[i:j]).strip())
                i = j
                continue
            if not stripped:
                _flush()
            else:
                buf.append(line)
            i += 1
        _flush()
        _close_code()

        # Merge header/footer and repeated-short-line paragraphs into adjacent prose (text kept verbatim)
        paragraphs = _merge_page_furniture(paragraphs, page_furniture)

        for p, furniture_merged in paragraphs:
            # Base-layer size enforcement: prose paragraphs
            # over the hard limit split at line boundaries
            # (c5b); fenced structural blocks and
            # table-containing paragraphs stay atomic
            # (c1/c2 first).
            oversize = size_hard_limit > 0 and len(p) > size_hard_limit
            if oversize and _is_splittable_prose(p):
                pieces = []
                hard_fallback = False
                # Furniture-aware splitting: merged-in furniture
                # lines must not be isolated into pure-furniture
                # pieces by wherever the cut lands (defense
                # against sub-100-char noise units returning).
                for piece in _split_with_furniture(
                    p, size_hard_limit, page_furniture
                ):
                    if len(piece) > size_hard_limit:
                        # Single giant line: line-boundary splitting can't help; final hard-cap fallback forces a cut
                        pieces.extend(
                            _split_at_hard_fallback(piece, size_hard_limit)
                        )
                        hard_fallback = True
                    else:
                        pieces.append(piece)
            else:
                pieces = [p]
                hard_fallback = False
            for piece in pieces:
                unit_metadata = {
                    "heading": heading,
                    "heading_level": heading_level,
                    "structure_preserved": True,
                }
                if extra_metadata:
                    # Caller-attached facts such as container
                    # provenance (slide_index/chapter_index)
                    # are carried per unit as-is.
                    unit_metadata.update(extra_metadata)
                if len(pieces) > 1:
                    # Size-degradation split provenance — this
                    # unit is a line-boundary slice of an
                    # over-limit prose paragraph, not a
                    # semantic paragraph boundary.
                    unit_metadata["size_split"] = True
                    if hard_fallback:
                        unit_metadata["split_reason"] = "hard_limit_fallback"
                if oversize and not _is_splittable_prose(p):
                    # Integrity blocks (fenced code/tables) are not cut; the over-limit fact is recorded honestly.
                    unit_metadata["oversized"] = True
                    unit_metadata["oversized_reason"] = (
                        "structural_block exceeds hard_limit"
                    )
                if structure_provenance is not None:
                    # Evidence source of the section heading
                    # (declared/inherited) — quality scoring and
                    # RSE downstream use it to tell declared
                    # structure from guessed structure.
                    unit_metadata["structure_provenance"] = structure_provenance
                if furniture_merged:
                    # This unit absorbed a header/footer or
                    # repeated-short-line paragraph that stood
                    # alone — provenance marker for quality
                    # scoring and test assertions.
                    unit_metadata["page_furniture_merged"] = True
                units.append(
                    RepresentationUnit(
                        text=piece,
                        unit_type=UnitType.NARRATIVE,
                        sequence_index=unit_index,
                        heading=heading,
                        heading_path=list(heading_path),
                        structural_info=StructuralInfo(
                            heading_level=heading_level,
                            heading_path=list(heading_path),
                            parent_section=(
                                heading_path[-1] if heading_path else None
                            ),
                        ),
                        source_context=src_ctx,
                        metadata=unit_metadata,
                    )
                )
                unit_index += 1
        return unit_index
