"""LaTeX → Unicode math transcription (the engine behind
RenderedTex)."""

import re
from dataclasses import dataclass, field

from provendoc.core.representations.narrative.tex_env_catalog import (  # noqa: F401
    _CMD_RE,
    _INCLUDEGRAPHICS_RE,
    _TAG_RE,
    _TEXT_ARG_CMDS_RE,
)
from provendoc.core.representations.narrative.tex_macro_expansion import (  # noqa: F401
    _ACCENTS,
    _MATH_FONT_RE,
    _MATHBB,
    _MATHCAL,
    _NOT_COMBOS,
    _SCRIPT_RE,
    _SUBSCRIPTS,
    _SUPERSCRIPTS,
    _SYMBOL_RE,
    _SYMBOLS,
    _expand_macros_in,
    _read_braced,
)

# ============================================================
# rendered result
# ============================================================


@dataclass
class RenderedTex:
    """Output of render_tex_narrative: text + candidates + spans
    + evidence."""

    text: str = ""
    heading_candidates: list = field(default_factory=list)
    display_math_blocks: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    fallback: bool = False


# ============================================================
# math transcription (LaTeX → Unicode)
# ============================================================



def _tex_in_comment(src, pos):
    r"""Whether the token at pos sits in an unescaped % comment
    (TeX comment rules)."""
    line_start = src.rfind("\n", 0, pos) + 1
    seg = src[line_start:pos]
    i = 0
    while True:
        j = seg.find("%", i)
        if j < 0:
            return False
        k, nb = j - 1, 0
        while k >= 0 and seg[k] == "\\":
            nb += 1
            k -= 1
        if nb % 2 == 0:
            return True
        i = j + 1


# multicolumn/multirow cell heads (first two arg groups
# stripped, content group follows)
_CELL_HEAD_RES = (
    re.compile(r"\\multicolumn\s*\{[^{}]*\}\s*\{[^{}]*\}\s*(?=\{)"),
    re.compile(r"\\multirow\s*\{[^{}]*\}\s*\{[^{}]*\}\s*(?=\{)"),
)


def _tabular_cell_text(cell):
    r"""Tabular cell preprocessing (called after macro expansion).

    \multicolumn{n}{fmt}{content} / \multirow{n}{*}{content} →
    the third argument content (nested up to 3 levels);
    \cite{key} → [key].
    """
    c = cell
    for _ in range(3):
        base = c.lstrip()
        for head_re in _CELL_HEAD_RES:
            m = head_re.match(base)
            if m is None:
                continue
            content, _end = _read_braced(base, m.end())
            if content is not None:
                c = content
                break
        else:
            break
    # normalize non-breaking spaces / quote ligatures; literal
    # escapes (\% \{ etc.) are left for transcribe_math — a
    # bare % would hit its comment stripping
    c = c.replace("~", " ").replace("``", "\u201c").replace("''", "\u201d")
    # strip pure-typography \rule[raise]{w}{h} struts (cells
    # bypass _dispatch_cmd)
    c = re.sub(
        r"\\rule\s*(?:\[[^\]\n]*\])?\s*\{[^{}]*\}\s*\{[^{}]*\}",
        " ", c)
    # strip per-cell tabular wrapper shells (cell-macro
    # expansion products) and normalize in-shell \\ to spaces;
    # the normalization must stay inside the shell — a global
    # sub would also kill array-cell row separators
    def _strip_cell_tabular_shell(m):
        inner = re.sub(r"\\\\\*?(?:\s*\[[^\]]*\])?\s*", " ", m.group(1))
        return " " + inner + " "

    c = re.sub(
        r"(?s)\\begin\s*\{tabular\*?\}\s*(?:\[[^\]]*\])?"
        r"\s*\{(?:[^{}]|\{[^{}]*\})*\}(.*?)\\end\s*\{tabular\*?\}",
        _strip_cell_tabular_shell, c)
    return re.sub(r"\\(?:cite|citep|citet)\s*\{([^{}]*)\}", r"[\1]", c)


def _skip_ws(text, pos):
    """Skip whitespace from pos; return the new position."""
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _absorb_scripts(text, tok, end):
    """Absorb _/^ scripts trailing a single token into it.

    Keeps single-token fraction numerators (α followed by _S)
    from breaking argument reads; _apply_scripts takes over
    later. Returns (token, end).
    """
    while end < len(text) and text[end] in "_^":
        marker = text[end]
        end2 = end + 1
        # script argument: {...} / \cmd / single char (incl. Greek)
        if end2 < len(text) and text[end2] == "{":
            arg, aend = _read_braced(text, end2)
            if arg is None:
                break
            tok += marker + "{" + arg + "}"
            end = aend
        elif end2 < len(text) and text[end2] == "\\":
            tm = re.match(r"[a-zA-Z]+", text[end2 + 1:])
            if tm is None:
                break
            tok += marker + text[end2:end2 + 1 + tm.end()]
            end = end2 + 1 + tm.end()
        elif end2 < len(text) and text[end2] not in " \t\r\n":
            tok += marker + text[end2]
            end = end2 + 1
        else:
            break
    return tok, end


