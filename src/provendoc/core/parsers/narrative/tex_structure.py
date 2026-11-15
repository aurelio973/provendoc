# ============================================================
# Narrative Domain
# LaTeX (.tex) static-structure facts (evidence-only census)
# ============================================================
"""TeX static-structure and semantic fact registration — P0: tex_static_structure_prereg_v1 +
P1: tex_p1_semantic_facts_prereg_v1 (both decided; v2 is a pure superset).

Discipline (aligned with the markdown_structure_facts precedent):

- **evidence-only**: register facts only; never rewrite ``text``, never touch representations,
  and never touch any existing metadata key (the tex_formulas / tex_includes v2 upgrade
  happens inside tex_parser and is a pure superset);
- single-pass lexical scanner: the comment channel (% to end of line, ``\\%`` escape excluded) + verbatim
  opaque regions (environment bodies with ``\\verb`` delimiter awareness) affect only the **state machine** (pairing/
  depth/tri-state derivation); no character is ever deleted from the text — census counts are
  computed over the **original full text** per the prereg §5 anchored semantics (a typesetting
  command inside a comment is still a fact the author wrote; the source-evidence contract makes no distinction);
- macro definitions register only the name and argument count, **never evaluated** (security
  red line: TeX is a Turing-complete typesetting language; structure registration does no expansion or execution semantics);
- ``\\write18`` / ``\\openin`` / ``\\openout`` / pipe ``\\input|`` are only counted
  and sampled, ``executed`` is always False (external I/O is always treated as not having happened at the parsing layer);
- local failures never sink the whole file: a total scanner failure degrades silently to the
  ``tex_structure_error`` key; tri-state derivation counts that key into partial reasons;
- tri-state is a **derived view** (aligned with the txt/markdown_parse_status precedent):
  ``{"status": "complete"|"partial"|"failed", "reasons": [...]}``,
  reason codes are emitted per the prereg frozen tokens in uppercase, frozen order:
  UNCLOSED_GROUP → UNCLOSED_ENVIRONMENT → UNCLOSED_MATH →
  UNCLOSED_VERBATIM → UNMATCHED_BEGIN → UNMATCHED_END →
  DUPLICATE_LABEL → INVALID_ENCODING → CATCODE_CHANGED →
  EXTERNAL_IO_BLOCKED → TEX_STRUCTURE_ERROR;
  MISSING_INPUT_FILE / CIRCULAR_INPUT are registered only inside tex_includes_v2
  (includes are never expanded per the contract; a missing target is a recorded fact, not a parse degradation).

State-machine semantic split (prereg §2 P2 decided wording):

- UNCLOSED_GROUP: a ``{`` outside comments/verbatim still open at EOF (depth>0);
- UNCLOSED_ENVIRONMENT: the environment stack is non-empty at EOF, or an environment
  swallowed by an outer ``\\end{other}`` (never got its own end);
- UNCLOSED_MATH: an unclosed display environment, or ``\\[`` at EOF without ``\\]``
  (inline ``$`` is exempt from pairing checks due to currency ambiguity — prefer missing over false);
- UNCLOSED_VERBATIM: an unclosed verbatim-family environment body, or a ``\\verb`` delimiter
  unclosed on the line;
- UNMATCHED_BEGIN: cross-name close events (an end closed onto a different begin;
  each swallowed begin counted once); UNMATCHED_END: an end with no matching open environment.

Heuristic boundary (honest disclosure, no guessing): a ``\\begin{verbatim}`` inside a comment
is treated as a real environment; ``\\\\%`` (line break then comment) is misjudged as a literal % by the escape rule;
neither appears in the anchored corpus; impact is bounded by never-crash corpus regression.
"""

import re

# ── Cap discipline (aligned with the markdown_structure_facts precedent) ──────────────
_SAMPLE_LIMIT = 5           # per-category sample-list cap
_LIST_LIMIT = 20            # cap for registered lists (targets/package names etc.)
_ENV_NAME_LIMIT = 50        # deduplicated environment-name cap
_PACKAGE_LIST_LIMIT = 20    # usepackage package-name sample cap
_TITLE_SNIPPET = 100        # title/author/date registered-value truncation
_COMMENT_SNIPPET = 80       # comment sample truncation
_RAW_TEX_LIMIT = 200        # display formula raw_tex sample truncation
_PROXIMITY = 200            # caption → label association search window
_URL_TARGET_SNIPPET = 200   # external-link target sample truncation (r19 external-link registration)

# ── Math environments (same frozen set as tex_parser v0, single source of truth) ────────
_MATH_ENVIRONMENTS = {
    "equation", "align", "alignat", "flalign", "gather",
    "multline", "eqnarray", "displaymath", "math",
}
_BEGIN_ENV_RE = re.compile(r"\\begin\{([a-zA-Z*]+)\}")

