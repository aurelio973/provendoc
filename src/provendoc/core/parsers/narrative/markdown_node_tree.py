# ============================================================
# Narrative Domain
# Markdown unified node tree — P0 (line-level splitter) + P1 (inline)
# ============================================================
r"""Markdown unified node tree — P0 line-level segmenter + P1 inline elements (b3-3a).

Basis: ``markdown_unified_nodes_p0_prereg_v1.md`` (D-P1–D-P6 signed off, all A),
``markdown_p1_repository_prereg_v1.md`` (b3-3, D-R1–D-R7 signed off, all A)
and the master draft ``markdown_unified_nodes_prereg_v1.md`` (D-U1–D-U5 signed off, all A).

Positioning discipline (master-draft sign-off items):

- existing chunk output changes zero — this module only adds ``metadata["markdown_nodes"]`` (additive);
- a markdown-specific tree; the six DocumentTree primitive roles stay untouched (D-U2);
- zero consumption at the chunk layer (D-U5: P0 does no chunk back-linking).

Node schema (D-P2/D-P3): flat ``node_id``/``parent_id``; ``source_ref``
``{line_start, line_end}`` 1-based closed interval; ``raw_span`` char/byte half-open interval
(``positions_basis="document_text_utf8"``, same basis as the F8 position stamps);
``resolution_status`` (complete|partial) + ``reason_code``;
``evidence_type`` (constructed|facts_registry|shared_extractor|native_markdown);
``type_facts`` small fields — no raw_text/normalized_content/confidence carried (D-P4).

Segmenter (13 categories after the D-P5 baseline correction): per-line category decision + consecutive-line grouping.

- range categories (from facts/shared extractors, whole segment per block): front_matter / code_block /
  table / merge_conflict;
- line categories (one block per line): heading / directive / footnote;
- group categories (native regex scan, blank-line-separated consecutive-line groups): list(+list_item) / quote /
  paragraph;
- the document root node is always built (node_id=md-n0000, covering the whole document).

Priority: fm > fence > table > conflict > heading > list > quote >
paragraph > directive > footnote (prereg §2.1; implementation postscript: directive/footnote
are independent line categories, not folded into paragraph — see the prereg §8 postscript). Lines hit by multiple categories go into
``priority_conflicts``, higher priority wins; lines already claimed by fm/fence/table/conflict no longer
run the list/quote regexes (code content inside fences gets no semantic matching, preventing conflict-count noise).

Cap: non-root nodes ``_NODE_LIMIT``=2000 (corpus-measured max 1443); beyond it, truncation follows document order
and sets ``nodes_truncated=True``; a list_item always sorts after its parent list, so truncation
never produces orphan parent references.

Honest disclosure of facts location-list truncation: when the relevant facts carry a ``*_truncated`` flag, register
``truncated_fact_sources`` (the affected lines may be under-classified as paragraph — a known limitation).

P1 inline elements (b3-3a): link / image / diagram, three kinds (D-R2: html_block moved
to P2). The scan domain is same-source with the inline census (outside the fenced ∪ front matter exclusion domain),
per-target registration, zero changes to existing census keys (prereg §2.1). Target semantic classification
(D-R4): http(s)/mailto → complete; javascript:/data: → partial +
UNSAFE_PROTOCOL; no scheme (local relative path/anchor) → unresolved +
NO_REPOSITORY_CONTEXT (an upgrade path reserved for batch 3-C repository injection). Reference-form
targets register the label only, never expand it (D-R3); an inline node's parent_id points at the dominant block node covering that line
(D-R5: a link inside a list_item hangs off the item). Inline hits are registered in order, skipping overlaps
with already-registered spans (the ``<img>`` whole tag claims first, the ref-definition target precedes
bare/autolink — preventing double registration of img src and definition-line URLs). diagram: fence_info
first-word whitelist {mermaid, plantuml, graphviz, dot} upgrades (D-R2); all other
fences stay code_block.

P2 six kinds (b3-4, ``markdown_p2_nodes_prereg_v1.md`` D-Q1–D-Q9 all A):

- **block-level**: ``$$`` display equation blocks (boundary lines reclassified as equation, lines inside the range keep
  their existing classification — the range does not swallow; unclosed registers only the opener line + EQUATION_UNCLOSED,
  D-Q2); ``:::`` container boundary lines (D-Q5: only open/close boundary nodes are registered; engine
  word whitelist → component(kind=open/close), the MyST ``{word}`` shape is compatible;
  out-of-whitelist engine words or an orphan ``:::`` → unknown_extension +
  UNKNOWN_MARKDOWN_EXTENSION, the blueprint §4 unsupported contract); line-initial
  lowercase HTML tag consecutive lines run-merge into html_block (D-Q6; the ``<!--`` /
  ``<img`` / autolink domains excluded, uppercase tags belong to the JSX domain and are left alone); an opener that never gets a
  close (displaced by a second opener or end of file) → the known-admonition open node degrades to
  partial + CONTAINER_UNCLOSED (closing patch, symmetric with EQUATION_UNCLOSED;
  unknown_extension keeps its original unsupported verdict, never rewritten).
- **inline**: pandoc ``[@key]`` citation (D-Q1; the ``[@x][]`` collapsed ref
  and the ``[@x]:`` definition line go to the link domain first, excluded by lookaround); ``$…$`` paired-adjacent
  inline equation (D-Q3 currency guard; ``\[…\]``/``\(…\)`` are not probed — corpus-verified
  as fully escaped, prefer missing them); JSX only when ext_profile==mdx (D-Q7,
  the ``syntax_profile`` facts as single source; uppercase tags in the ``.md`` family are left alone — corpus-verified
  11/12 false positives); template expressions node-ified as spans → unresolved +
  UNKNOWN_TEMPLATE_ENGINE (D-Q8, regex single-source imported from facts, kind aligned with the
  census sub-keys).
- **scan order**: template (dollar_var etc.) before the inline equation — the ``${x}``
  and ``$…$`` collision domain, the more specific dollar_var claims first; citation/JSX have no collisions.
- html_block lines still run the inline scan (P1 behaviour zero-drift first; inline nodes overlapping block
  nodes is the existing D-R5 precedent).
"""

