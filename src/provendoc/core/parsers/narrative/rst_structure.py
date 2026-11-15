# ============================================================
# Narrative Domain
# reStructuredText static structure facts (rst_structure_v1)
# ============================================================
"""reST static structure facts: evidence-only census (rst_static_structure_prereg_v1).

Fact boundary:
    - all facts land in metadata keys; the text channel keeps every character unchanged
      (evidence-first, same contract as the tex/markdown family precedent)
    - line-level census + context guards: simple-table separator lines must exclude title-underline
      pollution (3002 of the 3706 solo adornments are title underlines, probe-b evidence — without
      the guard false positives are guaranteed); literal-block trigger lines exclude
      ``..`` explicit-marker lines (the ``::`` of ``.. note::`` is directive syntax, not a
      paragraph-tail trigger; guard semantics calibrated on the five anchors 11→2/61→55/8→8/33→25/4→4)
    - the inline census (roles/links/footnote refs/substitution refs/inline markup) counts
      raw over the full text: whatever the author wrote gets counted (same meaning as the tex precedent "count \begin
      even inside comment bodies"); directive option areas are not excluded
    - diagnostics only register with reduced confidence (short underline / duplicate target /
      unresolved substitution etc.), never corrected, never simulated (the spec's own words
      "do not forcibly correct to standard hierarchy" quoted verbatim)
    - include/image targets are only registered + reconciled against local exists, never expanded
      (single-file source-evidence contract, same semantics as tex_includes_v2)
    - raw directives are counted only, executed is always false (security red line, spec batch 2 §6)
    - unknown directives keep verbatim-text semantics: UNKNOWN is registered only for names
      outside the known-directive table (corpus of 84 names fully tested; the only out-of-table name = handcrafted
      ``my-extension``）
    - P1 single-file deepening (rst_p1_single_file_prereg_v2, all additive):
      the role-by-name histogram is same-source with role_references, ±0; nested directive counting carries
      a literal-content guard (code/code-block/raw/parsed-literal bodies, ``::``
      literal-block bodies, directive-shaped lines inside comment bodies are teaching examples, not structure — scout anchors:
      ≥4 indented ``.. math::`` in sphinx teaching documents are syntax demos inside literal blocks);
      toctree/literalinclude/table options/version/glossary/
      citation/cross-references are registered only; the tri-state reasons frozen order has zero changes

Tri-state derivation (rst_parse_status) reasons frozen order:
    INVALID_ENCODING → INVALID_HEADING_UNDERLINE → DUPLICATE_TARGET
    → UNRESOLVED_FOOTNOTE → UNRESOLVED_SUBSTITUTION
    → UNRESOLVED_REFERENCE → UNKNOWN_RST_DIRECTIVE
    → RST_STRUCTURE_ERROR (PARSE_FAILED placed before parse_failed).
    partial never degrades the body (text stays the full source — source preservation first).
"""
import re
from collections import Counter
from pathlib import Path

# ── Line-level regex family (line-anchored; MULTILINE semantics naturally hold in the per-line loop) ──
_BULLET_RE = re.compile(r"^[-*+] ")
_ENUM_DIGIT_RE = re.compile(r"^\d+\. ")
_ENUM_AUTO_RE = re.compile(r"^#\. ")
_ENUM_ALPHA_RE = re.compile(r"^[a-zA-Z]\. ")
_ENUM_ROMAN_RE = re.compile(
    r"^(?:i{1,3}|iv|vi{0,3}|ix|x{1,3}|xl|l|xc|c)\. ", re.IGNORECASE,
)
_FIELD_LINE_RE = re.compile(r"^:[A-Za-z][\w -]*:( |$)")
_OPTION_LINE_RE = re.compile(r"^(?:-\w|--[\w-]+)(?:[ =][^\s]*)?\s{2}")
_DIRECTIVE_LINE_RE = re.compile(r"^\.\. ([a-zA-Z][\w-]*)::")
_TARGET_DEF_RE = re.compile(r"^\.\. _([^:\s]+):")
_ANON_TARGET_RE = re.compile(r"^\.\. __:")
_FOOTNOTE_DEF_RE = re.compile(r"^\.\. \[(#+|\*|\d+)\]")
_CITATION_DEF_RE = re.compile(r"^\.\. \[([^\]#\*\d][^\]]*)\]")
_SUBST_DEF_RE = re.compile(r"^\.\. \|([^\n|]+)\|")
_INCLUDE_LINE_RE = re.compile(r"^\.\. include::[ \t]*(\S+)")
_IMAGE_LINE_RE = re.compile(r"^\.\. (image|figure)::[ \t]*(\S+)")
_GRID_BORDER_RE = re.compile(r"^\s*\+[-=+]+\+")
_GRID_HEADER_RE = re.compile(r"^\s*\+[+=]*=+[+=]*\+$")
_SIMPLE_SEP_ROW_RE = re.compile(r"^\s*={2,}(?:\s+={2,})+\s*$")
_ADORNMENT_CHARS = set("=-~^\"`:.'_*+#")