def _read_math_arg(text, pos, permissive=False):
    """Read a math command argument: a {...} balanced group or
    a single token.

    permissive=False limits bare tokens to
    alphanumerics/!?/Greek (symbol-table transcription runs
    first); permissive=True accepts any single char or \\cmd
    verbatim. Returns (content, end) or (None, pos).
    """
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        return None, pos
    if text[pos] == "{":
        return _read_braced(text, pos)
    if permissive:
        if text[pos] == "\\":
            tm = re.match(r"[a-zA-Z]+", text[pos + 1:])
            if tm is not None:
                return _absorb_scripts(
                    text, text[pos:pos + 1 + tm.end()],
                    pos + 1 + tm.end())
            return None, pos
        if text[pos] == "}":
            return None, pos
        return _absorb_scripts(text, text[pos], pos + 1)
    mt = re.match(r"[0-9A-Za-z!?\u0391-\u03c9]", text[pos:])
    if mt is not None:
        return _absorb_scripts(text, mt.group(0), pos + 1)
    return None, pos


def _needs_parens(rendered_arg):
    """Whether a rendered fraction operand needs parentheses."""
    if not rendered_arg:
        return False
    # single token (identifier/digit/char incl. Greek): no parens
    if re.fullmatch(r"[\w.\-]{1,3}", rendered_arg, flags=re.UNICODE):
        return False
    # plain numbers (with decimal point): no parens
    if re.fullmatch(r"[0-9.]+", rendered_arg):
        return False
    return True


def _wrap_arg(rendered_arg):
    if _needs_parens(rendered_arg):
        return "(" + rendered_arg + ")"
    return rendered_arg


def _font_repl(m):
    cmd, content = m.group(1), m.group(2)
    base = cmd.replace("*", "")
    if base == "mathbb":
        return _MATHBB.get(content.strip(), content)
    if base in ("mathcal", "mathscr"):
        # mathscr reuses the cal mapping (BMP-safe subset;
        # others keep the literal)
        return _MATHCAL.get(content.strip(), content)
    if base in _ACCENTS:
        # attach the combining mark to the first alphanumeric
        # char; if the content contains a command the mark would
        # land inside its name, so fall back to bare content
        if "\\" not in content:
            first = re.search(r"[a-zA-Z0-9]", content)
            if first is not None:
                k = first.start()
                return content[:k + 1] + _ACCENTS[base] + content[k + 1:]
        if len(content) == 1:
            return content + _ACCENTS[base]
    return content


def _apply_scripts(s):
    # double-braced shells _{{X}} / ^{{X}} → _{X} (≤3 rounds)
    for _ in range(3):
        new = re.sub(r"([_^])\s*\{\s*(\{[^{}]*\})\s*\}", r"\1\2", s)
        if new == s:
            break
        s = new

    # same-base scripts normalized to subscript-first (reading
    # order); only adjacent pairs are swapped, nested groups
    # are left alone
    for _ in range(2):
        new = re.sub(
            r"\^\s*(\{(?:[^{}]|\{[^{}]*\})*\}|[a-zA-Z0-9])"
            r"\s*_\s*(\{(?:[^{}]|\{[^{}]*\})*\}|[a-zA-Z0-9])",
            r"_\2^\1",
            s,
        )
        if new == s:
            break
        s = new

    def repl(m):
        op, grp, single = m.group(1), m.group(2), m.group(3)
        table = _SUPERSCRIPTS if op == "^" else _SUBSCRIPTS
        if grp is None:
            # single char: mapped → Unicode; unmapped stays literal op+c
            if not single:
                return op
            mapped = table.get(single, "")
            return mapped if mapped else op + single
        content = grp
        mapped = "".join(table.get(c, "") for c in content)
        if len(mapped) == len(content):
            return mapped
        # pure grouping shell {X} → unwrap one level and remap
        shell = re.fullmatch(r"\{([^{}]*)\}", content)
        if shell is not None:
            inner = shell.group(1)
            mapped2 = "".join(table.get(c, "") for c in inner)
            if len(mapped2) == len(inner):
                return mapped2
        # unmappable brace group: keep the structural op{content}
        # form; brace placeholders survive the later
        # brace-deletion step and are restored afterwards
        content = content.replace("{", "\x01").replace("}", "\x02")
        return op + "\x01" + content + "\x02"
    return _SCRIPT_RE.sub(repl, s)


_TEXT_MODE_OLD_FONTS = {
    # old-style font declarations inside \text{}/\mbox{} →
    # modern equivalents (MathJax text mode doesn't support
    # \rm/\tt etc.)
    "tt": "\\texttt",
    "rm": "\\textrm",
    "it": "\\textit",
    "bf": "\\textbf",
    "sf": "\\textsf",
    "sc": "\\textsc",
}

_TEXT_MODE_OLD_FONT_RE = re.compile(
    r"\{\s*\\(tt|rm|it|bf|sf|sc)(?![a-zA-Z])\s+"
    r"((?:[^{}]|\{[^{}]*\})*)\}"
)

_TEXT_SHELL_RE = re.compile(r"\\(?:text|mbox|textrm|texttt)\s*\{")