# ── verbatim opaque regions (prereg §2 P1 item 2 frozen list) ─────────────
_VERBATIM_ENVIRONMENTS = (
    "verbatim", "verbatim*", "Verbatim", "lstlisting", "minted",
)
_VERBATIM_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(re.escape(e) for e in _VERBATIM_ENVIRONMENTS) + r")\}"
)
# Main lexical regex: comment / closed verb / unclosed verb / escape pair / lone backslash /
# plain text — six channels in one scan (semantically equal to a per-character walk, C-level speed).
# Branch order is priority: verb before escape pairs (otherwise \verb's v would be swallowed),
# comment before plain text. An escaped \% never enters the comment channel; a line-break \\% is handled as line-break + comment.
_LEXICAL_MASTER_RE = re.compile(
    r"(?P<comment>%[^\n]*)"
    r"|(?P<verb_closed>\\verb\*?(?![a-zA-Z])(?P<verb_d>[^a-zA-Z\s])"
    r"(?P<verb_body>[^\n]*?)(?P=verb_d))"
    r"|(?P<verb_open>\\verb\*?(?![a-zA-Z])[^a-zA-Z\s]?[^\n]*)"
    r"|\\."
    r"|\\\\"
    r"|[^%\\]+"
)
# Escape-pair-stripped view (grouping-depth only: literal braces like \{ don't count)
_ESCAPE_PAIR_RE = re.compile(r"\\.")
_BRACE_TOKEN_RE = re.compile(r"[{}]")
# display delimiter tokens (v0-family semantics, aligned with tex_formulas counting)
_DISPLAY_TOKEN_RE = re.compile(r"(?<!\\)\\\[|(?<!\\)\\\]")
_BLANK_RE = re.compile(r"[^\n]")