# ── Inline regex family ──
_ROLE_RE = re.compile(r"(:[a-zA-Z][\w.+-]*:)+`[^`\n]*`")
_EMPHASIS_RE = re.compile(r"(?<![\w*\\])\*(?!\s)[^*\n]+?(?<!\s)\*(?![\w*])")
_STRONG_RE = re.compile(r"(?<!\*)\*\*(?!\s)[^*\n]+?(?<!\s)\*\*(?!\*)")
_LITERAL_INLINE_RE = re.compile(r"(?<!`)`[^`\n]+?`(?!`)")
_NAMED_REF_RE = re.compile(r"`[^`\n]+`_(?!_)")
_INLINE_LINK_RE = re.compile(r"`[^`\n]+ <[^`\n]+>`__?")
_INLINE_LINK_URL_RE = re.compile(r"`[^`\n]+ <([^`\n]+)>`__?")
_STANDALONE_URL_RE = re.compile(r"^https?://\S+$")
_BARE_ANON_URL_RE = re.compile(r"^__\s+([^\s]+)")
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_FOOTNOTE_REF_RE = re.compile(r"\[(#+|\*|\d+)\]_")
_SUBST_REF_RE = re.compile(r"(?<!\|)\|([A-Za-z][\w. -]*)\|(?!\|)")
_CODEBLOCK_LINE_RE = re.compile(r"^\.\. code(?:-block)?::[ \t]*(\S*)")
_ANY_DIRECTIVE_RE = re.compile(r"^(\s*)\.\. ([a-zA-Z][\w-]*)::[ \t]*(.*)$")
_REF_ROLE_RE = re.compile(r":ref:`([^`\n]*)`")
_CIT_REF_RE = re.compile(r"\[([^\]#\*\d][^\]]*)\]_")
_NESTED_OPT_RE = re.compile(r"^\s+:[\w-]+:")

# ── Directive classification table (84-name corpus fully tested; unknown only accepts out-of-table names) ──
_ADMONITION_NAMES = {
    "note", "warning", "attention", "caution", "danger", "error",
    "hint", "important", "tip", "admonition", "sidebar", "topic",
    "epigraph", "pull-quote", "container",
}
_SPHINX_API_NAMES = {
    "method", "function", "class", "attribute", "exception", "data",
    "module", "currentmodule", "classmethod", "staticmethod",
    "decorator", "object", "automethod", "autofunction", "autoclass",
    "automodule", "autoattribute", "autoexception", "autodata",
    "autosummary", "confval", "envvar", "describe", "productionlist",
    "program", "option", "audit-event", "event", "role",
    "availability", "moduleauthor", "sectionauthor", "impl-detail",
    "tab", "tabs",
}
_VERSION_NAMES = {
    "versionadded", "versionchanged", "deprecated", "versionremoved",
    "deprecated-removed", "soft-deprecated",
}
_TABLE_DIRECTIVE_NAMES = {"table", "list-table", "csv-table", "tabularcolumns"}
_CODE_NAMES = {"code-block", "code", "sourcecode"}
_IMAGE_NAMES = {"image", "figure"}
_MISC_NAMES = {
    "index", "seealso", "rubric", "highlight", "only", "doctest",
    "testcode", "testoutput", "testsetup", "testcleanup", "todo",
    "grammar-snippet", "glossary", "inheritance-diagram", "contents",
    "cssclass", "rst-class", "parsed-literal", "line-block",
    "header", "footer", "date", "meta", "hlist", "sectnum",
}

_MAX_SAMPLES = 5
_MAX_SYSTEM_MESSAGES = 20
_MAX_INCLUDE_TARGETS = 20
_MAX_IMAGE_TARGETS = 20
_URL_SNIPPET = 120  # r19 external-link target sample truncation

# ── P1 constants (rst_p1_single_file_prereg_v2) ──
_LITERAL_BODY_DIRECTIVE_NAMES = {"code", "code-block", "raw", "parsed-literal"}
_VERSION_DIRECTIVE_NAMES = (
    "versionadded", "versionchanged", "deprecated", "versionremoved",
)


def classify_directive(name):
    """Directive name → classification (unknown names return unknown)."""
    if name in _ADMONITION_NAMES:
        return "admonition"
    if name in _SPHINX_API_NAMES:
        return "sphinx_api"
    if name in _VERSION_NAMES:
        return "version"
    if name in _TABLE_DIRECTIVE_NAMES:
        return "table_directive"
    if name in _CODE_NAMES:
        return "code"
    if name in _IMAGE_NAMES:
        return "image"
    if name == "math":
        return "math"
    if name == "raw":
        return "raw"
    if name == "toctree":
        return "toctree"
    if name == "literalinclude":
        return "literalinclude"
    if name == "include":
        return "include"
    if name in _MISC_NAMES:
        return "sphinx_misc"
    return "unknown"


def _is_uniform_adornment(stripped, min_len=2):
    """Whole string made of one adornment character (reaching the minimum length) → return that character."""
    if len(stripped) < min_len:
        return None
    if stripped[0] not in _ADORNMENT_CHARS:
        return None
    if set(stripped) != {stripped[0]}:
        return None
    return stripped[0]


def _indent_of(line):
    """Leading-indent column count (blank lines return 0)."""
    stripped = line.lstrip()
    return len(line) - len(stripped) if stripped else 0