import bisect
import hashlib
import re

from provendoc.core.text.markdown_blocks import extract_fenced_code_blocks

from .markdown_structure_facts import (
    _RE_AUTOLINK_MAIL,
    _RE_AUTOLINK_URL,
    _RE_BARE_URL,
    _RE_INLINE_IMAGE,
    _RE_INLINE_LINK,
    _RE_REF_DEF,
    _RE_REF_IMAGE,
    _RE_REF_LINK,
    _RE_TASK_ITEM,
    _RE_TMPL_DOLLAR,
    _RE_TMPL_DOUBLE,
    _RE_TMPL_ERB,
    _RE_TMPL_PERCENT,
)

__all__ = ["derive_markdown_nodes", "NODE_LIMIT"]

# ── Constants ──────────────────────────────────────────
NODE_LIMIT = 2000                  # non-root node cap (D-P5)
_POSITIONS_BASIS = "document_text_utf8"  # same basis as the F8 position stamps

# Line categories (one-to-one with node_type, except BLANK which is a separator and yields no node)
_FM = "front_matter"
_FENCE = "code_block"
_TABLE = "table"
_CONFLICT = "merge_conflict"
_HEADING = "heading"
_DIRECTIVE = "directive"
_FOOTNOTE = "footnote"
_LIST = "list"
_QUOTE = "quote"
_PLAIN = "paragraph"
_BLANK = "blank"                    # internal classification only, produces no node

# P2 (b3-4) block-level line categories
_EQUATION = "equation"              # $$ display-block boundary line
_CONTAINER = "component"            # ::: container boundary line (admonition whitelist)
_UNKNOWN_EXT = "unknown_extension"  # out-of-whitelist engine word / orphan :::
_HTML = "html_block"                # line-initial lowercase HTML tag consecutive lines

_INTERVAL_CATS = frozenset({_FM, _FENCE, _TABLE, _CONFLICT})
_RUN_CATS = frozenset({_LIST, _QUOTE, _PLAIN, _HTML})

# native line-level recognition (group categories; no existing facts single source — new territory, this module is the single source)
_RE_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+")
_RE_QUOTE_LINE = re.compile(r"^[ \t]*>")