def _modernize_text_mode_fonts(s, _depth=0):
    r"""Normalize old font declarations inside balanced
    \text{}/\mbox{}/\textrm{}/\texttt{} bodies (recursion depth
    ≤ 4). Math outside the shells is untouched."""
    if _depth > 4 or "\\" not in s:
        return s
    out = []
    pos = 0
    for m in _TEXT_SHELL_RE.finditer(s):
        if m.start() < pos:
            continue  # nested inner consumed by an outer shell
        body, end = _read_braced(s, m.end() - 1)
        if body is None:
            continue
        out.append(s[pos:m.start()])
        inner = _modernize_text_mode_fonts(body, _depth + 1)
        inner = _TEXT_MODE_OLD_FONT_RE.sub(
            lambda mm: _TEXT_MODE_OLD_FONTS[mm.group(1)]
            + "{" + mm.group(2) + "}",
            inner,
        )
        out.append(s[m.start():m.end()] + inner + "}")
        pos = end
    if not out:
        return s
    out.append(s[pos:])
    return "".join(out)


def _has_grouped_amp(s):
    r"""Whether any & sits inside a brace group deeper than the
    innermost open alignment environment.

    TeX accepts such hidden alignment chars, MathJax reports
    Misplaced &. The stack records the brace depth at each
    \begin{env}; escaped pairs are skipped.
    """
    _env_re = re.compile(r"\\(begin|end)\s*\{([^{}]*)\}")

    def _is_align(env):
        e = env.strip()
        if e.rstrip("*") in (
            "align", "aligned", "alignat", "alignedat", "split",
            "cases", "array", "eqnarray", "subarray", "smallmatrix",
        ):
            return True
        return "matrix" in e

    stack = []  # [(env, depth_at_begin), ...]
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            m = _env_re.match(s, i)
            if m and _is_align(m.group(2)):
                if m.group(1) == "begin":
                    stack.append((m.group(2).strip(), depth))
                elif stack and stack[-1][0] == m.group(2).strip():
                    stack.pop()
                i = m.end()
                continue
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
        elif c == "&" and stack and depth > stack[-1][1]:
            return True
        i += 1
    return False


_XYMATRIX_OPTS_RE = re.compile(
    r"(?:@[0-9]+\s*|@[A-Za-z]+\s*=\s*[-0-9A-Za-z.]+\s*)+")


def _parse_xymatrix_cell(cell):
    r"""Split an xymatrix cell → (content, [(dir, label), ...]).

    Only orthogonal single-direction arrows \ar[r|d] are
    accepted (labels via ^{..}/_{..} or a bare token); decorated
    or diagonal forms return None → whole-figure fallback.
    """
    arrows = []
    buf = []
    i = 0
    n = len(cell)
    while i < n:
        if (
            cell.startswith("\\ar", i)
            and (i + 3 >= n or not cell[i + 3].isalpha())
        ):
            j = i + 3
            while j < n and cell[j] in " \t":
                j += 1
            if j < n and cell[j] == "@":
                return None  # decorated arrows (\ar@{-->}) unsupported
            if j >= n or cell[j] != "[":
                return None
            k = cell.find("]", j)
            if k < 0:
                return None
            dirs = cell[j + 1 : k].replace(" ", "")
            if len(dirs) != 1 or dirs not in "rd":
                return None
            q = k + 1
            while q < n and cell[q] in " \t":
                q += 1
            label = None
            if q < n and cell[q] in "^_":
                q += 1
                if q < n and cell[q] == "-":
                    q += 1
                while q < n and cell[q] in " \t":
                    q += 1
                if q < n and cell[q] == "{":
                    grp, qe = _read_braced(cell, q)
                    if grp is None:
                        return None
                    label = grp
                    q = qe
                elif q < n and cell[q] == "\\":
                    r_ = q + 1
                    while r_ < n and cell[r_].isalpha():
                        r_ += 1
                    if r_ == q + 1:
                        return None
                    label = cell[q:r_]
                    q = r_
                elif q < n:
                    label = cell[q]
                    q += 1
                else:
                    return None
            arrows.append((dirs, label))
            i = q
            continue
        buf.append(cell[i])
        i += 1
    return "".join(buf), arrows