def _literal_content_lines(lines):
    """Line-number set of literal content (the semantic basis of the nested-directive guard).

    Literal content = code/code-block/raw/parsed-literal directive bodies + ``::``-triggered
    literal-block bodies + comment bodies (the marker lines of target/footnote/citation/substitution
    definitions are handled by region start). Directive-shaped lines falling inside these regions are
    teaching examples (scout anchors: ≥4 indented ``.. math::`` in sphinx teaching documents are syntax
    demos inside literal blocks); nested counting and structure registration exclude them all.
    """
    skip = set()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind = _indent_of(line)
        region = False
        m = _ANY_DIRECTIVE_RE.match(line)
        if m:
            region = m.group(2).lower() in _LITERAL_BODY_DIRECTIVE_NAMES
        elif line.lstrip().startswith(".. ") or line.rstrip() == "..":
            region = True
        elif line.rstrip().endswith("::"):
            region = True
        if region:
            j = i + 1
            while j < n and (not lines[j].strip() or _indent_of(lines[j]) > ind):
                skip.add(j)
                j += 1
            i = j
            continue
        i += 1
    return skip


def _directive_instances(lines):
    """Instance list of all directive-shaped lines (any indent), carrying literal-region flags.

    Top-level (indent 0) instances = the P0 frozen directive_counts semantics; nested
    instances enter directive_counts_nested only outside literal-content regions.
    """
    skip = _literal_content_lines(lines)
    out = []
    for i, line in enumerate(lines):
        m = _ANY_DIRECTIVE_RE.match(line)
        if not m:
            continue
        out.append({
            "idx": i,
            "line": line,
            "indent": len(m.group(1)),
            "name": m.group(2).lower(),
            "arg": m.group(3).strip(),
            "in_literal_region": i in skip,
        })
    return out


# ── Directive evidence v3 (v2 superset: classified counts + language histogram + unknown + nested guard) ──

def directive_evidence_v2(text):
    """Directive facts v3 (v2 superset): top-level per-name counts + nested counts (post-guard).

    v1/v2 fields (directive_counts/directive_total/directive_samples/
    directive_class_counts/codeblock_*/unknown_*/raw_directive_count/
    executed/note) all preserved; new directive_counts_nested and
    nested_directive_total added (any-indent directive-shaped lines, excluding literal-content-region
    teaching examples). Directives are never interpreted, never expanded.
    """
    lines = text.splitlines()
    counts = {}
    nested_counts = {}
    samples = []
    classes = {}
    codeblock_langs = Counter()
    codeblock_unlabeled = 0
    unknown_names = []
    raw_count = 0
    for inst in _directive_instances(lines):
        name = inst["name"]
        if inst["indent"] == 0:
            counts[name] = counts.get(name, 0) + 1
            if len(samples) < _MAX_SAMPLES:
                samples.append(name)
            cls = classify_directive(name)
            classes[cls] = classes.get(cls, 0) + 1
            if cls == "raw":
                raw_count += 1
            if cls == "unknown" and name not in unknown_names:
                unknown_names.append(name)
            if name in ("code", "code-block"):
                cmatch = _CODEBLOCK_LINE_RE.match(inst["line"])
                if cmatch:
                    lang = cmatch.group(1)
                    if lang:
                        codeblock_langs[lang] += 1
                    else:
                        codeblock_unlabeled += 1
        elif not inst["in_literal_region"]:
            nested_counts[name] = nested_counts.get(name, 0) + 1
    return {
        "extractor": "rst_directives_v3",
        "directive_counts": counts,
        "directive_total": sum(counts.values()),
        "directive_samples": samples,
        "directive_class_counts": classes,
        "codeblock_language_counts": dict(codeblock_langs),
        "codeblock_unlabeled_count": codeblock_unlabeled,
        "unknown_directive_count": sum(counts[n] for n in unknown_names),
        "unknown_directive_names": unknown_names[:_MAX_SAMPLES],
        "raw_directive_count": raw_count,
        "directive_counts_nested": nested_counts,
        "nested_directive_total": sum(nested_counts.values()),
        "executed": False,
        "note": (
            "directives are counted and classified, never interpreted: "
            "toctree/include targets are not expanded (the composed "
            "document does not exist in this file's source); raw "
            "content is registered but never executed"
        ),
    }


# ── include option-block scan (batch 2 §3: mode + line_filter) ──

def _include_options(lines, idx):
    """Read the indented option area following an include line, returning (mode, line_filter)."""
    mode = "text"
    line_filter = {}
    j = idx + 1
    while j < len(lines) and lines[j].startswith(" ") and lines[j].strip():
        opt = lines[j].strip()
        if opt.startswith(":literal:"):
            mode = "literal"
        elif opt.startswith(":code:"):
            mode = "code"
            lang = opt[len(":code:"):].strip()
            if lang:
                line_filter["code"] = lang
        elif opt.startswith(":start-after:"):
            line_filter["start_after"] = opt[len(":start-after:"):].strip()
        elif opt.startswith(":end-before:"):
            line_filter["end_before"] = opt[len(":end-before:"):].strip()
        elif opt.startswith(":start-line:"):
            line_filter["start_line"] = opt[len(":start-line:"):].strip()
        elif opt.startswith(":end-line:"):
            line_filter["end_line"] = opt[len(":end-line:"):].strip()
        j += 1
    return mode, line_filter


def _overlaps(start, end, spans):
    return any(s < end and start < e for s, e in spans)


# ── Main entry (parser wiring point) ──

def collect_rst_structure_facts(text, metadata, file_path=None):
    """reST static structure evidence registration main entry (evidence-only, parser wiring point).

    Scan failure degrades silently to ``rst_structure_error``; the ``rst_parse_status`` derived
    view and the bounded ``rst_system_messages`` diagnostic list are always written last.
    The whole function never raises, never rewrites text.
    """
    try:
        structure = _census(text, file_path)
        metadata["rst_toctree_v1"] = structure.pop("rst_toctree_v1", {})
        metadata["rst_cross_references"] = structure.pop(
            "rst_cross_references", {},
        )
        metadata["rst_structure_v1"] = structure
    except Exception as exc:  # pragma: no cover - bottom-line silent degrade
        metadata["rst_structure_error"] = str(exc)
    metadata["rst_parse_status"] = _derive_rst_parse_status(metadata)
    metadata["rst_system_messages"] = derive_rst_system_messages(metadata)