# ── census regex family (anchored semantics: counts over the original full text, prereg §5 reconciliation basis) ──
_BEGIN_ANY_RE = re.compile(r"\\begin\{([^}]*)\}")
_END_ANY_RE = re.compile(r"\\end\{([^}]*)\}")
_SECTIONING_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)"
    r"\*?(?![a-zA-Z])"
)
_DOCUMENTCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{([^}]*)\}")
_USEPACKAGE_COUNT_RE = re.compile(r"\\usepackage(?![a-zA-Z])")
_USEPACKAGE_NAME_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}")
_INPUT_REFERENCE_RE = re.compile(
    r"\\(?:input|include|InputIfFileExists)\s*(?:\[[^\]]*\])?\s*\{"
)
_CITE_REF_COUNT_RE = re.compile(
    r"\\(?:cite|ref|eqref|autoref|pageref|Citep|citep|citet|citealp|"
    r"citeauthor|vref)\*?(?![a-zA-Z])"
)
_CITE_COUNT_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|Citep|Citet|Citealp)"
    r"\*?(?![a-zA-Z])"
)
_REF_ARG_RE = re.compile(
    r"\\(?:ref|eqref|autoref|pageref|vref)\*?(?![a-zA-Z])\s*\{([^}]*)\}"
)
_LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
_MACRO_DEF_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|newenvironment)"
    r"\*?\s*(?:\[[^\]]*\])?\s*\{\\([a-zA-Z@]+)\}(?:\[([0-9]+)\])?"
)
_CAPTION_RE = re.compile(r"\\caption\s*(?:\[[^\]]*\])?\s*\{")
_INCLUDEGRAPHICS_RE = re.compile(
    r"\\includegraphics\*?(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
_CATCODE_RE = re.compile(r"\\catcode(?![a-zA-Z])")
_EXTERNAL_IO_RE = re.compile(
    r"\\(?:write18|openin|openout)(?![a-zA-Z])|\\input\s*\|"
)
_TITLE_META_RE = re.compile(r"\\(title|author|date)\s*\{")
_DOCUMENT_BEGIN_RE = re.compile(r"\\begin\{document\}")
# ── r19 external-link target regexes (\href first arg / \url|\path parameters; scheme prefix decides external) ──
_HREF_TARGET_RE = re.compile(
    r"\\href\*?(?![a-zA-Z])\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
_URL_CMD_RE = re.compile(r"\\(?:url|path)\*?(?![a-zA-Z])\s*\{([^}]*)\}")
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

# ── P1 semantic fact layer (tex_p1_semantic_facts_prereg_v1, D-T1~D-T8 all A) ──
_TIKZ_SAMPLE_LIMIT = 3      # tikz environment-body sample cap
_COLUMN_SPEC_SNIPPET = 80   # column-spec parameter verbatim truncation
_FOOTNOTE_SNIPPET = 100     # footnote text sample truncation
# theorem-family built-in whitelist (D-T3: whitelist ∪ newtheorem declaration names in this file)
_THEOREM_FAMILY_BASE = frozenset({
    "theorem", "lemma", "corollary", "proposition", "definition",
    "proof", "remark", "example", "conjecture", "axiom", "fact",
    "notation", "observation", "property", "question", "claim",
    "answer", "exercise", "problem", "solution",
})
# table-family environments (D-T4: count aggregation, no cell extraction)
_TABULAR_FAMILY = (
    "tabular", "tabularx", "longtable", "array", "supertabular",
)
# list environments (P1-C: counting + blanked-view nesting depth)
_LIST_ENVIRONMENTS = ("itemize", "enumerate", "description")

_NEWTHEOREM_NAME_RE = re.compile(r"\\newtheorem\*?\s*\{([a-zA-Z]+)\}")
_NEWENVIRONMENT_NAME_RE = re.compile(r"\\newenvironment\*?\s*\{([a-zA-Z]+)\}")
_TABULAR_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(n + r"\*?" for n in _TABULAR_FAMILY) + r")\}"
)
_ROW_SEP_RE = re.compile(r"\\\\")
_CELL_SEP_RE = re.compile(r"(?<!\\)&")
_TABLE_RULE_RE = re.compile(r"\\(?:(?:top|mid|bottom|cmid)rule|hline)\b")
_MULTICOLUMN_RE = re.compile(r"\\multicolumn(?![a-zA-Z])")
_MULTIROW_RE = re.compile(r"\\multirow(?![a-zA-Z])")
_ITEM_CMD_RE = re.compile(r"\\item(?![a-zA-Z])")
_FOOTNOTE_RE = re.compile(r"\\footnote\s*(?:\[[^\]]*\])?\s*\{")
_APPENDIX_RE = re.compile(r"^[ \t]*\\appendix(?![a-zA-Z])", re.M)
_TABLEOFCONTENTS_RE = re.compile(r"\\tableofcontents(?![a-zA-Z])")
_BIBITEM_COUNT_RE = re.compile(r"\\bibitem(?![a-zA-Z])")
_BIBITEM_KEY_RE = re.compile(r"\\bibitem(?:\[[^\]]*\])?\s*\{([^}]*)\}")
_BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{([^}]*)\}")
_BIBLIOGRAPHYSTYLE_RE = re.compile(r"\\bibliographystyle\s*\{([^}]*)\}")
_ADDBIBRESOURCE_RE = re.compile(
    r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
_EQREF_RE = re.compile(r"\\eqref(?![a-zA-Z])\s*\{")


# ── Helpers: nesting-aware parameter reading (same semantics as tex_parser._read_braced) ──────
def _read_braced(text, start):
    """Read the ``{...}`` content at start (nesting/escape aware); out of range returns (None, start)."""
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return None, start


def _clean_snippet(raw, limit):
    """Conservatively sanitize registered values: strip inline typesetting commands/braces, squeeze whitespace, truncate."""
    text = re.sub(r"\\(?:textbf|textit|texttt|emph|text|textrm)\{([^{}]*)\}", r"\1", raw)
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    text = re.sub(r"[{}$]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:limit]


def _line_of(text, offset):
    """Line number (1-based) containing the offset."""
    return text.count("\n", 0, offset) + 1


# ── Lexical view: verbatim bodies + comments blanked (positions kept, content not) ──────────
def _blanked_view(text):
    """Return (code, census) — code is the same length as text; verbatim bodies and comment regions
    are replaced with spaces (newlines kept); census carries the comment-channel and verbatim facts.
    Unclosed verbatim bodies fall back to plain text for continued scanning (UNCLOSED_VERBATIM).
    """
    census = {
        "verbatim_region_count": 0,
        "unclosed_verbatim_environment_count": 0,
        "inline_verb_count": 0,
        "unclosed_inline_verb_count": 0,
        "comment_count": 0,
        "comment_character_count": 0,
        "comment_join_count": 0,
        "comment_samples": [],
    }

    # pass 1: verbatim environment bodies (located over the original full text; heuristics per the module docstring)
    regions = []
    for m in _VERBATIM_BEGIN_RE.finditer(text):
        if any(s <= m.start() < e for s, e, _ in regions):
            continue  # already inside an opaque region, treated as literal content
        end_marker = "\\end{" + m.group(1) + "}"
        end_index = text.find(end_marker, m.end())
        if end_index < 0:
            census["unclosed_verbatim_environment_count"] += 1
        else:
            regions.append((m.end(), end_index, m.group(1)))
    census["verbatim_region_count"] = len(regions)
    parts = []
    prev = 0
    for start, end, _ in regions:
        parts.append(text[prev:start])
        parts.append(_BLANK_RE.sub(" ", text[start:end]))  # newlines kept
        prev = end
    parts.append(text[prev:])
    verbatim_blanked = "".join(parts)

    # pass 2: one scan of the main lexical regex — comment channel / closed and unclosed
    # \\verb / escape sequences / plain text (semantically equal to a per-character walk, C-level speed):
    # a % inside a \\verb body never enters the comment channel; a \\verb inside a comment is not counted;
    # a \% escape is skipped; a \\% is handled correctly as line-break + comment.
    ranges = []
    for m in _LEXICAL_MASTER_RE.finditer(verbatim_blanked):
        if m.group("comment") is not None:
            census["comment_count"] += 1
            census["comment_character_count"] += len(m.group())
            if m.end() < len(verbatim_blanked) \
                    and verbatim_blanked[m.end()] == "\n":
                census["comment_join_count"] += 1  # trailing comment joins the next line
            if len(census["comment_samples"]) < _SAMPLE_LIMIT:
                census["comment_samples"].append(
                    verbatim_blanked[m.start() + 1:m.end()]
                    .strip()[:_COMMENT_SNIPPET]
                )
            ranges.append((m.start(), m.end()))
        elif m.group("verb_closed") is not None:
            census["inline_verb_count"] += 1
            ranges.append((m.start("verb_body"), m.end("verb_body")))
        elif m.group("verb_open") is not None:
            census["unclosed_inline_verb_count"] += 1
            ranges.append((m.start(), m.end()))
    parts = []
    prev = 0
    for start, end in sorted(ranges):
        parts.append(verbatim_blanked[prev:start])
        parts.append(" " * (end - start))
        prev = end
    parts.append(verbatim_blanked[prev:])
    return "".join(parts), census


# ── State machine: group depth / environment-tree pairing / display delimiters ──────────────
def _scan_state(code):
    """Run the single-pass state machine on the blanked code view; return pairing facts."""
    findings = {
        "group_open_count": 0,
        "group_close_count": 0,
        "group_max_depth": 0,
        "unclosed_group_depth": 0,
        "unmatched_closing_brace_count": 0,
        "environment_max_depth": 0,
        "unclosed_environment_count": 0,
        "unclosed_math_environment_count": 0,
        "unmatched_begin_count": 0,
        "unmatched_end_count": 0,
        "display_delimiter_open_count": 0,
        "unclosed_display_delimiter_depth": 0,
    }

    # Group depth (after escape-pair stripping only real brace events remain; \\{ after a line break still counts as an open brace)
    stripped = _ESCAPE_PAIR_RE.sub("xx", code)
    depth = 0
    for m in _BRACE_TOKEN_RE.finditer(stripped):
        if m.group() == "{":
            findings["group_open_count"] += 1
            depth += 1
            if depth > findings["group_max_depth"]:
                findings["group_max_depth"] = depth
        else:
            findings["group_close_count"] += 1
            if depth == 0:
                findings["unmatched_closing_brace_count"] += 1
            else:
                depth -= 1
    findings["unclosed_group_depth"] = depth

    # Display delimiters (v0-family token regex, aligned with the bracket_display_count
    # counting basis; pairing depth walks the token event stream)
    display_depth = 0
    for m in _DISPLAY_TOKEN_RE.finditer(code):
        if m.group().endswith("["):
            findings["display_delimiter_open_count"] += 1
            display_depth += 1
        else:
            display_depth = max(0, display_depth - 1)
    findings["unclosed_display_delimiter_depth"] = display_depth

    # Environment tree: begin/end event stream + stack pairing (semantic split per the module docstring)
    events = []
    for m in _BEGIN_ANY_RE.finditer(code):
        events.append((m.start(), "begin", m.group(1).strip()))
    for m in _END_ANY_RE.finditer(code):
        events.append((m.start(), "end", m.group(1).strip()))
    events.sort(key=lambda ev: ev[0])
    stack = []
    for _, kind, name in events:
        if kind == "begin":
            stack.append(name)
            if len(stack) > findings["environment_max_depth"]:
                findings["environment_max_depth"] = len(stack)
        else:
            if name in stack:
                while stack and stack[-1] != name:
                    swallowed = stack.pop()
                    findings["unmatched_begin_count"] += 1
                    findings["unclosed_environment_count"] += 1
                    if swallowed.rstrip("*") in _MATH_ENVIRONMENTS:
                        findings["unclosed_math_environment_count"] += 1
                stack.pop()
            else:
                findings["unmatched_end_count"] += 1
    for name in stack:  # still unclosed at EOF
        findings["unclosed_environment_count"] += 1
        if name.rstrip("*") in _MATH_ENVIRONMENTS:
            findings["unclosed_math_environment_count"] += 1
    return findings


# ── census: anchored-semantics counting over the original full text ──────────────────
def _scan_census(text, code):
    """census counts + bounded samples (prereg §5 anchored semantics, raw full text)."""
    census = {}

    census["character_count"] = len(text)
    census["line_count"] = text.count("\n") + 1

    # Environment census (all begins, including those inside comments — anchored basis per the module docstring)
    env_counts = {}
    for m in _BEGIN_ANY_RE.finditer(text):
        name = m.group(1).strip()
        env_counts[name] = env_counts.get(name, 0) + 1
    census["environment_total"] = sum(env_counts.values())
    if len(env_counts) > _ENV_NAME_LIMIT:
        kept = dict(sorted(env_counts.items(), key=lambda kv: -kv[1])[:_ENV_NAME_LIMIT])
        census["environment_counts"] = kept
        census["environment_counts_truncated"] = True
    else:
        census["environment_counts"] = env_counts
    math_total = 0
    for m in _BEGIN_ENV_RE.finditer(text):
        if m.group(1).rstrip("*") in _MATH_ENVIRONMENTS:
            math_total += 1
    census["math_environment_total"] = math_total

    census["sectioning_command_count"] = len(_SECTIONING_RE.findall(text))

    # preamble identity registration
    dc = _DOCUMENTCLASS_RE.search(text)
    census["documentclass"] = dc.group(1).strip() if dc else None
    census["usepackage_count"] = len(_USEPACKAGE_COUNT_RE.findall(text))
    packages = []
    for m in _USEPACKAGE_NAME_RE.finditer(text):
        for pkg in m.group(1).split(","):
            pkg = pkg.strip()
            if pkg and len(packages) < _PACKAGE_LIST_LIMIT:
                packages.append(pkg)
    census["usepackage_packages"] = packages
    for field in ("title", "author", "date"):
        m = _TITLE_META_RE.search(code)
        while m is not None and m.group(1) != field:
            m = _TITLE_META_RE.search(code, m.end())
        if m is not None:
            value, _ = _read_braced(code, m.end() - 1)
            census[field] = _clean_snippet(value, _TITLE_SNIPPET) if value else None
        else:
            census[field] = None
    census["document_begin_present"] = bool(_DOCUMENT_BEGIN_RE.search(code))
    census["is_fragment"] = (
        census["documentclass"] is None and not census["document_begin_present"]
    )

    # Macro-definition registration (name and argument count only, never evaluated)
    macro_defs = [
        {"name": "\\" + m.group(1), "argument_count": int(m.group(2) or 0)}
        for m in _MACRO_DEF_RE.finditer(text)
    ]
    census["macro_definition_count"] = len(macro_defs)
    census["macro_definition_samples"] = macro_defs[:_SAMPLE_LIMIT]

    # Figure/table candidates
    census["figure_environment_count"] = sum(
        env_counts.get(name, 0)
        for name in ("figure", "figure*", "wrapfigure")
    )
    census["table_environment_count"] = sum(
        env_counts.get(name, 0)
        for name in ("table", "table*", "longtable", "tabularx", "supertabular")
    )
    captions = []
    for m in _CAPTION_RE.finditer(text):
        value, end = _read_braced(text, m.end() - 1)
        if value is None:
            continue
        label = None
        proximity = text[end:end + _PROXIMITY]
        lm = _LABEL_RE.search(proximity)
        if lm is not None:
            label = lm.group(1).strip()
        captions.append({
            "text": _clean_snippet(value, _TITLE_SNIPPET),
            "label": label,
        })
    census["caption_count"] = len(captions)
    census["caption_samples"] = captions[:_SAMPLE_LIMIT]
    graphics = [
        m.group(1).strip()
        for m in _INCLUDEGRAPHICS_RE.finditer(text)
    ]
    census["includegraphics_count"] = len(graphics)
    census["includegraphics_targets"] = graphics[:_SAMPLE_LIMIT]

    # Citation graph (label definitions / cite and ref usages / duplicate labels)
    labels = [m.group(1).strip() for m in _LABEL_RE.finditer(text)]
    census["label_definition_count"] = len(labels)
    census["label_samples"] = labels[:_SAMPLE_LIMIT]
    seen, dups = set(), []
    for label in labels:
        if label in seen and label not in dups and len(dups) < _SAMPLE_LIMIT:
            dups.append(label)
        seen.add(label)
    census["duplicate_label_count"] = len(labels) - len(seen)
    census["duplicate_labels"] = dups
    census["citation_command_count"] = len(_CITE_COUNT_RE.findall(text))
    census["citation_and_reference_command_count"] = len(
        _CITE_REF_COUNT_RE.findall(text)
    )
    defined = set(labels)
    external_refs = []
    for m in _REF_ARG_RE.finditer(text):
        for key in m.group(1).split(","):
            key = key.strip()
            if key and key not in defined:
                if len(external_refs) < _LIST_LIMIT:
                    external_refs.append(key)
    census["external_label_reference_count"] = len(external_refs)
    census["external_label_reference_samples"] = external_refs[:_SAMPLE_LIMIT]

    # Input references (anchored semantics: input/include/InputIfFileExists + optional parameters)
    census["input_reference_count"] = len(_INPUT_REFERENCE_RE.findall(text))

    # r19 external-link target registration (\\href first arg + \\url/\\path parameters; scheme prefix decides
    # external; semantics aligned with the EPUB remote / markdown autolink family precedents; raw full-text
    # counting includes comments, same §5 anchored basis; evidence-only, never rewrites text)
    href_targets = [m.group(1).strip() for m in _HREF_TARGET_RE.finditer(text)]
    url_targets = [m.group(1).strip() for m in _URL_CMD_RE.finditer(text)]
    census["hyperlink_command_count"] = len(href_targets) + len(url_targets)
    external_targets = [
        t for t in href_targets + url_targets if _URI_SCHEME_RE.match(t)
    ]
    census["hyperlink_external_count"] = len(external_targets)
    census["hyperlink_external_targets"] = [
        t[:_URL_TARGET_SNIPPET] for t in external_targets[:_SAMPLE_LIMIT]
    ]
    scheme_counts = {}
    for target in external_targets:
        scheme = target.split(":", 1)[0].lower()
        scheme_counts[scheme] = scheme_counts.get(scheme, 0) + 1
    census["hyperlink_external_scheme_counts"] = scheme_counts

    return census


# ── P1 semantic fact layer (tex_p1_semantic_facts_prereg_v1 — pure superset, zero new reason codes) ──
def _environment_spans(text, names):
    """Union of body intervals of an environment-name family (raw full-text pairing; unclosed environments yield no interval)."""
    rx = re.compile(
        r"\\(begin|end)\{(" + "|".join(re.escape(n) for n in names) + r")\*?\}"
    )
    stack, closed = [], []
    for m in rx.finditer(text):
        if m.group(1) == "begin":
            stack.append(m.end())
        elif stack:
            closed.append((stack.pop(), m.start()))
    closed.sort()
    merged = []
    for start, end in closed:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _max_env_nesting(view, names):
    """Maximum nesting depth of the given environment-name family (pairing statistics on the blanked code view)."""
    rx = re.compile(
        r"\\(begin|end)\{(" + "|".join(re.escape(n) for n in names) + r")\*?\}"
    )
    depth = best = 0
    for m in rx.finditer(view):
        if m.group(1) == "begin":
            depth += 1
            best = max(best, depth)
        elif depth:
            depth -= 1
    return best


def _scan_p1_semantics(text, code):
    """P1 semantic classification/depth/bibliography evidence (prereg §2 P1-A~H; all additive)."""
    facts = {}

    # Environment count recomputation (census's environment_counts has a cap truncation; the semantic layer stays decoupled)
    env_counts = {}
    for m in _BEGIN_ANY_RE.finditer(text):
        name = m.group(1).strip()
        env_counts[name] = env_counts.get(name, 0) + 1

    # P1-A theorem family (D-T3: built-in whitelist ∪ newtheorem declaration names in this file)
    declared = []
    for m in _NEWTHEOREM_NAME_RE.finditer(text):
        if m.group(1) not in declared:
            declared.append(m.group(1))
    family = _THEOREM_FAMILY_BASE | set(declared)
    theorem_counts = {
        name: n
        for name, n in env_counts.items()
        if (name[:-1] if name.endswith("*") else name) in family
    }
    facts["theorem_family_total"] = sum(theorem_counts.values())
    facts["theorem_family_counts"] = theorem_counts
    facts["theorem_family_declared_names"] = declared[:_LIST_LIMIT]
    custom_envs = []
    for m in _NEWENVIRONMENT_NAME_RE.finditer(text):
        if m.group(1) not in custom_envs:
            custom_envs.append(m.group(1))
    facts["custom_environment_definitions"] = custom_envs[:_LIST_LIMIT]

    # P1-B table structure (D-T4: family count aggregation + column-spec verbatim samples + row/cell counts)
    tabular_counts = {
        name: n
        for name, n in env_counts.items()
        if (name[:-1] if name.endswith("*") else name) in _TABULAR_FAMILY
    }
    facts["tabular_family_total"] = sum(tabular_counts.values())
    facts["tabular_family_counts"] = tabular_counts
    column_specs = []
    for m in _TABULAR_BEGIN_RE.finditer(text):
        if len(column_specs) >= _SAMPLE_LIMIT:
            break
        pos = m.end()
        if m.group(1).endswith("*"):
            # tabular* form: {width}{column-spec} — skip the width parameter
            _width, width_end = _read_braced(text, pos)
            if _width is None:
                continue
            pos = width_end
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        value, _end = _read_braced(text, pos)
        if value is not None:
            column_specs.append(value.strip()[:_COLUMN_SPEC_SNIPPET])
    facts["table_column_spec_samples"] = column_specs
    rows = cells = 0
    for start, end in _environment_spans(text, _TABULAR_FAMILY):
        body = text[start:end]
        rows += len(_ROW_SEP_RE.findall(body))
        cells += len(_CELL_SEP_RE.findall(body))
    facts["table_row_count"] = rows
    facts["table_cell_separator_count"] = cells
    facts["table_rule_count"] = len(_TABLE_RULE_RE.findall(text))
    facts["multicolumn_count"] = len(_MULTICOLUMN_RE.findall(text))
    facts["multirow_count"] = len(_MULTIROW_RE.findall(text))

    # P1-C lists (counting + blanked-view nesting depth)
    facts["list_environment_counts"] = {
        name: env_counts.get(name, 0) for name in _LIST_ENVIRONMENTS
    }
    facts["list_environment_total"] = sum(
        facts["list_environment_counts"].values()
    )
    facts["item_count"] = len(_ITEM_CMD_RE.findall(text))
    facts["list_max_nesting_depth"] = _max_env_nesting(
        code, _LIST_ENVIRONMENTS
    )

    # P1-D footnotes (counting + bounded cleaned samples)
    footnote_samples = []
    for m in _FOOTNOTE_RE.finditer(text):
        if len(footnote_samples) >= _SAMPLE_LIMIT:
            break
        value, _end = _read_braced(text, m.end() - 1)
        if value:
            footnote_samples.append(_clean_snippet(value, _FOOTNOTE_SNIPPET))
    facts["footnote_count"] = len(_FOOTNOTE_RE.findall(text))
    facts["footnote_samples"] = footnote_samples

    # P1-E appendix and table of contents (appendix records line numbers; TOC records existence only)
    am = _APPENDIX_RE.search(text)
    facts["appendix_present"] = am is not None
    facts["appendix_line"] = _line_of(text, am.start()) if am else None
    facts["tableofcontents_present"] = bool(
        _TABLEOFCONTENTS_RE.search(text)
    )

    # P1-F bibliography evidence (embedded thebibliography / external bibliography / biblatex)
    facts["thebibliography_environment_count"] = env_counts.get(
        "thebibliography", 0
    )
    facts["bibitem_count"] = len(_BIBITEM_COUNT_RE.findall(text))
    facts["bibitem_key_samples"] = [
        key.strip() for key in _BIBITEM_KEY_RE.findall(text)[:_SAMPLE_LIMIT]
    ]
    facts["bibliography_targets"] = [
        t.strip() for t in _BIBLIOGRAPHY_RE.findall(text)
    ][:_SAMPLE_LIMIT]
    facts["bibliographystyle_targets"] = [
        t.strip() for t in _BIBLIOGRAPHYSTYLE_RE.findall(text)
    ][:_SAMPLE_LIMIT]
    facts["addbibresource_targets"] = [
        t.strip() for t in _ADDBIBRESOURCE_RE.findall(text)
    ][:_SAMPLE_LIMIT]

    # P1-G graphics extensions and formula references (tikz samples 200 chars × ≤3; eqref counted separately)
    facts["tikzpicture_count"] = env_counts.get("tikzpicture", 0)
    tikz_samples = []
    for start, end in _environment_spans(text, ("tikzpicture",)):
        if len(tikz_samples) >= _TIKZ_SAMPLE_LIMIT:
            break
        tikz_samples.append(text[start:end].strip()[:_RAW_TEX_LIMIT])
    facts["tikzpicture_samples"] = tikz_samples
    facts["eqref_count"] = len(_EQREF_RE.findall(text))

    return facts


# ── Risk registration (catcode / external I/O) ───────────────────────────────
def _scan_risks(text):
    """Risk keys: declarative counts + line-number samples + executed always False."""
    catcode_lines = []
    for m in _CATCODE_RE.finditer(text):
        if len(catcode_lines) < _SAMPLE_LIMIT:
            catcode_lines.append(_line_of(text, m.start()))
    io_samples = []
    for m in _EXTERNAL_IO_RE.finditer(text):
        if len(io_samples) < _SAMPLE_LIMIT:
            io_samples.append(text[m.start():m.start() + 60].split("\n")[0])
    return {
        "catcode_changes": {
            "count": len(_CATCODE_RE.findall(text)),
            "line_samples": catcode_lines,
            "confidence": "declaration-only",
        },
        "external_io_references": {
            "count": len(_EXTERNAL_IO_RE.findall(text)),
            "samples": io_samples,
            "executed": False,
        },
    }


# ── Single-pass scan main function ─────────────────────────────────────────────
def _scan_tex_structure(text):
    """Scan main entry: returns the complete tex_structure_v2 fact dict (pure function)."""
    code, lexical = _blanked_view(text)
    structure = {"extractor": "tex_structure_v2"}
    structure.update(lexical)
    structure.update(_scan_state(code))
    structure.update(_scan_census(text, code))
    structure.update(_scan_p1_semantics(text, code))
    structure.update(_scan_risks(text))
    structure["note"] = (
        "single-pass lexical census over the authored source (comments "
        "included, counts follow the prereg §5 anchor semantics); the "
        "LaTeX source stays in the narrative text verbatim "
        "(evidence-first, no stripping)"
    )
    return structure


# ── Tri-state derived view ───────────────────────────────────────────────────────
def derive_tex_parse_status(metadata):
    """Derive the document-level tri-state from registered evidence keys — reasons in frozen order (see module docstring).

    failed = ``parse_failed`` truthy; partial = any active-subset reason hit;
    complete = neither. The status value domain follows the lowercase txt/markdown precedent.
    """
    reasons = []

    st = metadata.get("tex_structure")
    if not isinstance(st, dict):
        st = {}

    if st.get("unclosed_group_depth"):
        reasons.append("UNCLOSED_GROUP")
    if st.get("unclosed_environment_count"):
        reasons.append("UNCLOSED_ENVIRONMENT")
    if st.get("unclosed_math_environment_count") or \
            st.get("unclosed_display_delimiter_depth"):
        reasons.append("UNCLOSED_MATH")
    if st.get("unclosed_verbatim_environment_count") or \
            st.get("unclosed_inline_verb_count"):
        reasons.append("UNCLOSED_VERBATIM")
    if st.get("unmatched_begin_count"):
        reasons.append("UNMATCHED_BEGIN")
    if st.get("unmatched_end_count"):
        reasons.append("UNMATCHED_END")
    if st.get("duplicate_label_count"):
        reasons.append("DUPLICATE_LABEL")

    sanitization = metadata.get("sanitization")
    if (isinstance(sanitization, dict)
            and (sanitization.get("replacement_injected") or 0) > 0) \
            or metadata.get("encoding_fallback"):
        reasons.append("INVALID_ENCODING")

    catcode = st.get("catcode_changes")
    if isinstance(catcode, dict) and catcode.get("count"):
        reasons.append("CATCODE_CHANGED")
    io = st.get("external_io_references")
    if isinstance(io, dict) and io.get("count"):
        reasons.append("EXTERNAL_IO_BLOCKED")

    # The scan-failure key is written by collect_tex_structure_facts into the nested
    # metadata["tex_structure"]["tex_structure_error"] (no top-level copy) —
    # the nested key must be read here, otherwise a scan failure silently misreports complete.
    if st.get("tex_structure_error"):
        reasons.append("TEX_STRUCTURE_ERROR")

    if metadata.get("parse_failed"):
        return {"status": "failed", "reasons": ["PARSE_FAILED", *reasons]}
    if reasons:
        return {"status": "partial", "reasons": reasons}
    return {"status": "complete", "reasons": []}


# ── Main entry (parser wiring point) ─────────────────────────────────
def collect_tex_structure_facts(text, metadata):
    """TeX static-structure evidence registration main entry (evidence-only, parser wiring point).

    A scan failure degrades silently to ``tex_structure_error``; the derived
    ``tex_parse_status`` view is always written last. The whole never throws and never rewrites text.
    Returns the scan artifacts for tex_parser to assemble the v2 superset.
    """
    try:
        structure = _scan_tex_structure(text)
        metadata["tex_structure"] = structure
    except Exception as exc:  # pragma: no cover - defensive
        metadata["tex_structure"] = {
            "extractor": "tex_structure_v2",
            "tex_structure_error": f"scan: {exc}",
        }
        structure = {}
    try:
        metadata["tex_parse_status"] = derive_tex_parse_status(metadata)
    except Exception as exc:  # pragma: no cover - defensive
        metadata["tex_parse_status"] = {
            "status": "partial",
            "reasons": ["TEX_STRUCTURE_ERROR"],
        }
        metadata["tex_structure"]["tex_structure_error"] = f"derive: {exc}"
    return structure