def _rewrite_xymatrix(s):
    r"""Rewrite \xymatrix into a MathJax-renderable arrow array.

    Horizontal arrows become \xrightarrow{...} between
    columns, vertical ones a \Big\downarrow row below. Only
    orthogonal single-direction arrow grids are supported;
    anything else returns None → the caller falls back to a
    verbatim block.
    """
    m = re.match(r"\s*\\xymatrix\s*", s)
    if not m:
        return None
    pos = m.end()
    om = _XYMATRIX_OPTS_RE.match(s, pos)
    if om:
        pos = om.end()
    if pos >= len(s) or s[pos] != "{":
        return None
    body, bend = _read_braced(s, pos)
    if body is None or s[bend:].strip():
        return None
    rows = re.split(r"\\\\(?=[\s&]|$)", body)
    grid = []
    for row in rows:
        cells = []
        for cell in row.split("&"):
            parsed = _parse_xymatrix_cell(cell)
            if parsed is None:
                return None
            cells.append(parsed)
        if not cells:
            return None
        grid.append(cells)
    ncols = max(len(r) for r in grid)
    if ncols < 1:
        return None
    grid = [r + [("", [])] * (ncols - len(r)) for r in grid]
    out_rows = []
    for ri, row in enumerate(grid):
        parts = []
        has_d = False
        vcells = [""] * (2 * ncols - 1)
        for ci, (content, arrows) in enumerate(row):
            hlab = None
            for d, lab in arrows:
                if d == "r":
                    if ci + 1 >= ncols:
                        return None  # horizontal arrow out of range
                    hlab = lab if lab is not None else hlab
                else:  # d == "d"
                    has_d = True
                    if ri + 1 >= len(grid):
                        return None  # vertical arrow out of range
                    if lab:
                        vcells[2 * ci] = (
                            "{\\scriptstyle " + lab + "}\\Big\\downarrow")
                    else:
                        vcells[2 * ci] = "\\Big\\downarrow"
            parts.append(content.strip())
            if ci + 1 < ncols:
                if hlab is not None or any(
                    d == "r" for d, _ in arrows
                ):
                    if hlab:
                        parts.append(
                            "\\xrightarrow{\\; " + hlab + " \\;}")
                    else:
                        parts.append("\\longrightarrow")
                else:
                    parts.append("")
        out_rows.append(" & ".join(parts))
        if has_d:
            out_rows.append(" & ".join(vcells))
    colspec = "c" * (2 * ncols - 1)
    return (
        "\\begin{array}{" + colspec + "} "
        + " \\\\".join(out_rows) + " \\end{array}"
    )


def _clean_math_latex(src, evidence=None, macros=None):
    r"""Minimal, lossless cleanup of raw LaTeX math (no Unicode
    transcription).

    Strips only render-irrelevant noise: author comments
    (%…), \label, math-mode \includegraphics. Everything else
    is kept verbatim for MathJax/KaTeX; author macros are
    expanded deterministically.
    """
    if evidence is None:
        evidence = {}
    s = src
    if macros:
        s = _expand_macros_in(s, macros, evidence)
    # superscript \dag/\ddag → †/‡ (MathJax doesn't know \dag);
    # before script handling
    s = re.sub(r"([_^])\{\\(?:dag|dagger)\}", "\u2020", s)
    s = re.sub(r"([_^])\{\\(?:ddag|ddagger)\}", "\u2021", s)
    # plain TeX \hbox{\vtop{\offinterlineskip\halign{...}}}
    # table construct → single-column \begin{array}{c} ...;
    # the template row (# placeholders) is dropped. Must run
    # before the _TEXT_ARG_CMDS_RE masking (\hbox would be
    # absorbed as text mode).
    _HALIGN_RE = re.compile(
        r"\\hbox\s*\{\s*\\vtop\s*\{\s*\\offinterlineskip\s*"
        r"\\halign\s*\{")
    _CLOSE2_RE = re.compile(r"\s*\}\s*\}")
    _ha_pos = 0
    _ha_out = []
    while True:
        _hm = _HALIGN_RE.search(s, _ha_pos)
        if not _hm:
            break
        _hb, _hb_end = _read_braced(s, _hm.end() - 1)
        if _hb is None:
            _ha_pos = _hm.end()
            continue
        _cm = _CLOSE2_RE.match(s, _hb_end)
        _ha_end = _cm.end() if _cm else _hb_end
        _rows = _hb.split("\\cr")
        if _rows and "#" in _rows[0]:
            _rows = _rows[1:]
        _rows = [re.sub(r"\$", "", _r).strip() for _r in _rows]
        _rows = [_r for _r in _rows if _r]
        _repl = ""
        if _rows:
            _repl = ("\\begin{array}{c} " + " \\\\ ".join(_rows)
                     + " \\end{array}")
        _ha_out.append(s[_ha_pos:_hm.start()])
        _ha_out.append(_repl)
        _ha_pos = _ha_end
    _ha_out.append(s[_ha_pos:])
    s = "".join(_ha_out)
    # normalize old font declarations in text mode (math mode
    # untouched)
    s = _modernize_text_mode_fonts(s)
    # \raisebox{dim}{content} shell → keep content (nested
    # forms unwrap inner-first; the balanced read avoids
    # set-brace interference)
    _RAISEBOX_HEAD_RE = re.compile(r"\\raisebox\s*\{[^{}]*\}\s*\{")
    for _ in range(4):
        _rb_out = []
        _rb_pos = 0
        _rb_changed = False
        while True:
            _rm = _RAISEBOX_HEAD_RE.search(s, _rb_pos)
            if not _rm:
                break
            _rc, _rb_end = _read_braced(s, _rm.end() - 1)
            if _rc is None:
                _rb_pos = _rm.end()
                continue
            _rb_out.append(s[_rb_pos:_rm.start()])
            _rb_out.append(_rc)
            _rb_pos = _rb_end
            _rb_changed = True
        if not _rb_changed:
            break
        _rb_out.append(s[_rb_pos:])
        s = "".join(_rb_out)
    # bare \raisebox{dim} (no content group) → drop
    s = re.sub(r"\\raisebox\s*\{[^{}]*\}", " ", s)
    # \frac\textrm{i}{2} authoring slip → \frac{\textrm{i}}{2};
    # also the unbraced single-token denominator form
    s = re.sub(r"\\frac\s*\\textrm\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
               r"\\frac{\\textrm{\1}}{\2}", s)
    s = re.sub(r"\\frac\s*\\textrm\s*\{([^{}]*)\}\s+([A-Za-z0-9.])(?![A-Za-z0-9.])",
               r"\\frac{\\textrm{\1}}{\2}", s)
    # font-size declarations and bare $ pairs are cleaned only
    # outside text shells (\mbox/\text bodies stay legal);
    # mask the text bodies, operate on the masked string, then
    # restore
    _tx_frags = []
    _tx_out = []
    _tx_pos = 0
    while True:
        _tm = _TEXT_ARG_CMDS_RE.search(s, _tx_pos)
        if not _tm:
            break
        _tb, _tb_end = _read_braced(s, _tm.end() - 1)
        if _tb is None:
            _tx_pos = _tm.end()
            continue
        _tx_out.append(s[_tx_pos:_tm.end() - 1])
        _tx_frags.append(_tb)
        _tx_out.append("\x00e%d\x00" % (len(_tx_frags) - 1))
        _tx_pos = _tb_end
    if _tx_frags:
        _tx_out.append(s[_tx_pos:])
        _tx_s = "".join(_tx_out)
    else:
        _tx_s = s
    # font-size declarations are MathJax-unsupported noise in
    # math mode; (?![a-zA-Z]) avoids \smallmatrix/psmallmatrix
    _tx_s = re.sub(
        r"\\(?:footnotesize|scriptsize|small|normalsize|large|"
        r"Large|LARGE|huge|Huge|tiny)(?![a-zA-Z])", " ", _tx_s)
    # bare $...$ pairs (raisebox-unwrapping residue) → unwrap
    # to content; empty ones dropped
    def _unwrap_bare_dollar(m):
        return m.group(1) if m.group(1).strip() else " "
    _tx_s = re.sub(r"\$([^$]*)\$", _unwrap_bare_dollar, _tx_s)
    if _tx_frags:
        for _ti, _tf in enumerate(_tx_frags):
            _tx_s = _tx_s.replace("\x00e%d\x00" % _ti, "{" + _tf + "}")
    s = _tx_s
    # strip comments (escaped \% unaffected)
    s = re.sub(r"(?<!\\)%[^\n]*", "", s)
    s = re.sub(r"\\label\{[^{}]*\}", "", s)
    # mathtools bracketed small-matrix variants (p/b/v/B/V) →
    # standard matrix envs (plain smallmatrix is natively
    # supported)
    s = re.sub(r"\\(begin|end)\{([pbvBV])smallmatrix\}",
               r"\\\1{\2matrix}", s)
    # math-mode graphics references → drop, counted in
    # graphics evidence
    _mg = _INCLUDEGRAPHICS_RE.findall(s)
    if _mg:
        evidence["graphics"] = evidence.get("graphics", 0) + len(_mg)
        s = _INCLUDEGRAPHICS_RE.sub(" ", s)
    # normalize line breaks/indentation (LaTeX ignores newlines)
    s = re.sub(r"\s+", " ", s).strip()
    # a trailing lone \ would fuse with the rebuilt \end{env}/$
    # into \\end → restore the control space
    if s.endswith("\\"):
        s += " "
    return s