def _census(text, file_path):
    """Single-pass line-level census (frozen semantics, see the module docstring)."""
    lines = text.splitlines()

    st = {
        "extractor": "rst_structure_v1",
        "line_count": len(lines),
        "char_count": len(text),
        "bullet_lines": 0,
        "enum_digit_lines": 0,
        "enum_auto_lines": 0,
        "enum_alpha_lines": 0,
        "enum_roman_lines": 0,
        "field_lines": 0,
        "option_lines": 0,
        "literal_blocks": 0,
        "literal_trigger_orphans": 0,
        "definition_list_candidates": 0,
        "emphasis_count": 0,
        "strong_count": 0,
        "literal_inline_count": 0,
        "role_references": 0,
        "named_target_count": 0,
        "anonymous_target_count": 0,
        "duplicate_target_count": 0,
        "duplicate_target_samples": [],
        "footnote_manual_definitions": 0,
        "footnote_auto_definitions": 0,
        "footnote_symbol_definitions": 0,
        "footnote_references": 0,
        "unresolved_footnote_count": 0,
        "unresolved_footnote_samples": [],
        "citation_definitions": 0,
        "named_references": 0,
        "inline_links": 0,
        "standalone_urls": 0,
        # ── r19 external-link target registration (additive; cross-family external-URL semantics) ──
        "external_link_target_count": 0,
        "external_link_target_samples": [],
        "external_link_scheme_counts": {},
        "unresolved_reference_count": 0,
        "unresolved_reference_samples": [],
        "substitution_definitions": 0,
        "substitution_references": 0,
        "unresolved_substitution_count": 0,
        "unresolved_substitution_samples": [],
        "grid_border_rows": 0,
        "grid_header_separator_rows": 0,
        "simple_table_separator_rows": 0,
        "simple_table_multicol_separator_rows": 0,
        "comment_lines": 0,
        # ── r25 (adversarial-repair batch): bare ``..`` standalone comment-block registration (additive) ──
        "comment_block_count": 0,
        "comment_samples": [],
        "short_underline_count": 0,
        "short_underline_samples": [],
        "raw_directive_count": 0,
        # ── P1 single-file deepening keys (rst_p1_single_file_prereg_v2, additive) ──
        "role_name_counts": {},
        "role_name_distinct": 0,
        "math_directive_count": 0,
        "math_directive_nested_count": 0,
        "version_directives": {},
        "glossary_directive_count": 0,
        "glossary_nested_count": 0,
        "glossary_term_candidates": 0,
        "citation_references_resolved": 0,
        "literalinclude_count": 0,
        "literalinclude_nested_count": 0,
        "literalinclude_targets": [],
        "literalinclude_options": {},
        "table_directive_options": {},
    }

    target_lines = {}
    footnote_manual_defs = set()
    citation_names = set()
    subst_defs = set()
    include_targets = []
    include_mode_samples = []
    image_targets = []
    named_target_total = 0

    def _register_external_link_target(url, lineno):
        """r19 external-link target registration: scheme prefix decides external (same semantics as the markdown autolink)."""
        if not _URI_SCHEME_RE.match(url):
            return
        st["external_link_target_count"] += 1
        scheme = url.split(":", 1)[0].lower()
        counts = st["external_link_scheme_counts"]
        counts[scheme] = counts.get(scheme, 0) + 1
        if len(st["external_link_target_samples"]) < _MAX_SAMPLES:
            st["external_link_target_samples"].append(
                {"line": lineno, "url": url[:_URL_SNIPPET]},
            )
    _role_name_counts = Counter()
    _ref_instances = []
    _citation_ref_instances = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""

        # ── explicit-marker line registration (the inline census does not exclude — raw full-text counting semantics) ──
        is_explicit = False
        dm = _DIRECTIVE_LINE_RE.match(line)
        if dm:
            is_explicit = True
            name = dm.group(1).lower()
            if name == "raw":
                st["raw_directive_count"] += 1
            elif name == "include":
                im = _INCLUDE_LINE_RE.match(line)
                if im:
                    target = im.group(1)
                    include_targets.append(target)
                    mode, line_filter = _include_options(lines, i)
                    if len(include_mode_samples) < _MAX_SAMPLES:
                        sample = {"target": target, "mode": mode}
                        if line_filter:
                            sample["line_filter"] = line_filter
                        include_mode_samples.append(sample)
            elif name in ("image", "figure"):
                gm = _IMAGE_LINE_RE.match(line)
                if gm:
                    image_targets.append(gm.group(2))
        # The anonymous-target check must come first: ``.. __:`` also matches the ``.. _x:``-shaped regex
        elif _ANON_TARGET_RE.match(line):
            is_explicit = True
            st["anonymous_target_count"] += 1
            # r19: external-link value registration on anonymous-target lines (same-line value-anchored semantics)
            am = _ANON_TARGET_RE.match(line)
            val = line[am.end():].strip()
            if val:
                _register_external_link_target(val, i + 1)
        elif _TARGET_DEF_RE.match(line):
            is_explicit = True
            tdm = _TARGET_DEF_RE.match(line)
            target_lines.setdefault(
                tdm.group(1), [],
            ).append(i + 1)
            named_target_total += 1
            # r19: external-link value registration on named-target lines (same-line value-anchored semantics)
            val = line[tdm.end():].strip()
            if val:
                _register_external_link_target(val, i + 1)
        else:
            fmatch = _FOOTNOTE_DEF_RE.match(line)
            if fmatch:
                is_explicit = True
                kind = fmatch.group(1)
                if kind == "#":
                    st["footnote_auto_definitions"] += 1
                elif kind == "*":
                    st["footnote_symbol_definitions"] += 1
                else:
                    st["footnote_manual_definitions"] += 1
                    footnote_manual_defs.add(kind)
            else:
                cmatch = _CITATION_DEF_RE.match(line)
                if cmatch:
                    is_explicit = True
                    st["citation_definitions"] += 1
                    citation_names.add(cmatch.group(1).strip())
                else:
                    smatch = _SUBST_DEF_RE.match(line)
                    if smatch:
                        is_explicit = True
                        st["substitution_definitions"] += 1
                        subst_defs.add(smatch.group(1).strip())
                    elif line.startswith(".. "):
                        is_explicit = True
                        st["comment_lines"] += 1
                    elif line.rstrip() == "..":
                        # r25 (adversarial-repair batch): the bare ``..`` standalone comment block —
                        # its existence and indented body text are facts the document declares; an undisclosed
                        # omission is a gap. The existing comment_lines semantics (``.. ``
                        # prefix lines) stay frozen; is_explicit is not set
                        # (literal-trigger/definition-list existing counting semantics stay frozen).
                        st["comment_block_count"] += 1
                        body = []
                        j = i + 1
                        while j < len(lines):
                            body_line = lines[j]
                            if body_line.strip() and not body_line.startswith(
                                (" ", "\t")
                            ):
                                break
                            if body_line.strip():
                                body.append(body_line.strip())
                            j += 1
                        if len(st["comment_samples"]) < _MAX_SAMPLES:
                            st["comment_samples"].append({
                                "line": i + 1,
                                "text": "\n".join(body)[:_URL_SNIPPET],
                            })

        # ── table rows (grid borders + header separators + multi-column simple separators) ──
        if _GRID_BORDER_RE.match(line) or (
            _GRID_HEADER_RE.match(line) and "=" in line
        ):
            st["grid_border_rows"] += 1
            if _GRID_HEADER_RE.match(line) and "=" in line:
                st["grid_header_separator_rows"] += 1
        elif _SIMPLE_SEP_ROW_RE.match(line):
            # multi-column separator lines (====  ====): naturally not title underlines (docutils
            # underlines contain no space-separated segments); no guard needed, count directly
            st["simple_table_multicol_separator_rows"] += 1

        # ── list/field/option lines ──
        if _BULLET_RE.match(line):
            st["bullet_lines"] += 1
        elif _ENUM_DIGIT_RE.match(line):
            st["enum_digit_lines"] += 1
        elif _ENUM_AUTO_RE.match(line):
            st["enum_auto_lines"] += 1
        elif _ENUM_ALPHA_RE.match(line):
            st["enum_alpha_lines"] += 1
        elif _ENUM_ROMAN_RE.match(line):
            st["enum_roman_lines"] += 1
        if _FIELD_LINE_RE.match(line):
            st["field_lines"] += 1
        elif _OPTION_LINE_RE.match(line):
            st["option_lines"] += 1

        # ── literal-block trigger (guard: exclude .. explicit-marker lines) ──
        if line.rstrip().endswith("::") and not is_explicit:
            nxt2 = lines[i + 2] if i + 2 < len(lines) else ""
            if nxt.startswith((" ", "\t")):
                st["literal_blocks"] += 1
            elif not nxt.strip() and nxt2.startswith((" ", "\t")):
                st["literal_blocks"] += 1
            else:
                st["literal_trigger_orphans"] += 1

        # ── simple-table separator lines (guard: exclude title underlines/top-and-bottom overline shapes) ──
        adorn_char = _is_uniform_adornment(stripped) if stripped else None
        if adorn_char is not None and adorn_char != "+":
            prev = lines[i - 1].strip() if i >= 1 else ""
            prev2 = lines[i - 2].strip() if i >= 2 else ""
            nxt2 = lines[i + 2] if i + 2 < len(lines) else ""
            is_overline = False
            if nxt.strip() and nxt2.strip():
                is_overline = (
                    _is_uniform_adornment(nxt2.strip()) == adorn_char
                )
            if prev and prev2 and not prev.startswith("..") and not is_overline:
                st["simple_table_separator_rows"] += 1

        # ── short-underline diagnostic (title line + short adornment + preceding blank-line guard) ──
        if (
            stripped
            and _is_uniform_adornment(stripped) is None
            and (i == 0 or not lines[i - 1].strip())
        ):
            nxt_stripped = nxt.strip()
            nxt_char = _is_uniform_adornment(nxt_stripped, min_len=1)
            if nxt_char is not None and len(nxt_stripped) < len(stripped):
                st["short_underline_count"] += 1
                if len(st["short_underline_samples"]) < _MAX_SAMPLES:
                    st["short_underline_samples"].append({
                        "line": i + 1,
                        "title": stripped[:40],
                        "adornment": nxt_stripped[:10],
                    })

        # ── definition-list candidates (conservative heuristic; the col-0 requirement naturally excludes directives/literal bodies) ──
        if (
            not is_explicit
            and not line.startswith((" ", "\t"))
            and stripped
            and not stripped.endswith("::")
            and not _FIELD_LINE_RE.match(line)
            and not _OPTION_LINE_RE.match(line)
            and stripped[0] not in "|+="
            and (i == 0 or not lines[i - 1].strip())
            and nxt.startswith((" ", "\t"))
            and nxt.strip()
        ):
            st["definition_list_candidates"] += 1

        # ── inline census (raw full-text counting, explicit-marker lines not excluded) ──
        role_spans = [(m.start(), m.end()) for m in _ROLE_RE.finditer(line)]
        st["role_references"] += len(role_spans)
        for m in _ROLE_RE.finditer(line):
            _role_name_counts[m.group(1).strip(":").lower()] += 1
        for rm in _REF_ROLE_RE.finditer(line):
            _ref_instances.append((i + 1, rm.group(1)))
        for cm in _CIT_REF_RE.finditer(line):
            _citation_ref_instances.append((i + 1, cm.group(1).strip()))
        for m in _LITERAL_INLINE_RE.finditer(line):
            if _overlaps(m.start(), m.end(), role_spans):
                continue
            if "<" in m.group(0):
                continue
            st["literal_inline_count"] += 1
        link_spans = [(m.start(), m.end()) for m in _INLINE_LINK_RE.finditer(line)]
        st["inline_links"] += len(link_spans)
        # r19: inline external-link target value registration (`text <url>`__ form)
        for lm in _INLINE_LINK_URL_RE.finditer(line):
            _register_external_link_target(lm.group(1).strip(), i + 1)
        for m in _NAMED_REF_RE.finditer(line):
            if _overlaps(m.start(), m.end(), link_spans):
                continue
            inner = m.group(0)[1:m.group(0).rindex("`")]
            if " <" in inner:
                continue
            st["named_references"] += 1
            # local reconciliation (unresolved is decided only for single tokens with no path/URL shape)
            if (
                " " not in inner
                and "/" not in inner
                and "@" not in inner
                and not inner.lower().startswith("http")
                and inner not in target_lines
                and inner not in citation_names
            ):
                st["unresolved_reference_count"] += 1
                if len(st["unresolved_reference_samples"]) < _MAX_SAMPLES:
                    st["unresolved_reference_samples"].append(
                        {"line": i + 1, "name": inner[:40]},
                    )
        if _STANDALONE_URL_RE.match(line):
            st["standalone_urls"] += 1
            # r19: standalone-line URL value registration
            _register_external_link_target(line.strip(), i + 1)
        # r19: bare anonymous-target line (`__ <url>` form) external-link value registration
        bam = _BARE_ANON_URL_RE.match(line)
        if bam:
            _register_external_link_target(bam.group(1), i + 1)
        for m in _FOOTNOTE_REF_RE.finditer(line):
            label = m.group(1)
            st["footnote_references"] += 1
            if label not in ("#", "*") and label not in footnote_manual_defs:
                st["unresolved_footnote_count"] += 1
                if len(st["unresolved_footnote_samples"]) < _MAX_SAMPLES:
                    st["unresolved_footnote_samples"].append(
                        {"line": i + 1, "label": label},
                    )
        for m in _SUBST_REF_RE.finditer(line):
            name = m.group(1).strip()
            st["substitution_references"] += 1
            if name and name not in subst_defs:
                st["unresolved_substitution_count"] += 1
                if len(st["unresolved_substitution_samples"]) < _MAX_SAMPLES:
                    st["unresolved_substitution_samples"].append(
                        {"line": i + 1, "name": name[:40]},
                    )
        emph_spans = role_spans + link_spans
        strong_spans = []
        for m in _STRONG_RE.finditer(line):
            if _overlaps(m.start(), m.end(), emph_spans):
                continue
            strong_spans.append((m.start(), m.end()))
            st["strong_count"] += 1
        for m in _EMPHASIS_RE.finditer(line):
            if _overlaps(m.start(), m.end(), emph_spans + strong_spans):
                continue
            st["emphasis_count"] += 1

    # ── named-target aggregation + duplicate detection ──
    st["named_target_count"] = named_target_total
    for name, lns in target_lines.items():
        if len(lns) > 1:
            st["duplicate_target_count"] += len(lns) - 1
            if len(st["duplicate_target_samples"]) < _MAX_SAMPLES:
                st["duplicate_target_samples"].append({
                    "name": name[:40],
                    "count": len(lns),
                    "first_line": lns[0],
                })

    # ── include / image local reconciliation (register, never expand) ──
    st["include_count"] = len(include_targets)
    st["include_targets"] = include_targets[:_MAX_INCLUDE_TARGETS]
    st["include_mode_samples"] = include_mode_samples
    st["image_figure_count"] = len(image_targets)
    st["image_targets"] = image_targets[:_MAX_IMAGE_TARGETS]
    if file_path is not None:
        base_dir = Path(file_path).resolve().parent
        self_path = Path(file_path).resolve()
        include_exists = {}
        include_missing = []
        circular = False
        for target in include_targets:
            probe = (base_dir / target).resolve()
            ok = probe.is_file()
            include_exists[target] = ok
            if ok and probe == self_path:
                circular = True
            elif not ok and len(include_missing) < _MAX_SAMPLES:
                include_missing.append(target)
        st["include_exists_check"] = include_exists
        st["include_missing_targets"] = include_missing
        st["include_circular_self_reference"] = circular
        image_exists = {}
        image_missing = []
        for target in image_targets:
            probe = (base_dir / target).resolve()
            ok = probe.is_file()
            image_exists[target] = ok
            if not ok and len(image_missing) < _MAX_SAMPLES:
                image_missing.append(target)
        st["image_exists_check"] = image_exists
        st["image_missing_targets"] = image_missing

    # ── P1: role histogram (same-source with role_references, ±0) ──
    st["role_name_counts"] = dict(_role_name_counts)
    st["role_name_distinct"] = len(_role_name_counts)

    # ── P1: citation reference local reconciliation (full definition set, single pass afterwards) ──
    st["citation_references_resolved"] = sum(
        1 for _, cname in _citation_ref_instances if cname in citation_names
    )

    # ── P1: nested directive structure (frozen semantics behind the literal-content guard) ──
    instances = _directive_instances(lines)
    version_counts = {}
    version_nested = {}
    version_samples = []
    toctree = {"count": 0, "nested_count": 0, "entries": 0,
               "options": {}, "entry_samples": []}
    litinc_targets = []
    litinc_opts = {}
    table_opts = {}
    glossary_top = 0
    glossary_nested = 0
    glossary_terms = 0
    math_top = 0
    math_nested = 0
    for inst in instances:
        iname = inst["name"]
        if iname in _VERSION_DIRECTIVE_NAMES:
            if inst["indent"] == 0:
                version_counts[iname] = version_counts.get(iname, 0) + 1
            elif not inst["in_literal_region"]:
                version_nested[iname] = version_nested.get(iname, 0) + 1
            if inst["arg"] and len(version_samples) < _MAX_SAMPLES:
                version_samples.append(
                    {"name": iname, "version": inst["arg"][:20]},
                )
        elif iname == "math":
            if inst["indent"] == 0:
                math_top += 1
            elif not inst["in_literal_region"]:
                math_nested += 1
    st["math_directive_count"] = math_top
    st["math_directive_nested_count"] = math_nested
    st["version_directives"] = {
        "counts": version_counts,
        "nested_counts": version_nested,
        "version_samples": version_samples,
    }

    # ── P1: block-structure directive scan (toctree/literalinclude/table/glossary) ──
    for inst in instances:
        iname = inst["name"]
        if iname not in ("toctree", "literalinclude",
                         "list-table", "csv-table", "glossary"):
            continue
        if inst["in_literal_region"]:
            continue
        d = inst["indent"]
        opts = {}
        header_done = False
        prev_blank = False
        child_indent = None
        j = inst["idx"] + 1
        while j < len(lines):
            ln = lines[j]
            if not ln.strip():
                # reST semantics: a blank line ends the option-header area (csv-table row data can
                # start with a :name: shape; without the blank-line stop it would miscount as options)
                header_done = True
                prev_blank = True
                j += 1
                continue
            if _indent_of(ln) <= d:
                break
            om = _NESTED_OPT_RE.match(ln)
            if not header_done:
                if om:
                    oname = om.group(0).strip()[1:-1]
                    opts[oname] = opts.get(oname, 0) + 1
                    prev_blank = False
                    j += 1
                    continue
                header_done = True
            cur_indent = _indent_of(ln)
            if iname == "toctree":
                if (not om and ln.strip()
                        and not ln.lstrip().startswith("..")):
                    toctree["entries"] += 1
                    if len(toctree["entry_samples"]) < _MAX_SAMPLES:
                        toctree["entry_samples"].append(ln.strip()[:40])
            elif iname == "glossary":
                # term candidates = direct-child indented lines + preceding blank line + non-option
                # (definition continuation lines indent deeper, not counted)
                if child_indent is None:
                    child_indent = cur_indent
                if (not om and prev_blank and cur_indent == child_indent):
                    glossary_terms += 1
            prev_blank = False
            j += 1
        if iname == "toctree":
            if d == 0:
                toctree["count"] += 1
            else:
                toctree["nested_count"] += 1
            for k, v in opts.items():
                toctree["options"][k] = toctree["options"].get(k, 0) + v
        elif iname == "literalinclude":
            if d == 0:
                st["literalinclude_count"] += 1
            else:
                st["literalinclude_nested_count"] += 1
            if inst["arg"]:
                litinc_targets.append(inst["arg"])
            for k, v in opts.items():
                litinc_opts[k] = litinc_opts.get(k, 0) + v
        elif iname == "glossary":
            if d == 0:
                glossary_top += 1
            else:
                glossary_nested += 1
        else:
            for k, v in opts.items():
                table_opts[k] = table_opts.get(k, 0) + v
    st["glossary_directive_count"] = glossary_top
    st["glossary_nested_count"] = glossary_nested
    st["glossary_term_candidates"] = glossary_terms
    st["literalinclude_targets"] = litinc_targets[:_MAX_INCLUDE_TARGETS]
    st["literalinclude_options"] = litinc_opts
    st["table_directive_options"] = table_opts
    if file_path is not None:
        base_dir = Path(file_path).resolve().parent
        st["literalinclude_exists_check"] = {
            t: (base_dir / t).resolve().is_file()
            for t in litinc_targets[:_MAX_INCLUDE_TARGETS]
        }
    toctree["note"] = (
        "toctree entries are recorded as evidence, never expanded "
        "or composed into this document"
    )
    st["rst_toctree_v1"] = toctree

    # ── P1: cross-reference registration (cross-file std labels are not error-judged in single-file mode) ──
    ref_resolved = 0
    ref_unresolved = 0
    for _line_no, raw_target in _ref_instances:
        t = raw_target.strip()
        if t.startswith("<") and t.endswith(">"):
            t = t[1:-1].strip()
        elif "<" in t:
            t = t.split("<")[-1].rstrip(">").strip()
        if t in target_lines:
            ref_resolved += 1
        else:
            ref_unresolved += 1
    st["rst_cross_references"] = {
        "ref_role_count": len(_ref_instances),
        "ref_local_resolved": ref_resolved,
        "ref_local_unresolved": ref_unresolved,
        "download_role_count": int(_role_name_counts.get("download", 0)),
        "note": (
            "cross-file std labels are not judgeable in single-file "
            "mode; unresolved means not defined in this file only"
        ),
    }

    st["executed"] = False
    st["note"] = (
        "evidence-only census: every fact is a count or bounded sample; "
        "the reST source stays in the narrative text verbatim; include/"
        "image targets are recorded with local existence checks but "
        "never expanded; raw directives are never executed"
    )
    return st