# P1 inline (b3-3a): <img> whole tag + src capture (the census _RE_HTML_IMG only counts,
# the counting key stays untouched; src capture is a new capability of the inline probe)
_RE_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_RE_IMG_SRC = re.compile(r"(?<![-\w])src\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)

_DIAGRAM_ENGINES = frozenset({"mermaid", "plantuml", "graphviz", "dot"})

# Target block types for inline-node parent attachment (D-R5; P2 block-level categories included)
_BLOCK_PARENT_TYPES = frozenset({
    _HEADING, _DIRECTIVE, _FOOTNOTE, _LIST, "list_item", _QUOTE, _PLAIN,
    _EQUATION, _CONTAINER, _UNKNOWN_EXT, _HTML,
})

# ── P2 (b3-4) block-level regexes (native new territory, this module the single source) ────────────
_RE_EQ_BLOCK = re.compile(r"^\s*\$\$")
_RE_COLON_LINE = re.compile(r"^\s*:{3,}")
_RE_COLON_WORD = re.compile(r"^\s*:{3,}\s*\{?\s*([A-Za-z][\w-]*)")
_RE_HTML_LINE = re.compile(r"^\s*</?([a-z][a-z0-9-]*)\b")

# admonition container engine-word whitelist (D-Q5; Docusaurus admonitions and MyST/RST
# directives share the domain; the brace shape ``:::{note}`` is handled by _RE_COLON_WORD)
_ADMONITION_ENGINES = frozenset({
    "note", "tip", "info", "warning", "danger", "caution",
    "attention", "hint", "success", "error",
})
# html_block domain exclusion (existing domains claim first): <!-- goes to the directive/body domain, <img goes to
# P1 html_img, <http(s)/<mailto goes to autolink
_HTML_LINE_EXCLUDES = ("<!--", "<img", "</img", "<http", "<mailto")

# ── P2 (b3-4) inline regexes ──────────────────────────
# pandoc citation [@key]: lookaround excludes the link-domain shapes — [@x][] (collapsed
# reference) and [@x]: (definition line) go to link; ![@x (image) excluded forward
_RE_CITATION = re.compile(r"(?<!!)\[@([^\]\s]+)\](?!\[)(?!\s*:)")
# inline equation $…$: paired and adjacent (neither side touches whitespace), the close side touches no digit, neither side belongs to
# $$ double-dollar (D-Q3 currency guard; \[…\]/\(…\) not probed — D-Q2 corpus-verified)
_RE_EQ_INLINE = re.compile(
    r"(?<![\w\\$])\$(?!\s)(?!\$)([^$\n]+?)(?<![\s\\$])\$(?!\$)(?!\d)"
)
# JSX tag span (only ext_profile==mdx, D-Q7; uppercase tags in the .md family left alone)
_RE_JSX_TAG = re.compile(r"<([A-Z][A-Za-z0-9]*)\b[^<>]*>")


def _classify_target(raw, *, is_mail=False):
    """Target semantic classification (D-R4): returns (status, reason_code)."""
    low = raw.strip().lower()
    if low.startswith(("javascript:", "data:")):
        return "partial", "UNSAFE_PROTOCOL"
    if low.startswith(("http://", "https://", "mailto:")):
        return "complete", None
    if is_mail:
        return "complete", None          # <a@b.com> autolink email
    return "unresolved", "NO_REPOSITORY_CONTEXT"


def _collect_inline_hits(body, flags, jsx_enabled=False):
    """Inline per-target probing (b3-3a + b3-4): line-by-line scan outside the fenced ∪ fm exclusion domain.

    Returns [dict(line, start, end, node_type, kind, status, reason, facts)];
    start/end are inline offsets. Overlap dedup: each line keeps a list of registered spans; a hit overlapping one is
    skipped; scan order html_img(whole tag claims first) > definition(target claims first)
    > inline image > inline link > ref image > ref link > autolink >
    bare URL > citation > jsx > template(dollar/double/percent/erb) >
    inline equation (template before equation: ``${x}`` is more specific, claims first, D-Q8/postscript).
    html_block lines still run the inline scan (P1 behaviour kept as-is — zero drift before domain
    purity; inline nodes overlapping block nodes is the existing precedent).
    """
    hits = []
    for ln in range(1, len(body) + 1):
        f = flags[ln]
        if _FM in f or _FENCE in f:
            continue                       # same-source exclusion domain as the inline census
        line = body[ln - 1]
        taken = []

        def _overlaps(s, e):
            return any(s < te and ts < e for ts, te in taken)

        def _hit_raw(s, e, node_type, kind, status, reason, facts=None):
            if _overlaps(s, e):
                return
            taken.append((s, e))
            hits.append({
                "line": ln, "start": s, "end": e,
                "node_type": node_type, "kind": kind,
                "status": status, "reason": reason,
                "facts": facts or {},
            })

        def _hit(s, e, node_type, kind, target, *, is_mail=False):
            status, reason = _classify_target(target, is_mail=is_mail)
            _hit_raw(s, e, node_type, kind, status, reason,
                     {"target_raw": target})

        for m in _RE_IMG_TAG.finditer(line):       # whole tag claims first (URLs inside attributes are not bare-registered)
            s, e = m.span()
            if _overlaps(s, e):
                continue
            taken.append((s, e))
            src = _RE_IMG_SRC.search(m.group(0))
            if src:
                # span already claimed — goes straight into hits, cannot go through _hit_raw (it would be caught by the self-check)
                status, reason = _classify_target(src.group(1))
                hits.append({
                    "line": ln, "start": s, "end": e,
                    "node_type": "image", "kind": "html_img",
                    "status": status, "reason": reason,
                    "facts": {"target_raw": src.group(1)},
                })

        d = _RE_REF_DEF.match(line)                 # definition-line target claims first
        if d:
            _hit(d.start(2), d.end(2), "link", "definition", d.group(2))

        for m in _RE_INLINE_IMAGE.finditer(line):
            _hit(*m.span(), "image", "inline", m.group(2))
        for m in _RE_INLINE_LINK.finditer(line):
            _hit(*m.span(), "link", "inline", m.group(2))
        for m in _RE_REF_IMAGE.finditer(line):
            _hit(*m.span(), "image", "reference", m.group(2) or m.group(1))
        for m in _RE_REF_LINK.finditer(line):
            _hit(*m.span(), "link", "reference", m.group(2) or m.group(1))
        for m in _RE_AUTOLINK_URL.finditer(line):
            _hit(*m.span(), "link", "autolink_url", m.group(1))
        for m in _RE_AUTOLINK_MAIL.finditer(line):
            _hit(*m.span(), "link", "autolink_mail", m.group(1), is_mail=True)
        for m in _RE_BARE_URL.finditer(line):
            _hit(*m.span(), "link", "bare_url", m.group(0))
        # ── P2 (b3-4) inline four kinds ──────────────────────────
        for m in _RE_CITATION.finditer(line):          # D-Q1 pandoc
            _hit_raw(*m.span(), "citation", "pandoc", "complete", None,
                     {"key": m.group(1)})
        if jsx_enabled:                                 # D-Q7, mdx only
            for m in _RE_JSX_TAG.finditer(line):
                _hit_raw(*m.span(), "component", "jsx", "complete", None,
                         {"tag": m.group(1)})
        for kind, rex in (                              # D-Q8 template
            ("dollar_var", _RE_TMPL_DOLLAR),
            ("double_brace", _RE_TMPL_DOUBLE),
            ("percent_tag", _RE_TMPL_PERCENT),
            ("erb", _RE_TMPL_ERB),
        ):
            for m in rex.finditer(line):
                _hit_raw(*m.span(), "unresolved", kind, "unresolved",
                         "UNKNOWN_TEMPLATE_ENGINE", None)
        for m in _RE_EQ_INLINE.finditer(line):         # D-Q3 adjacent pairing
            _hit_raw(*m.span(), "equation", "inline_dollar",
                     "complete", None, None)
    return hits


def derive_markdown_nodes(text, metadata):
    """Line-level segmentation → flat node tree, written into ``metadata["markdown_nodes"]``.

    Reads only existing facts keys (front_matter/markdown_tables/merge_conflicts/
    heading_candidates/meta_directives/footnotes) + the shared fence extractor,
    zero rewrites of existing keys; an overall failure writes no half-built key (built locally, assigned in one shot at the end).
    """
    body = text.splitlines()
    kept = text.splitlines(keepends=True)
    n = len(body)

    # Line-start offset table: char_starts[i] = the char start of line i+1 (i in 0..n);
    # char_starts[n] = len(text) — the half-open right end of the line range [a,b] is char_starts[b].
    char_starts = [0]
    byte_starts = [0]
    for rl in kept:
        char_starts.append(char_starts[-1] + len(rl))
        byte_starts.append(byte_starts[-1] + len(rl.encode("utf-8")))

    # ── 1. Per-line category table ─────────────────────────────────────────
    flags = [set() for _ in range(n + 1)]    # 1-based: category set hit per line
    primary = [None] * (n + 1)               # 1-based: the winning primary category

    fm = metadata.get("front_matter")
    fm_ok = isinstance(fm, dict) and fm.get("status") in ("parsed", "empty")
    if fm_ok:
        for ln in range(fm["line_start"], fm["line_end"] + 1):
            flags[ln].add(_FM)

    fence_blocks = []
    for block in extract_fenced_code_blocks(text):
        for ln in range(block["line_start"], block["line_end"] + 1):
            flags[ln].add(_FENCE)
        fence_blocks.append(block)

    tables = metadata.get("markdown_tables")
    table_entries = []
    if isinstance(tables, dict):
        for e in tables.get("entries", []):
            for ln in range(e["line_start"], e["line_end"] + 1):
                flags[ln].add(_TABLE)
            table_entries.append(e)

    conflicts = metadata.get("merge_conflicts")
    conflict_entries = []
    if isinstance(conflicts, dict):
        for e in conflicts.get("entries", []):
            last = e["line_end"] if e.get("line_end") is not None else n
            for ln in range(e["line_start"], last + 1):
                flags[ln].add(_CONFLICT)
            conflict_entries.append(e)

    heading_rows = {}
    for cand in metadata.get("heading_candidates") or []:
        if getattr(cand, "pattern", None) != "markdown":
            continue
        ln = bisect.bisect_right(char_starts, cand.start)
        if ln <= n and ln not in heading_rows:
            flags[ln].add(_HEADING)
            heading_rows[ln] = cand

    directives = metadata.get("meta_directives")
    directive_lines = []
    if isinstance(directives, dict):
        for ln in directives.get("locations") or []:
            if 1 <= ln <= n:
                flags[ln].add(_DIRECTIVE)
                directive_lines.append(ln)

    footnotes = metadata.get("footnotes")
    footnote_lines = []
    if isinstance(footnotes, dict):
        for ln in footnotes.get("definition_lines") or []:
            if 1 <= ln <= n:
                flags[ln].add(_FOOTNOTE)
                footnote_lines.append(ln)

    # ── P2 (b3-4) block-level pre-scan: $$ equation blocks / ::: container boundaries / html consecutive lines ─
    sp = metadata.get("syntax_profile")
    jsx_enabled = (
        isinstance(sp, dict) and sp.get("extension_profile") == "mdx"
    )

    eq_entries = []        # (line_start, line_end, closed)
    ln = 1
    while ln <= n:
        if (flags[ln] & _INTERVAL_CATS) or not _RE_EQ_BLOCK.match(body[ln - 1]):
            ln += 1
            continue
        stripped = body[ln - 1].strip()
        if len(stripped) >= 5 and stripped.endswith("$$"):
            eq_entries.append((ln, ln, True))     # single-line self-closing $$…$$
            ln += 1
            continue
        close = None
        j = ln + 1
        while j <= n:
            if (not (flags[j] & _INTERVAL_CATS)
                    and _RE_EQ_BLOCK.match(body[j - 1])):
                close = j
                break
            j += 1
        if close is None:
            eq_entries.append((ln, ln, False))    # unclosed: only the opener line is registered
            ln += 1
        else:
            eq_entries.append((ln, close, True))
            ln = close + 1
    for a, b, closed in eq_entries:
        flags[a].add(_EQUATION)
        if b != a:
            flags[b].add(_EQUATION)

    container_entries = []   # dict: line/node_type/kind/engine/variant/…
    open_kind = None         # None | "component" | "unknown_extension"
    open_entry = None        # entry of the current unclosed opener (settled on close/displacement/EOF)

    def _settle_unclosed(entry):
        # Honest marking of openers that never get a close (displaced by a new opener or end of file)
        # (symmetric with EQUATION_UNCLOSED; §7 closing patch): a known admonition degrades to
        # partial + CONTAINER_UNCLOSED; unknown_extension keeps its original
        # unsupported/UNKNOWN_MARKDOWN_EXTENSION (the more severe verdict wins, never rewritten)
        if entry["node_type"] == "component":
            entry["status"] = "partial"
            entry["reason"] = "CONTAINER_UNCLOSED"

    for ln in range(1, n + 1):
        if ((flags[ln] & _INTERVAL_CATS)
                or not _RE_COLON_LINE.match(body[ln - 1])):
            continue
        m = _RE_COLON_WORD.match(body[ln - 1])
        word = m.group(1) if m else None
        if word is not None:
            if open_entry is not None:
                _settle_unclosed(open_entry)   # double opener: the former never gets its close
            known = word.lower() in _ADMONITION_ENGINES
            open_kind = "component" if known else "unknown_extension"
            open_entry = {
                "line": ln, "node_type": open_kind, "kind": "open",
                "engine": "admonition" if known else "unknown",
                "variant": word,
                "status": "complete" if known else "unsupported",
                "reason": None if known else "UNKNOWN_MARKDOWN_EXTENSION",
            }
            container_entries.append(open_entry)
        elif open_kind is not None:               # a bare ::: = boundary closer
            container_entries.append({
                "line": ln, "node_type": open_kind, "kind": "close",
                "engine": "admonition" if open_kind == "component"
                           else "unknown",
                "variant": None,
                "status": "complete" if open_kind == "component"
                           else "unsupported",
                "reason": None if open_kind == "component"
                           else "UNKNOWN_MARKDOWN_EXTENSION",
            })
            open_kind = None
            open_entry = None
        else:                                      # orphan ::: (no opener context)
            container_entries.append({
                "line": ln, "node_type": "unknown_extension",
                "kind": "orphan", "engine": None, "variant": None,
                "status": "unsupported",
                "reason": "UNKNOWN_MARKDOWN_EXTENSION",
            })
    if open_entry is not None:                    # EOF: the opener never closed
        _settle_unclosed(open_entry)
    for e in container_entries:
        flags[e["line"]].add(
            _CONTAINER if e["node_type"] == "component" else _UNKNOWN_EXT
        )

    ln = 1
    while ln <= n:
        if ((flags[ln] & _INTERVAL_CATS)
                or not _RE_HTML_LINE.match(body[ln - 1])
                or body[ln - 1].lstrip().startswith(_HTML_LINE_EXCLUDES)):
            ln += 1
            continue
        end = ln
        while end + 1 <= n:
            if ((flags[end + 1] & _INTERVAL_CATS)
                    or not _RE_HTML_LINE.match(body[end])
                    or body[end].lstrip().startswith(_HTML_LINE_EXCLUDES)):
                break
            end += 1
        for k in range(ln, end + 1):
            flags[k].add(_HTML)
        ln = end + 1

    priority_conflicts = 0
    blank_count = 0
    for ln in range(1, n + 1):
        f = flags[ln]
        line = body[ln - 1]
        if not (f & _INTERVAL_CATS):  # range-claimed lines run no semantic regexes (noise prevention)
            if _RE_LIST_ITEM.match(line):
                f.add(_LIST)
            if _RE_QUOTE_LINE.match(line):
                f.add(_QUOTE)
        if len(f) > 1:
            priority_conflicts += 1
        if _FM in f:
            primary[ln] = _FM
        elif _FENCE in f:
            primary[ln] = _FENCE
        elif _TABLE in f:
            primary[ln] = _TABLE
        elif _CONFLICT in f:
            primary[ln] = _CONFLICT
        elif _HEADING in f:
            primary[ln] = _HEADING
        elif _DIRECTIVE in f:
            primary[ln] = _DIRECTIVE
        elif _FOOTNOTE in f:
            primary[ln] = _FOOTNOTE
        elif not line.strip():
            primary[ln] = _BLANK
            blank_count += 1
        elif _LIST in f:
            primary[ln] = _LIST
        elif _QUOTE in f:
            primary[ln] = _QUOTE
        elif _EQUATION in f:          # P2 block-level line category (D-Q4: after quote)
            primary[ln] = _EQUATION
        elif _CONTAINER in f:
            primary[ln] = _CONTAINER
        elif _UNKNOWN_EXT in f:
            primary[ln] = _UNKNOWN_EXT
        elif _HTML in f:
            primary[ln] = _HTML
        else:
            primary[ln] = _PLAIN

    # ── 2. Node construction ───────────────────────────────────────
    nodes = []
    item_links = []   # (item_dict, parent_dict) — parent_id backfilled after numbering

    def _add(node_type, a, b, *, status="complete", reason_code=None,
             evidence_type=None, type_facts=None):
        nd = {
            "node_id": None,          # uniformly numbered after sorting
            "node_type": node_type,
            "parent_id": None,
            "source_ref": {"line_start": a, "line_end": b},
            "raw_span": {
                "char_start": char_starts[a - 1],
                "char_end": char_starts[b],
                "byte_start": byte_starts[a - 1],
                "byte_end": byte_starts[b],
                "positions_basis": _POSITIONS_BASIS,
            },
            "resolution_status": status,
            "reason_code": reason_code,
            "evidence_type": evidence_type,
        }
        if type_facts:
            nd["type_facts"] = type_facts
        nodes.append(nd)
        return nd

    if fm_ok:
        _add(_FM, fm["line_start"], fm["line_end"],
             evidence_type="facts_registry",
             type_facts={"style": fm.get("style")})

    for b in fence_blocks:
        info = b["fence_info"]
        parts = info.split()
        first = parts[0].lower() if parts else ""
        if first in _DIAGRAM_ENGINES:          # P1 diagram upgrade (b3-3a)
            _add("diagram", b["line_start"], b["line_end"],
                 status="complete" if b["closed"] else "partial",
                 reason_code=None if b["closed"] else "unclosed_fence",
                 evidence_type="shared_extractor",
                 type_facts={"engine": first, "fence_info": info,
                             "closed": b["closed"]})
        else:
            _add(_FENCE, b["line_start"], b["line_end"],
                 status="complete" if b["closed"] else "partial",
                 reason_code=None if b["closed"] else "unclosed_fence",
                 evidence_type="shared_extractor",
                 type_facts={"fence_info": info, "closed": b["closed"]})

    for e in table_entries:
        ragged = bool(e.get("ragged_rows")) or bool(e.get("ragged_rows_truncated"))
        _add(_TABLE, e["line_start"], e["line_end"],
             status="complete" if not ragged else "partial",
             reason_code=None if not ragged else "ragged_table",
             evidence_type="facts_registry",
             type_facts={"header_cols": e["header_cols"]})

    for e in conflict_entries:
        balanced = bool(e.get("balanced"))
        _add(_CONFLICT, e["line_start"], e["line_end"] if e.get("line_end") is not None else n,
             status="complete" if balanced else "partial",
             reason_code=None if balanced else "unbalanced_conflict",
             evidence_type="facts_registry",
             type_facts={"balanced": balanced})

    for a, b, closed in eq_entries:                  # P2 $$ display blocks (b3-4)
        _add(_EQUATION, a, b,
             status="complete" if closed else "partial",
             reason_code=None if closed else "EQUATION_UNCLOSED",
             evidence_type="native_markdown",
             type_facts={"kind": "display_block", "closed": closed})

    for e in container_entries:                      # P2 ::: container boundaries (b3-4)
        _add(e["node_type"], e["line"], e["line"],
             status=e["status"], reason_code=e["reason"],
             evidence_type="native_markdown",
             type_facts={"kind": e["kind"], "engine": e["engine"],
                         "variant": e["variant"]})

    for ln in sorted(heading_rows):
        if primary[ln] != _HEADING:   # line claimed by a higher priority — the lower one builds nothing
            continue
        cand = heading_rows[ln]
        _add(_HEADING, ln, ln,
             evidence_type="shared_extractor",
             type_facts={"level": cand.depth_hint})

    for ln in sorted(directive_lines):
        if primary[ln] != _DIRECTIVE:
            continue
        _add(_DIRECTIVE, ln, ln, evidence_type="facts_registry")

    for ln in sorted(footnote_lines):
        if primary[ln] != _FOOTNOTE:
            continue
        _add(_FOOTNOTE, ln, ln, evidence_type="facts_registry")

    ln = 1
    while ln <= n:
        cat = primary[ln]
        if cat not in _RUN_CATS:
            ln += 1
            continue
        end = ln
        while end + 1 <= n and primary[end + 1] == cat:
            end += 1
        if cat == _LIST:
            list_nd = _add(_LIST, ln, end, evidence_type="native_markdown")
            for k in range(ln, end + 1):
                tf = None
                if _RE_TASK_ITEM.match(body[k - 1]):
                    tf = {"task": True}
                item = _add("list_item", k, k, evidence_type="native_markdown",
                            type_facts=tf)
                item_links.append((item, list_nd))
        elif cat == _QUOTE:
            _add(_QUOTE, ln, end, evidence_type="native_markdown")
        elif cat == _HTML:                            # P2 html consecutive-line blocks (b3-4)
            m = _RE_HTML_LINE.match(body[ln - 1])
            _add(_HTML, ln, end, evidence_type="native_markdown",
                 type_facts={"first_tag": m.group(1) if m else None})
        else:
            _add(_PLAIN, ln, end, evidence_type="native_markdown")
        ln = end + 1

    # ── 2b. P1/P2 inline elements: link / image / citation / jsx / template /
    #    inline equation ──────────────────────────────────────────
    inline_records = []   # (node_dict, line_no) — parent_id backfilled after numbering
    for h in _collect_inline_hits(body, flags, jsx_enabled=jsx_enabled):
        cs = char_starts[h["line"] - 1] + h["start"]
        ce = char_starts[h["line"] - 1] + h["end"]
        prefix = body[h["line"] - 1][:h["start"]]
        type_facts = {"kind": h["kind"]}
        type_facts.update(h.get("facts") or {})
        nd = _add(h["node_type"], h["line"], h["line"],
                  status=h["status"], reason_code=h["reason"],
                  evidence_type="native_markdown",
                  type_facts=type_facts)
        nd["raw_span"] = {
            "char_start": cs, "char_end": ce,
            "byte_start": byte_starts[h["line"] - 1]
                          + len(prefix.encode("utf-8")),
            "byte_end": byte_starts[h["line"] - 1]
                        + len(body[h["line"] - 1][:h["end"]].encode("utf-8")),
            "positions_basis": _POSITIONS_BASIS,
        }
        inline_records.append((nd, h["line"]))

    # ── 3. Sort / truncate / number / back-link ──────────────────────────
    nodes.sort(key=lambda nd: nd["source_ref"]["line_start"])
    truncated = False
    if len(nodes) > NODE_LIMIT:
        nodes = nodes[:NODE_LIMIT]      # document-order truncation; the parent list always precedes the item, no orphan references
        truncated = True
    for i, nd in enumerate(nodes, 1):
        nd["node_id"] = "md-n%04d" % i
    kept_ids = {id(nd) for nd in nodes}
    for item, parent in item_links:
        if id(item) in kept_ids:
            item["parent_id"] = parent["node_id"]

    # Inline-node parent backfill (D-R5): points at the dominant block node covering that line;
    # nodes are already in document order, later-built narrow ranges (list_item) cover the same line's earlier-built wide range (list)
    # the inline forms of equation/component (inline_dollar/jsx) claim no line — not entered in the parent table
    _INLINE_KINDS = {(_EQUATION, "inline_dollar"), (_CONTAINER, "jsx")}
    block_of_line = [None] * (n + 1)
    for nd in nodes:
        if nd["node_type"] in _BLOCK_PARENT_TYPES:
            k = (nd.get("type_facts") or {}).get("kind")
            if (nd["node_type"], k) in _INLINE_KINDS:
                continue
            for ln in range(nd["source_ref"]["line_start"],
                            nd["source_ref"]["line_end"] + 1):
                block_of_line[ln] = nd
    for nd, ln in inline_records:
        if id(nd) in kept_ids and block_of_line[ln] is not None:
            nd["parent_id"] = block_of_line[ln]["node_id"]

    # ── 4. coverage accounting ─────────────────────────────────────
    covered = [False] * (n + 1)
    for nd in nodes:
        for ln in range(nd["source_ref"]["line_start"],
                        nd["source_ref"]["line_end"] + 1):
            covered[ln] = True
    covered_count = sum(covered)

    truncated_fact_sources = []
    if isinstance(tables, dict) and tables.get("entries_truncated"):
        truncated_fact_sources.append("markdown_tables")
    if isinstance(conflicts, dict) and conflicts.get("entries_truncated"):
        truncated_fact_sources.append("merge_conflicts")
    if isinstance(directives, dict) and directives.get("locations_truncated"):
        truncated_fact_sources.append("meta_directives")
    if isinstance(footnotes, dict) and footnotes.get("locations_truncated"):
        truncated_fact_sources.append("footnotes")

    doc_node = {
        "node_id": "md-n0000",
        "node_type": "document",
        "parent_id": None,
        "source_ref": {"line_start": 1, "line_end": n},
        "raw_span": {
            "char_start": 0,
            "char_end": len(text),
            "byte_start": 0,
            "byte_end": byte_starts[-1],
            "positions_basis": _POSITIONS_BASIS,
        },
        "resolution_status": "complete",
        "reason_code": None,
        "evidence_type": "constructed",
    }
    if n == 0:
        doc_node["source_ref"] = {"line_start": 1, "line_end": 0}

    payload = {
        "nodes": [doc_node] + nodes,
        "count": 1 + len(nodes),
        "coverage": {
            "document_lines": n,
            "classified_lines": covered_count,
            "blank_lines": blank_count,
            "unclassified_lines": n - covered_count - blank_count,
        },
        "priority_conflicts": priority_conflicts,
        "nodes_truncated": truncated,
        "truncated_fact_sources": truncated_fact_sources,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    metadata["markdown_nodes"] = payload
    return payload