def transcribe_math(src, display=True, evidence=None, depth=0,
                    macros=None):
    """LaTeX math source → Unicode narrative text.

    Unknown commands stay verbatim (counted in evidence); depth
    is the recursion guard (\\frac nesting cap); macros is the
    deterministic-expansion macro table.
    """
    if evidence is None:
        evidence = {}
    if depth > 12:
        return src
    s = src

    # --- deterministic macro expansion (round/budget guards) ---
    if macros:
        s = _expand_macros_in(s, macros, evidence)

    # strip comments (escaped \% unaffected)
    s = re.sub(r"(?<!\\)%[^\n]*", "", s)

    # math-mode graphics references → drop, counted in graphics
    # evidence
    _mg = _INCLUDEGRAPHICS_RE.findall(s)
    if _mg:
        evidence["graphics"] = evidence.get("graphics", 0) + len(_mg)
        s = _INCLUDEGRAPHICS_RE.sub(" ", s)

    # --- harmless deletions: numbering/style/delimiter size/env marks ---
    s = re.sub(r"\\label\{[^{}]*\}", "", s)
    # (?![a-zA-Z]) instead of \b: _ is a word char, so \b would
    # never match after \limits
    s = re.sub(r"\\(?:nonumber|notag|displaystyle|textstyle|"
               r"scriptstyle|scriptscriptstyle|limits|nolimits|"
               r"allowbreak|nobreak|qedhere)(?![a-zA-Z])", "", s)
    # plain TeX layout primitives (halign tables etc.) → drop;
    # invisible strut boxes too
    s = re.sub(
        r"\\(?:halign|valign|offinterlineskip|hfil{1,2}|vfil{1,2}|"
        r"crcr|cr|noalign|omit|span|tabskip|hsize|vsize|strut|"
        r"leftline|rightline|centerline)(?![a-zA-Z])",
        " ", s,
    )
    # reference commands in math mode (brace deletion would
    # fuse \eqref{eq:x})
    s = re.sub(r"\\eqref\{([^{}]*)\}", r"([\1])", s)
    s = re.sub(r"\\ref\{([^{}]*)\}", r"[\1]", s)
    # \tag{...} → (number); \substack{a\\b} multi-line
    # subscript stack → semicolon-flattened
    s = re.sub(
        r"\\tag\*?\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
        r"(\1)", s,
    )
    s = re.sub(
        r"\\substack\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
        lambda m: re.sub(r"\\\\\s*", ";", m.group(1)),
        s,
    )
    # unwrap \text{...} shells (inner $ delimiters stripped);
    # _read_braced handles arbitrary nesting where a one-level
    # regex would fail and leak \textif fusions
    _t_out = []
    _t_pos = 0
    for _tm in re.finditer(r"\\text\s*\{", s):
        if _tm.start() < _t_pos:
            continue  # nested inner consumed by an outer \text shell
        _tc, _t_end = _read_braced(s, _tm.end() - 1)
        if _tc is None:
            continue
        _t_out.append(s[_t_pos:_tm.start()])
        _t_out.append(re.sub(r"(?<!\\)\$", " ", _tc))
        _t_pos = _t_end
    if _t_out:
        _t_out.append(s[_t_pos:])
        s = "".join(_t_out)
    # drop balanced \hbox/\vtop/\vbox/\vcenter shells (≤3
    # rounds for nested inners); \raisebox keeps only its
    # content
    _BOX_HEAD_RE = re.compile(
        r"\\(?:hbox|vbox|vtop|vcenter)\b"
        r"(?:\s+(?:to|spread)\s*[-\d.]+\w*)?\s*\{"
    )
    for _ in range(3):
        _b_out = []
        _b_pos = 0
        _b_changed = False
        for _bm in _BOX_HEAD_RE.finditer(s):
            if _bm.start() < _b_pos:
                continue
            _bc, _b_end = _read_braced(s, _bm.end() - 1)
            if _bc is None:
                continue
            _b_out.append(s[_b_pos:_bm.start()])
            _b_out.append(_bc)
            _b_pos = _b_end
            _b_changed = True
        if not _b_changed:
            break
        _b_out.append(s[_b_pos:])
        s = "".join(_b_out)
    s = re.sub(r"\\raisebox\s*\{[^{}]*\}", "", s)
    # declarative font switches and size families (math mode
    # too)
    s = re.sub(r"\\(?:bf|it|sl|sc|tt|rm|sf)(?![a-zA-Z])", "", s)
    s = re.sub(
        r"\\(?:tiny|scriptsize|footnotesize|small|normalsize|"
        r"large|Large|LARGE|huge|Huge)(?![a-zA-Z])", "", s,
    )
    # old-style \cal switch → _MATHCAL (unknown letters stay
    # plain)
    s = re.sub(
        r"\\cal\s+([a-zA-Z])",
        lambda m: _MATHCAL.get(m.group(1), m.group(1)), s,
    )
    # brace-less font declarations (\mathbf 1_E) apply to the
    # next token → keep the char
    s = re.sub(
        r"\\(?:mathbf|mathrm|mathit|mathsf|mathtt|mathcal|"
        r"mathbb|mathfrak|mathscr|boldsymbol|bm)\s+"
        r"([a-zA-Z0-9])",
        r"\1", s,
    )
    # brace-less accents → char + combining mark (underline →
    # U+0332)
    def _bare_accent_repl(m):
        name, ch = m.group(1), m.group(2)
        mark = {"underline": "\u0332"}.get(name, _ACCENTS.get(name))
        return ch + mark if mark else ch

    s = re.sub(
        r"\\(hat|widehat|bar|overline|underline|tilde|widetilde|"
        r"vec|dot|ddot|acute|grave|check|breve)\s+([a-zA-Z0-9])",
        _bare_accent_repl, s,
    )
    s = re.sub(r"\\[bB]ig{1,2}[lmrt]?\b", "", s)
    s = re.sub(r"\\(?:left|right)\b", "", s)
    # graphics environments inside math → drop the whole
    # block, counted in graphics evidence
    _mgenv = re.findall(
        r"\\begin\s*\{((?:tikzpicture|tikzcd|pspicture|xy)\*?)\}"
        r"(?s:.*?)\\end\s*\{\1\}", s,
    )
    if _mgenv:
        evidence["graphics"] = evidence.get("graphics", 0) \
            + len(_mgenv)
        s = re.sub(
            r"\\begin\s*\{((?:tikzpicture|tikzcd|pspicture|xy)\*?)\}"
            r"(?s:.*?)\\end\s*\{\1\}", " ", s,
        )
    # swallow array/tabular column specs with the \begin{...}
    s = re.sub(
        r"\\begin\s*\{(?:array|tabular)\*?\}\s*\{[^{}]*\}",
        "\n" if display else " ", s,
    )
    s = re.sub(
        r"\\(?:begin|end)\s*\{[a-zA-Z*]+\}",
        "\n" if display else " ", s,
    )

    # --- row breaks \\[len]& before escape placeholders ---
    # the \\& form: the later \& placeholder substitution would
    # eat one backslash of \\ leaving a stray \&; \\[1mm]
    # spacing args are swallowed too
    s = re.sub(
        r"\\\\\s*(?:\[[^\]]*\])?\s*&+",
        "\n" if display else "; ", s,
    )
    s = re.sub(r"\\\\\s*\[[^\]]*\]",
               "\n" if display else "; ", s)
    # escaped space \  → space; lookbehind keeps \\+space
    # (row break + whitespace) from being half-eaten
    s = re.sub(r"(?<!\\)\\ ", " ", s)

    # --- escaped-char placeholders (shield literals from &/{ handling) ---
    s = s.replace("\\&", "\x00")
    s = s.replace("\\{", "\x01").replace("\\}", "\x02")
    s = s.replace("\\$", "\x03").replace("\\%", "\x04")
    s = s.replace("\\_", "\x05").replace("\\#", "\x06")

    # drop halign template # (escaped \# already is placeholder \x06)
    s = s.replace("#", "")
    # nested $ delimiters inside math → space (\$ already is
    # placeholder \x03)
    s = re.sub(r"\$", " ", s)

    # --- escaped pipe \| → ‖ (norm delimiter) ---
    s = s.replace("\\|", "\u2016")

    # --- spacing commands ---
    # substitute a space, not empty, to keep token boundaries
    # (\forall\,x must not fuse into \forallx); trailing
    # whitespace is normalized later
    s = re.sub(r"\\[,;:!@]", " ", s)
    s = re.sub(r"\\(?:quad|qquad|enspace|thinspace|space)\b", " ", s)
    s = re.sub(r"\\(?:hspace|hskip|mskip|mkern|vspace|vskip)\*?"
               r"(?:\s*[-\d.]+\w*)?\s*\{[^{}]*\}", " ", s)

    # --- \not\X combos (before single symbols) ---
    for combo, sym in _NOT_COMBOS.items():
        s = re.sub(combo, sym, s)

    # superscript dagger forms → †/‡ (no superscript dagger
    # codepoint; before script handling to avoid a stray ^{†})
    s = re.sub(r"([_^])\{\\(?:dag|dagger)\}", "\u2020", s)
    s = re.sub(r"([_^])\{\\(?:ddag|ddagger)\}", "\u2021", s)

    # --- symbol table (long names first; runs before font
    # --- unbracing so \cap\mathbb{G} is not fused into \capG) ---
    s = _SYMBOL_RE.sub(lambda m: _SYMBOLS[m.group(1)], s)

    # strip color shells \textcolor{blue}{x} → x (same
    # semantics as the text-mode pass: color is pure
    # typographic, keep content)
    s = re.sub(
        r"\\textcolor\s*\{[^{}]*\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
        r"\1", s,
    )

    # --- font/accent wrappers (iterate to a fixpoint, ≤8 rounds) ---
    for _ in range(8):
        new = _MATH_FONT_RE.sub(_font_repl, s)
        if new == s:
            break
        s = new

    # --- plain TeX fraction primitives {a \over b} / a \atop
    # b → / (infix transcription) ---
    s = re.sub(r"\\(?:over|atop)(?![a-zA-Z])", "/", s)

    # --- second brace-less accent pass (after symbol table /
    # font shells) ---
    # macro expansion can yield \overline\pi which the early
    # \s+ pattern misses; rescan with the extended character
    # class (Greek/math letters); braced forms already handled
    # by _MATH_FONT_RE
    def _bare_accent2_repl(m):
        name, ch = m.group(1), m.group(2)
        mark = {"underline": "\u0332"}.get(name, _ACCENTS.get(name))
        return ch + mark if mark else ch

    s = re.sub(
        r"\\(hat|widehat|bar|overline|underline|tilde|widetilde|"
        r"vec|dot|ddot|acute|grave|check|breve|slashed)"
        r"(?![a-zA-Z])\s*"
        r"([0-9A-Za-z\u0391-\u03c9\u2200-\u22ff"
        r"\U0001D400-\U0001D7ff])",
        _bare_accent2_repl, s,
    )

    # --- \stackrel{a}{b} → b[a] (over-annotation as a suffix
    # bracket, recursive transcription) ---
    # balanced reading replaces a one-level regex; covers deeply
    # nested args and brace-less single-token forms
    for _ in range(200):
        sm = re.search(r"\\stackrel(?![a-zA-Z])", s)
        if sm is None:
            break
        ra_v, p1 = _read_math_arg(s, sm.end(), permissive=True)
        rb_v, p2 = _read_math_arg(s, p1, permissive=True)
        if ra_v is None or rb_v is None:
            break
        ra = transcribe_math(ra_v, display, evidence,
                             depth + 1, macros)
        rb = transcribe_math(rb_v, display, evidence,
                             depth + 1, macros)
        # eqref/ref output already has [...] → strip to avoid
        # double brackets
        ra = ra.strip().strip("[]()")
        repl = rb + ("[" + ra + "]" if ra else "")
        if repl == s[sm.start():p2]:
            # no progress (rare self-referential form) →
            # unwrap to avoid an infinite loop
            repl = rb + ra_v
        s = s[:sm.start()] + repl + s[p2:]

    # --- \underbrace{X}_{Y} / \overbrace{X}^{Y} → X[Y] ---
    # (annotation as a suffix bracket, same convention as
    # \stackrel)
    for _ in range(200):
        um = re.search(r"\\(?:under|over)brace(?![a-zA-Z])", s)
        if um is None:
            break
        xb = _skip_ws(s, um.end())
        if xb >= len(s) or s[xb] != "{":
            break
        x_v, x_end = _read_braced(s, xb)
        if x_v is None:
            break
        end = x_end
        ann = None
        k = _skip_ws(s, x_end)
        if k < len(s) and s[k] in "_^":
            k2 = _skip_ws(s, k + 1)
            y_v, y_end = None, k
            if k2 < len(s) and s[k2] == "{":
                y_v, y_end = _read_braced(s, k2)
            elif k2 < len(s):
                tm = re.match(r"\\[a-zA-Z]+", s[k2:])
                if tm is not None:
                    k3 = _skip_ws(s, k2 + tm.end())
                    if k3 < len(s) and s[k3] == "{":
                        inner, i_end = _read_braced(s, k3)
                        if inner is not None:
                            y_v = tm.group(0) + "{" + inner + "}"
                            y_end = i_end
                    else:
                        y_v, y_end = tm.group(0), k2 + tm.end()
                elif s[k2] not in "{}\\":
                    y_v, y_end = s[k2], k2 + 1
            if y_v is not None:
                ann = transcribe_math(y_v, display, evidence,
                                      depth + 1, macros)
                end = y_end
        rx = transcribe_math(x_v, display, evidence, depth + 1,
                             macros)
        ra = (ann or "").strip().strip("[]()")
        repl = rx + ("[" + ra + "]" if ra else "")
        if repl == s[um.start():end]:
            repl = rx + x_v
        s = s[:um.start()] + repl + s[end:]

    # --- \\frac / \\sqrt / \\binom (manual balanced expansion) ---
    # args read via _read_math_arg (braced group or single
    # token); covers \frac12 / \frac ab shorthand as well as
    # cross-line, spaced, and mixed argument forms
    for _ in range(200):
        m = re.search(r"\\(?:[td]?frac|binom)(?![a-zA-Z])", s)
        sqrt_m = None
        if m is None:
            # relaxed lookahead: braced \sqrt{g} and bare-token
            # \sqrt g / \sqrt (2i+1) share one entry
            sqrt_m = re.search(r"\\sqrt(?![a-zA-Z])\s*(\[[^\]]*\])?", s)
            if sqrt_m is None:
                break
        if m is not None:
            a, a_end = _read_math_arg(s, m.end())
            if a is None:
                break
            b, b_end = _read_math_arg(s, a_end)
            if b is None:
                break
            cmd = m.group(0).lstrip("\\").rstrip()
            ra = transcribe_math(a, display, evidence, depth + 1,
                                 macros)
            rb = transcribe_math(b, display, evidence, depth + 1,
                                 macros)
            if cmd.startswith("binom"):
                repl = "C(" + ra + ", " + rb + ")"
            else:
                repl = _wrap_arg(ra) + "/" + _wrap_arg(rb)
            s = s[:m.start()] + repl + s[b_end:]
        else:
            a, a_end = _read_braced(s, sqrt_m.end())
            if a is None:
                # bare-token form \sqrt g / \sqrt (2i+1) —
                # single token / \cmd / balanced parens;
                # punctuation counts as no match
                a, a_end = _read_math_arg(s, sqrt_m.end(),
                                          permissive=True)
                if a is None:
                    break
                if a == "(":
                    close = s.find(")", a_end)
                    if close == -1:
                        break
                    a = s[a_end:close]
                    a_end = close + 1
                elif (len(a) == 1 and
                      not re.match(r"[0-9A-Za-z!?Α-ω]", a)):
                    break
            ra = transcribe_math(a, display, evidence, depth + 1,
                                 macros)
            opt = sqrt_m.group(1)
            prefix = "\u221a"
            if opt:
                idx = opt.strip("[]")
                opt_tr = _apply_scripts("^{" + idx + "}")
                prefix = opt_tr if opt_tr != "^" + idx else idx + "\u221a"
            s = (s[:sqrt_m.start()] + prefix + "(" + ra + ")"
                 + s[a_end:])

    # --- super/subscripts ---
    s = _apply_scripts(s)

    # --- row breaks / column separators / grouping braces ---
    s = s.replace("\\\\", "\n" if display else "; ")
    s = s.replace("&", "  ")
    s = s.replace("{", "").replace("}", "")

    # --- placeholder restore ---
    s = (s.replace("\x00", "&").replace("\x01", "{")
         .replace("\x02", "}").replace("\x03", "$")
         .replace("\x04", "%").replace("\x05", "_")
         .replace("\x06", "#"))

    # --- whitespace collapse ---
    s = re.sub(r"[ \t]+", " ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln)

    # --- \\tag suffix ---
    tag_m = _TAG_RE.search(s)
    if tag_m:
        tag = tag_m.group(1).strip()
        s = _TAG_RE.sub("", s).rstrip()
        if tag:
            s = s + "\n(" + tag + ")" if display else s + " (" + tag + ")"

    # --- unknown-command disclosure ---
    unknown = _CMD_RE.findall(s)
    if unknown:
        ev_unknown = evidence.setdefault("unknown_math_commands", 0)
        evidence["unknown_math_commands"] = ev_unknown + len(unknown)
        samples = evidence.setdefault(
            "unknown_math_command_samples", []
        )
        for u in unknown:
            if "\\" + u not in samples and len(samples) < 12:
                samples.append("\\" + u)
    return s