# ── tri-state derivation (reasons frozen order) ──

def _derive_rst_parse_status(metadata):
    """Derives the document-level tri-state from registered evidence keys — reasons frozen order (see the module docstring).

    failed = the ``parse_failed`` true value; partial = any active subset reason hit;
    complete = none of the above. status values follow the lowercase txt/tex/markdown precedent.
    """
    reasons = []

    st = metadata.get("rst_structure_v1")
    if not isinstance(st, dict):
        st = {}

    sanitization = metadata.get("sanitization")
    if (isinstance(sanitization, dict)
            and (sanitization.get("replacement_injected") or 0) > 0) \
            or metadata.get("encoding_fallback"):
        reasons.append("INVALID_ENCODING")
    if st.get("short_underline_count"):
        reasons.append("INVALID_HEADING_UNDERLINE")
    if st.get("duplicate_target_count"):
        reasons.append("DUPLICATE_TARGET")
    if st.get("unresolved_footnote_count"):
        reasons.append("UNRESOLVED_FOOTNOTE")
    if st.get("unresolved_substitution_count"):
        reasons.append("UNRESOLVED_SUBSTITUTION")
    if st.get("unresolved_reference_count"):
        reasons.append("UNRESOLVED_REFERENCE")

    directives = metadata.get("rst_directives")
    if isinstance(directives, dict) and directives.get("unknown_directive_count"):
        reasons.append("UNKNOWN_RST_DIRECTIVE")

    if metadata.get("rst_structure_error"):
        reasons.append("RST_STRUCTURE_ERROR")

    if metadata.get("parse_failed"):
        return {"status": "failed", "reasons": ["PARSE_FAILED", *reasons]}
    if reasons:
        return {"status": "partial", "reasons": reasons}
    return {"status": "complete", "reasons": []}


# ── system messages (batch 2 §1: diagnostics never mix into the body, separate track from reasons) ──

def derive_rst_system_messages(metadata):
    """Derives a bounded diagnostic list from structure evidence (code/severity/line/message/confidence).

    Separate track from rst_parse_status: reasons run the tri-state, system_messages run the
    per-item diagnostic evidence; parser diagnostics never mix into the body.
    """
    messages = []
    st = metadata.get("rst_structure_v1")
    if not isinstance(st, dict):
        return messages

    for sample in st.get("short_underline_samples", []):
        messages.append({
            "code": "RST_SYSTEM_MESSAGE",
            "severity": "warning",
            "line": sample.get("line"),
            "message": (
                f"heading underline shorter than title: "
                f"{sample.get('title')!r}"
            ),
            "confidence": 0.9,
        })
    for sample in st.get("duplicate_target_samples", []):
        messages.append({
            "code": "RST_SYSTEM_MESSAGE",
            "severity": "warning",
            "line": sample.get("first_line"),
            "message": f"duplicate target definition: {sample.get('name')!r}",
            "confidence": 0.95,
        })
    for sample in st.get("unresolved_footnote_samples", []):
        messages.append({
            "code": "RST_SYSTEM_MESSAGE",
            "severity": "warning",
            "line": sample.get("line"),
            "message": f"undefined footnote reference: [{sample.get('label')}]_",
            "confidence": 0.8,
        })
    for sample in st.get("unresolved_substitution_samples", []):
        messages.append({
            "code": "RST_SYSTEM_MESSAGE",
            "severity": "info",
            "line": sample.get("line"),
            "message": (
                f"unresolved substitution (may come from rst_prolog): "
                f"|{sample.get('name')}|"
            ),
            "confidence": 0.5,
        })
    for sample in st.get("unresolved_reference_samples", []):
        messages.append({
            "code": "RST_SYSTEM_MESSAGE",
            "severity": "info",
            "line": sample.get("line"),
            "message": f"unresolved named reference: {sample.get('name')!r}",
            "confidence": 0.6,
        })
    directives = metadata.get("rst_directives")
    if isinstance(directives, dict):
        for name in directives.get("unknown_directive_names", []):
            messages.append({
                "code": "RST_SYSTEM_MESSAGE",
                "severity": "info",
                "line": None,
                "message": f"unknown directive type: {name!r}",
                "confidence": 0.7,
            })
    return messages[:_MAX_SYSTEM_MESSAGES]
