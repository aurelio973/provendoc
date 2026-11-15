r"""Academic-paper scenario → DQR payload projection (v1.7).

The academic-paper scenario chain v1 is a LEDGER, not a repair: unlike
invoices (whose OCR text is corrupted and needs salvage), papers'
extraction pain is structure location, so v1 records line-level
coordinates for the conventions the resolver already saw as counts —
abstract / keywords / references headings, author-year citations,
display math and numbered section headings — and stamps them on
``document.metadata["academic_paper_scenario"]``.

v1.5 (prereg: ``specs/academic_paper_chunk_atomicity_prereg_v1.md``)
adds the chunk integration: References entries are derived as
blank-line-separated runs in ``references_line..EOF`` and stamped as
``references_entries``; the text is chunked by
``academic_paper_chunking.chunk_academic_paper_text`` so that formula
blocks and References entries are never split across chunk
boundaries; the shared ``check_chunk_invariants`` verdict (coverage +
byte integrity) is reported as ``chunk_invariants``.

v1.6 (prereg: ``specs/academic_paper_section_headings_prereg_v1.md``)
adds section-heading keyword locators: conventional section names
(目录/contents, 引言/前言/绪论/introduction, 结论/总结/结语/
conclusion/summary, 致谢/acknowledgements, 附录/appendix, 正文)
matched ONLY as standalone short lines (optional numbering prefix,
optional trailing colon; the appendix form additionally allows a
labeled title like "Appendix B. Proof of ...") — a keyword inside a
prose sentence never matches. Raw-TeX macro forms
(``\section{Introduction}``, bare ``\appendix``) are additionally
recognized (D6), with the brace title required to be the keyword
itself. The references region additionally understands the
``\begin{thebibliography}{10}`` environment (begin/end anchored at
line start only — a trailing ``%`` comment, ``\label``, or nested
braces in the width argument are all legal; entries are
``\bibitem`` runs, the ``\end`` line closes the region) and the
BibTeX ``\bibliography{...}`` macro (the line is recorded with
zero entries — entries live in the .bbl, not this source) (D7). ``\cite``-family lines are recorded as
``tex_citation_lines`` — raw source never carries rendered
author-year citations, so the v1 citation locator alone would be
honestly blind on this corpus. Located headings are recorded as
facts (``section_headings``), never
promoted into structure. One
behavioral payoff: a located appendix heading AFTER the References
heading ends the references region there (``references_end_line``)
instead of EOF, honestly narrowing v1.5's conservative back-matter
protection.

v1.7 (prereg: ``specs/academic_paper_math_regions_prereg_v1.md``)
widens display math from single begin lines to WHOLE regions: the
equation/align/gather/multline/eqnarray environment families,
``\\[ ... \\]`` and ``$$`` blocks mark every covered line
(interior included), so chunk atomicity protects the entire block;
the chunker consumes the UNCAPPED region list — the v1.6 cap-200
sample list silently dropped protection past line 200, a real
defect this version fixes. Special characters enter as census
counts: lines carrying sub/superscript tokens, symbol macros or
accent commands are counted in ``stats`` (``tex_subsup_lines`` /
``tex_symbol_lines`` / ``tex_accent_lines``) — on math-heavy
papers these saturate (tens of thousands of lines), so line lists
would be pure cap noise; an honest count is the fact. The table
line-break macro ``\\[2pt]`` never opens a bracket region
(lookbehind guard), and control words like ``\\cite`` never
count as the letter accents ``\\c``/``\\t``/``\\b``. The scan
is comment-aware (an unescaped ``%`` cuts the line, mirroring
what TeX reads), ``$``-stateful (``$$`` is a display delimiter
only outside inline math, so adjacent spans like
``$\\dag$$\\ddag$`` never open a phantom block; an unbalanced
``$`` sticks at most to its paragraph — blank lines reset it)
and macro-definition-guarded (``\\newcommand``-family bodies,
brace-depth tracked and blank-line bounded, contain definitions
not rendered math; a COMPLETE single-line definition of the form
``\\newcommand{\\bea}{\\begin{eqnarray}}`` is registered as an
environment alias, and the alias's real uses (``\\bea ... \\eea``,
the HEP-template convention, 28/339 corpus files) are tracked as
regions).
These guards came from real corpus leaks — three files had
93-96% of their lines falsely marked.

Contract discipline (mirrors invoice_scenario_dqr):

* verbatim projection — every located line number and bounded text
  is present; nothing summarized away;
* line numbers refer to ``original_text`` lines (1-based) — this
  chain does NO salvage, so there is no line_map and no second
  coordinate space;
* DQR surfaces the payload VERBATIM and never folds it into the
  four dimension scores (scenario evidence is reported fact, not a
  score input);
* attach-only — document text fields are never touched.

Honest boundaries:

* heading detection is heuristic (numbered-line shape, length-
  capped); a false heading is recorded as a fact, never promoted
  into structure — heading RECOVERY stays in heading_patterns;
* citation/formula caps are recorded in ``stats`` when hit — a
  capped count is a reported bound, not the true count;
* the references region is taken as ``references_line..EOF`` —
  anything after the References heading (appendix, back matter)
  is protected as if it were entries: conservative by design,
  registered in the ledger note, never silent; since v1.6 a
  located post-References appendix heading ends the region there
  (``references_end_line``), shrinking the protection honestly;
* section-heading keywords are shape-guarded locators (standalone
  line + optional numbering + optional colon), never a genre vote —
  identification counts stay in domain_signals, untouched by this
  chain (changing them would invalidate the calibration freeze);
* ``正文`` is a book/report convention rarely seen in papers — it
  is recorded as a fact when present, nothing more;
* the resolver's label decision is upstream (frameworks registry
  dispatch); this module is the pure projection primitive.

Dispatch (frameworks registry): the chain is registered under
``academic_paper_scenario`` in ``core/scenario/frameworks.py`` and
invoked by ``universal_parser`` after the L2 resolver labels the
text ``academic_paper``.
"""

from __future__ import annotations

import re
from typing import Any

from provendoc.core.scenario.academic_paper_chunking import (
    chunk_academic_paper_text,
)
from provendoc.core.scenario.invoice_chunking import check_chunk_invariants

SCENARIO_METADATA_KEY = "academic_paper_scenario"

_MAX_CITATION_LINES = 200
_MAX_FORMULA_LINES = 200
_MAX_HEADING_LINES = 100
_MAX_REFERENCE_ENTRIES = 400
_MAX_SECTION_HEADING_LINES = 100
_MAX_TEXT_CHARS = 80

# Line-locator shapes (locator duty only — identification counts stay
# in domain_signals; these find the line coordinates for the ledger).
_ABSTRACT_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:abstract|摘\s*要)[ \t]*[:：]?[ \t]*$"
)
_KEYWORDS_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:keywords?|关键词|关键字)[ \t]*[:：]"
)
_REFERENCES_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:参考文献|references|bibliography)[ \t]*$"
)
# raw-TeX bibliography forms (D7 — eyeball-run finding: 103/339 dev
# files open the bibliography with \begin{thebibliography}{10}, 67
# include it via the BibTeX \bibliography{...} macro; the v1.6
# empty-{}-only alternation missed both). The begin/end lines anchor
# at line START only — trailing content is legal TeX (a ``%``
# comment, a ``\label``, and the width argument itself may nest
# braces, e.g. ``{ADH{\etalchar{+}}13}``), and no prose line
# starts with an environment macro.
_TEX_THEBIB_BEGIN_RE = re.compile(
    r"(?im)^[ \t]*\\begin\{(?:the)?bibliography\}"
)
_TEX_THEBIB_END_RE = re.compile(
    r"(?im)^[ \t]*\\end\{(?:the)?bibliography\}"
)
_TEX_BIBMACRO_RE = re.compile(
    # \bibliographystyle{...} deliberately does NOT match: "style"
    # sits between the macro name and the brace
    r"(?im)^[ \t]*\\bibliography\{[^{}\n]*\}[ \t]*$"
)
_TEX_BIBITEM_RE = re.compile(r"^[ \t]*\\bibitem\b")
_CITATION_RE = re.compile(
    r"\([A-Z][A-Za-zÀ-ÿ'\-]+(?:\s+et\s+al\.?)?,\s*\(?(?:19|20)\d{2}[a-z]?\)"
)
# display math (D8): WHOLE environments are formula regions —
# begin line, every interior line, end line. Outer display
# environments only; inner nests (aligned/split) live inside these
# and are covered by the enclosing region. \\[ ... \\] regions use a
# lookbehind so the table line-break macro \\[2pt] never opens
# one; $$ blocks are tracked by parity so multi-line blocks mark
# their interior lines (inline $x$ stays excluded, the v1 decision).
# An unterminated environment extends to EOF — conservative, the
# protection never silently shrinks.
_TEX_DISPLAY_ENV_NAMES = (
    "equation", "align", "alignat", "flalign", "gather",
    "multline", "eqnarray", "displaymath", "math",
)
_TEX_DISPLAY_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(_TEX_DISPLAY_ENV_NAMES) + r")(\*?)\}"
)
_TEX_DISPLAY_END_RE = re.compile(r"\\end\{([A-Za-z]+)(\*?)\}")
_TEX_BRACKET_OPEN_RE = re.compile(r"(?<!\\)\\\[")
_TEX_BRACKET_CLOSE_RE = re.compile(r"(?<!\\)\\\]")
# a \begin{...} INSIDE a macro definition is the definition body,
# not an occurrence — the real uses expand a macro alias (the
# corpus's \be/\ee pair in 1801.01031), which this locator
# registers and tracks as alias regions instead of leaking a
# phantom region to EOF (96% of that file was marked before the
# registration existed); an alias whose definition fits none of
# the registered forms still falls back to normal chunking
_TEX_MACRO_DEF_RE = re.compile(
    r"\\(?:new|renew|provide)command\b"
    r"|\\(?:new|renew)environment\b"
    r"|\\def\b"
    r"|\\DeclareMathOperator\b"
)
# environment ALIASES: \newcommand{\bea}{\begin{eqnarray}} / \def\ee{\end{eqnarray}}
# — a HEP-template convention, 28/339 dev files, up to 143 alias
# uses in ONE file (1701.00784). The definition line must be a
# COMPLETE single-line form (\newcommand{\X}{\begin{env}}), so a
# multi-line definition body never mis-registers. Three more real
# corpus shapes found by the 339-file eye audit: \def may space
# its name (\def \beeq {...}, 1901.00500), a raw def may take an
# argument (\def\beq#1{\begin{equation}\label{#1}}, 1801.00413),
# and the body may carry trailing tokens ({\begin{eqnarray}
# \nonumber}, 1801.00728) — one brace level of trailing is
# allowed, but never a trailing \end: that shape is a
# self-contained wrapper (see _TEX_ENV_ALIAS_WRAP_RE), not an
# alias, and registering it as one would leak a phantom region at
# use sites where no end token ever appears
_TEX_ENV_ALIAS_DEFCMD_RE = re.compile(
    r"\\(?:new|renew|provide)command\{?\\([A-Za-z]+)\}?\s*\{\\(begin|end)\{([A-Za-z]+\*?)\}(?:(?!\\end)[^{}]|\{[^{}]*\})*\}"
)
_TEX_ENV_ALIAS_DEFRAW_RE = re.compile(
    r"\\def\s*\\([A-Za-z]+)\s*(?:#\d+\s*)?\{\\(begin|end)\{([A-Za-z]+\*?)\}(?:(?!\\end)[^{}]|\{[^{}]*\})*\}"
)
_TEX_ENV_ALIAS_DEFPAR_RE = re.compile(
    r"\\def\\([A-Za-z]+)#1\\([A-Za-z]+)"
    r"\{\\begin\{([A-Za-z]+\*?)\}#1\\end\{\3\}\}"
)
# single-macro ARGUMENT wrapper (1901.00218, the only corpus
# user): \newcommand{\eq}[1]{\begin{equation}#1\end{equation}} —
# the macro's USE (\eq{ ... }) is itself the display region,
# closed by the argument's matching brace, so it is tracked by
# brace depth instead of an end token
_TEX_ENV_ALIAS_WRAP_RE = re.compile(
    r"\\(?:new|renew|provide)command\{?\\([A-Za-z]+)\}?\s*(?:\[1\])?\s*\{\\begin\{([A-Za-z]+\*?)\}#1\\end\{\2\}\}"
)
# parameter-DELIMITER alias (1701.00528, the only corpus user):
# \def\ba#1\ea{\begin{align}#1\end{align}} registers the \ba/\ea
# pair for align; the delimiter end token (\ea) has no standalone
# source meaning — its ONLY role is closing a display region
_TEX_CONTROL_WORD_RE = re.compile(r"\\([A-Za-z]+)")
# subequations is a display-family WRAPPER whose body may carry
# prose between its inner equations (1701.00402: a full sentence
# sits between two equations of one subequations block — marking
# the whole block would swallow it), so it NEVER opens a region;
# only its boundary lines are display tokens (76 uses, 22 files),
# while the inner equation/align/etc. carry the region work. The
# theorem/lemma/defn family is NOT display at all — its begin
# lines are theorem-statement prose with inline math (1801.00656
# L176), so it stays outside the family by design
_TEX_SUBEQUATIONS_BOUNDARY_RE = re.compile(
    r"\\(begin|end)\{subequations(\*?)\}"
)

# special-character census (D9): sub/superscript tokens, symbol
# macros and accent commands. COUNTS ONLY — these saturate
# math-heavy papers (corpus census: ~58k/~62k/~33k-of-339-file
# lines), so line lists would be pure cap noise; a count is the
# honest fact. The accent guard (?![A-Za-z]) keeps control words
# like \cite from counting as the letter accents \c/\t/\b.
# Sub/sup tokens include label underscores like \label{fig_x} — a
# density census, not a parser; backslash-escaped tokens (\^ and
# \= accents, \_ separator) are guarded out by the lookbehind.
_TEX_SUBSUP_RE = re.compile(r"(?<!\\)[_^][ \t]*\\?[A-Za-z0-9{]")
_TEX_SYMBOL_NAMES = (
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon",
    "zeta", "eta", "theta", "vartheta", "iota", "kappa", "lambda",
    "mu", "nu", "xi", "pi", "varpi", "rho", "varrho", "sigma",
    "varsigma", "tau", "upsilon", "phi", "varphi", "chi", "psi",
    "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi",
    "Sigma", "Upsilon", "Phi", "Psi", "Omega",
    "sum", "prod", "coprod", "int", "oint", "lim", "max", "min",
    "sup", "inf", "frac", "dfrac", "tfrac", "sqrt", "partial",
    "nabla", "infty", "pm", "mp", "times", "div", "cdot", "cdots",
    "ldots", "dots", "leq", "geq", "neq", "approx", "sim",
    "simeq", "propto", "equiv", "ll", "gg", "subset", "supset",
    "subseteq", "supseteq", "in", "ni", "cup", "cap", "wedge",
    "vee", "neg", "land", "lor", "rightarrow", "leftarrow",
    "Rightarrow", "Leftarrow", "leftrightarrow", "Leftrightarrow",
    "mapsto", "implies", "iff", "forall", "exists", "left",
    "right", "langle", "rangle", "lceil", "rceil", "lfloor",
    "rfloor", "hat", "widehat", "bar", "overline", "vec",
    "overrightarrow", "tilde", "widetilde", "dot", "ddot",
    "underline", "mathrm", "mathbf", "mathit", "mathsf",
    "mathtt", "mathcal", "mathbb", "mathfrak", "bm", "prime",
    "circ", "star", "ast", "dagger", "ddagger", "aleph", "hbar",
    "ell", "Re", "Im", "emptyset", "varnothing", "top", "bot",
    "vdash", "dashv", "perp", "parallel", "angle", "triangle",
    "square",
)
_TEX_SYMBOL_RE = re.compile(
    r"\\(?:" + "|".join(_TEX_SYMBOL_NAMES) + r")(?![A-Za-z])"
)
_TEX_ACCENT_RE = re.compile(
    r"\\[`'^\"~=.][ \t]*(?:[A-Za-z{]|\\)"
    r"|\\[uvHtdbcr](?![A-Za-z])[ \t]*[A-Za-z{]"
)
# raw-TeX \cite family (\cite, \citep, \citet, \citeauthor...):
# the rendered author-year form the v1 locator seeks never appears
# in raw source, so these lines are the honest citation facts here
_TEX_CITE_LINE_RE = re.compile(r"\\cite[a-zA-Z]*\s*\{")
_NUMBERED_HEADING_RE = re.compile(
    # "1. Introduction" / "2.1 Method" — line-start number, short,
    # no sentence punctuation tail (a numbered list item inside a
    # sentence usually continues with text; the length cap keeps
    # prose paragraphs out)
    r"(?m)^[ \t]*\d{1,2}(?:\.\d{1,2}){0,3}\.?[ \t]+\S[^\n]{0,80}$"
)

# v1.6 section-heading locators: a conventional section name as a
# STANDALONE SHORT LINE (optional numbering prefix, optional
# trailing colon; the appendix form additionally allows a labeled
# title). The ^...$ shape is the guard — a keyword inside a prose
# sentence never matches. Locator duty only: these record facts;
# identification counts stay in domain_signals.
_KW_CONTENTS = r"(?:目\s*录|table\s+of\s+contents|contents)"
_KW_INTRODUCTION = r"(?:引\s*言|前\s*言|绪\s*论|introduction)"
_KW_CONCLUSION = r"(?:结\s*论|总\s*结|结\s*语|conclusions?|summary)"
_KW_ACKNOWLEDGEMENTS = r"(?:致\s*谢|鸣\s*谢|acknowledg(?:ements?|ment|e))"
_KW_BODY = r"正\s*文"
_KW_APPENDIX = r"(?:附\s*录|appendix)"

_SECTION_HEADING_PREFIX = (
    r"(?:第\s*[0-9一二三四五六七八九十百]+\s*[章节部分]"
    r"|\d{1,2}(?:\.\d{1,2}){0,3}"
    r"|[一二三四五六七八九十]{1,3}"
    r"|[A-Za-z])"
    r"[ \t.、．]*"
)


def _section_heading_re(body: str) -> re.Pattern[str]:
    return re.compile(
        r"(?im)^[ \t]*(?:" + _SECTION_HEADING_PREFIX + r")?" + body
    )


def _tex_section_re(keyword: str) -> re.Pattern[str]:
    r"""Raw-TeX macro form: ``\section{Introduction}`` etc. (D6).

    The brace title must be the keyword ITSELF — ``
    \section{Introduction to NLP}`` is a prose-ish title, not a
    conventional section marker, and stays unmatched.
    """
    return re.compile(
        r"(?im)^[ \t]*\\(?:section|chapter|part)\*?[ \t]*\{"
        + keyword
        + r"\}[ \t]*$"
    )


# bare \appendix macro switches TeX into appendix mode — a real
# region boundary, recorded as an appendix fact
_TEX_APPENDIX_MACRO_RE = re.compile(r"(?im)^[ \t]*\\appendix\b[ \t]*$")

_SECTION_HEADING_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contents", _section_heading_re(
        _KW_CONTENTS + r"[ \t]*[:：]?\s*$"
    )),
    ("introduction", _section_heading_re(
        _KW_INTRODUCTION + r"[ \t]*[:：]?\s*$"
    )),
    ("conclusion", _section_heading_re(
        _KW_CONCLUSION + r"[ \t]*[:：]?\s*$"
    )),
    ("acknowledgements", _section_heading_re(
        _KW_ACKNOWLEDGEMENTS + r"[ \t]*[:：]?\s*$"
    )),
    ("body", _section_heading_re(_KW_BODY + r"[ \t]*[:：]?\s*$")),
    # appendix allows a labeled title (label + punctuation separator
    # BEFORE the title, so prose like "Appendix A contains proofs"
    # cannot match); "Appendix A Proof" without a separator stays
    # unmatched — strict by design, the miss only costs the region
    # end-cap opportunity and falls back to the conservative EOF.
    ("appendix", re.compile(
        r"(?im)^[ \t]*(?:" + _KW_APPENDIX + r")[ \t]*"
        r"(?:[A-Za-z]|\d{1,2})"
        r"(?:[ \t]*[:：.．][ \t]*[^\n]{0,40})?[ \t]*$"
        r"|^[ \t]*(?:" + _KW_APPENDIX + r")[ \t]*[:：]?\s*$"
    )),
    # raw-TeX macro forms (D6) — appended so a plain-text line is
    # always tried first
    ("contents", _tex_section_re(_KW_CONTENTS)),
    ("introduction", _tex_section_re(_KW_INTRODUCTION)),
    ("conclusion", _tex_section_re(_KW_CONCLUSION)),
    ("acknowledgements", _tex_section_re(_KW_ACKNOWLEDGEMENTS)),
    ("body", _tex_section_re(_KW_BODY)),
    ("appendix", _tex_section_re(_KW_APPENDIX)),
    ("appendix", _TEX_APPENDIX_MACRO_RE),
)


def _line_of_first_match(pattern: re.Pattern[str], lines: list[str]) -> int | None:
    """1-based line number of the first line matching, else None."""
    for idx, line in enumerate(lines):
        if pattern.search(line):
            return idx + 1
    return None


def _lines_matching(pattern: re.Pattern[str], lines: list[str], cap: int):
    """(line_numbers, bounded_texts) for every matching line, capped."""
    found: list[int] = []
    texts: list[str] = []
    for idx, line in enumerate(lines):
        if pattern.search(line):
            found.append(idx + 1)
            texts.append(line.strip()[:_MAX_TEXT_CHARS])
            if len(found) >= cap:
                break
    return found, texts


def _strip_tex_comment(line: str) -> str:
    r"""Cut the line at the first UNESCAPED ``%`` (TeX comment).

    ``\%`` is a literal percent sign and survives; scanning the
    comment-stripped line mirrors what TeX actually reads — a
    commented ``\begin{align}`` or ``$$`` is not input at all
    (real corpus leak: ``%\author{...Zhang$\dag$$\ddag$...}``
    toggled a phantom display block under naive substring
    counting, marking 93% of 1901.00054).
    """
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            out.append(line[i : i + 2])
            i += 2
            continue
        if ch == "%":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _brace_depth(line: str) -> int:
    r"""Net unmatched open braces on the line (``\{``/``\}`` escaped)."""
    depth = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def _display_formula_lines(lines: list[str]) -> list[int]:
    r"""1-based line numbers covered by display math (D8), UNCAPPED.

    A ``\begin{equation}``-family line opens a region closed by the
    matching ``\end{...}`` (star must match too); a line may carry
    SEVERAL ends (``\end{array}\right.\end{equation}`` is a real
    corpus shape) so every end on the line is checked. ``\[ ... \]``
    and ``$$`` blocks are tracked to their closing line. Every line
    of the region — interior included — is returned, so chunk
    atomicity covers the whole block, not just its first line.
    Single-line forms (``$$x$$``, ``\[x\]``, begin and end on one
    line) mark that one line. Inner nests (aligned/split) are
    covered by the enclosing outer region. Unterminated
    environments extend to EOF — conservative by design.

    Comment-aware (``%`` to end of line is stripped first) and
    ``$``-stateful: ``$$`` counts as a display delimiter ONLY
    outside inline math, so adjacent inline spans like
    ``$\dag$$\ddag$`` never toggle a phantom display block; a
    ``\begin`` inside a macro definition line
    (``\newcommand{\be}{\begin{eqnarray*}}``) does not open a
    region — the definition is REGISTERED as an environment
    alias pair and its real uses (``\bea ... \eea``, the
    HEP-template convention) are tracked as regions instead of
    missed; an unregistered alias's uses fall back to normal
    chunking. The parameter-delimiter def form
    (``\def\ba#1\ea{\begin{align}#1\end{align}}``, 1701.00528)
    registers the begin/delimiter pair too, and the delimiter
    token (``\ea``) closes any open display region — it has no
    standalone meaning, so a ``\bea``-opened region closed by
    ``\ea`` is honoured instead of swallowing prose to EOF.
    A single-macro argument wrapper
    (``\newcommand{\eq}[1]{\begin{equation}#1\end{equation}}``,
    1901.00218) registers the wrapper word, and its uses
    (``\eq{ ... }``) are tracked as brace-depth regions closed
    by the argument's matching brace. ``subequations`` boundary
    lines are marked, but the wrapper never opens a region — prose
    may sit between its inner equations, so the inner display
    environments carry the region work themselves.
    Multi-line definition bodies are tracked by brace
    depth (``\newcommand{\VertexForPosets}[2]{ ... $$ ... }``
    is a real corpus shape); a blank line ends the body (TeX
    forbids ``\par`` in a non-``\long`` definition), so the
    suppression can never stick past a paragraph and silently
    swallow real regions.
    """
    marked: list[int] = []
    open_env: str | None = None  # environment name incl. star
    in_bracket = False
    in_ddollar = False
    in_inline = False
    def_depth = 0  # >0 → inside an unclosed macro definition body
    begin_alias: dict[str, str] = {}  # \bea → "eqnarray"
    end_alias: dict[str, str] = {}  # \eea → "eqnarray"
    # delimiter-form end tokens (\ea from \def\ba#1\ea{...}): no
    # standalone meaning, so wherever one appears inside an open
    # display region it closes that region — 1701.00528 opens 19
    # regions with \bea/eqnarray but closes them with \ea/align,
    # a systematic template convention, not a one-off typo
    end_delim_alias: set[str] = set()
    # argument-wrapper aliases (\eq → "equation", from
    # \newcommand{\eq}[1]{\begin{equation}#1\end{equation}},
    # 1901.00218 — the only corpus user): the macro's use is
    # itself the display region, closed by the argument's
    # matching brace rather than an end token
    wrapper_alias: dict[str, str] = {}
    wrapper_depth = 0  # >0 → inside a wrapper argument still open
    for idx, raw in enumerate(lines):
        line = _strip_tex_comment(raw)
        mark = False
        # paragraph break: inline math cannot span \par in TeX, so an
        # unbalanced $ sticks at most to the end of its paragraph —
        # without this reset a single stray $ would silently swallow
        # every later $$ display block; it also ends a macro
        # definition body, bounding any brace-imbalance suppression
        if not line.strip():
            in_inline = False
            def_depth = 0
            if wrapper_depth > 0:
                # a macro argument cannot span \par (TeX forbids
                # it in a non-\long argument), so a still-open
                # wrapper region at a blank line is a misfire —
                # close it instead of swallowing prose to EOF
                wrapper_depth = 0
                open_env = None
        if def_depth > 0:
            # inside a multi-line macro definition body: no display
            # toggles, no region opens, nothing marked — the body
            # is a definition, not rendered math
            def_depth = max(0, def_depth + _brace_depth(line))
            continue
        if (
            open_env is None
            and not in_bracket
            and not in_ddollar
            and _TEX_MACRO_DEF_RE.search(line)
        ):
            # a macro DEFINITION line (its body is not rendered
            # where it is written): first register any COMPLETE
            # single-line environment alias (\newcommand{\bea}{...
            # \begin{eqnarray}}} — only display-family envs count)
            # so its later uses are tracked as real regions, then
            # enter body tracking when the braces stay open past
            # this line; a balanced single-line definition just
            # skips this line — a \begin{...} or $$ inside the
            # body is the definition, not an occurrence
            for am in _TEX_ENV_ALIAS_DEFCMD_RE.finditer(line):
                name, kind, env = am.group(1), am.group(2), am.group(3)
                if env.rstrip("*") in _TEX_DISPLAY_ENV_NAMES:
                    (begin_alias if kind == "begin" else end_alias)[
                        name
                    ] = env
            for am in _TEX_ENV_ALIAS_DEFRAW_RE.finditer(line):
                name, kind, env = am.group(1), am.group(2), am.group(3)
                if env.rstrip("*") in _TEX_DISPLAY_ENV_NAMES:
                    (begin_alias if kind == "begin" else end_alias)[
                        name
                    ] = env
            for am in _TEX_ENV_ALIAS_DEFPAR_RE.finditer(line):
                bname, ename, env = (
                    am.group(1),
                    am.group(2),
                    am.group(3),
                )
                if env.rstrip("*") in _TEX_DISPLAY_ENV_NAMES:
                    begin_alias[bname] = env
                    end_alias[ename] = env
                    end_delim_alias.add(ename)
            for am in _TEX_ENV_ALIAS_WRAP_RE.finditer(line):
                name, env = am.group(1), am.group(2)
                if env.rstrip("*") in _TEX_DISPLAY_ENV_NAMES:
                    wrapper_alias[name] = env
            def_depth = max(0, _brace_depth(line))
            continue
        if (
            _TEX_SUBEQUATIONS_BOUNDARY_RE.search(line)
        ):
            # a subequations boundary line is a display token, but
            # the wrapper itself never opens a region — prose may
            # sit between its inner equations (1701.00402), so the
            # inner display envs do the region work themselves
            mark = True
        if open_env is not None:
            mark = True
            if wrapper_depth > 0:
                # an ARGUMENT-WRAPPER region (\eq{ ... }) closes by
                # brace balance — the closer is the macro argument's
                # closing brace, not an \end token; end words inside
                # the argument (\label{...}) must not close it early
                wrapper_depth += _brace_depth(line)
                if wrapper_depth <= 0:
                    wrapper_depth = 0
                    open_env = None
            else:
                for m in _TEX_DISPLAY_END_RE.finditer(line):
                    if m.group(1) + m.group(2) == open_env:
                        open_env = None
                        break
                else:
                    # close by END ALIAS too (\eea for eqnarray) — the
                    # real \\end and the alias are interchangeable
                    # close forms, whichever the template uses. A
                    # DELIMITER-form end token (\ea) closes ANY open
                    # region regardless of env: it never has a
                    # standalone meaning, so a mismatched pair
                    # (\bea-opened region closed by \ea) is the
                    # template's close convention, and honouring it
                    # bounds what would otherwise swallow prose to the
                    # next same-env close or EOF (1701.00528: a
                    # 243-line swallow through the bibliography)
                    if end_alias:
                        for wm in _TEX_CONTROL_WORD_RE.finditer(line):
                            name = wm.group(1)
                            if (
                                end_alias.get(name) == open_env
                                or name in end_delim_alias
                            ):
                                open_env = None
                                break
        if in_bracket:
            mark = True
            if _TEX_BRACKET_CLOSE_RE.search(line):
                in_bracket = False
        if in_ddollar:
            mark = True
        # $ state machine (mirroring TeX's own math-mode scanning):
        # a lone $ toggles inline math and is NOT a display marker
        # (the v1 decision); $$ is a display delimiter only when no
        # inline span is open, so $\dag$$\ddag$ reads as two inline
        # spans instead of a phantom display open
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "$":
                if in_inline:
                    in_inline = False
                    i += 1
                elif line[i + 1 : i + 2] == "$":
                    in_ddollar = not in_ddollar
                    mark = True
                    i += 2
                else:
                    in_inline = True
                    i += 1
            else:
                i += 1
        if open_env is None and not in_bracket:
            begin_m = _TEX_DISPLAY_BEGIN_RE.search(line)
            if begin_m:
                mark = True
                name = begin_m.group(1) + begin_m.group(2)
                # only an end AFTER the begin position closes it —
                # the proof style of 1701.00281 writes
                # "\end{displaymath} prose \begin{displaymath}"
                # on one line, and that earlier end closes the OLD
                # region, not the one this begin reopens
                closed_on_line = any(
                    em.group(1) + em.group(2) == name
                    and em.start() > begin_m.start()
                    for em in _TEX_DISPLAY_END_RE.finditer(line)
                )
                if not closed_on_line:
                    open_env = name
            elif wrapper_alias and any(
                wrapper_alias.get(wm.group(1)) is not None
                for wm in _TEX_CONTROL_WORD_RE.finditer(line)
            ):
                # open by ARGUMENT WRAPPER (\eq{ ... }): the macro's
                # argument IS the display region — the wrapper word
                # opens it and the argument's matching brace closes
                # it (tracked above by wrapper_depth); a use whose
                # braces balance on this very line is a one-line
                # display block, marked but never opened
                for wm in _TEX_CONTROL_WORD_RE.finditer(line):
                    env = wrapper_alias.get(wm.group(1))
                    if env is not None:
                        mark = True
                        wrapper_depth = _brace_depth(line[wm.end():])
                        if wrapper_depth > 0:
                            open_env = env
                        else:
                            wrapper_depth = 0
                        break
            elif begin_alias and any(
                begin_alias.get(wm.group(1)) is not None
                for wm in _TEX_CONTROL_WORD_RE.finditer(line)
            ):
                # open by BEGIN ALIAS (\bea for eqnarray); control
                # word matching is word-boundary by construction,
                # so \bean never triggers the \bea entry — but the
                # branch only fires when an alias word is actually
                # ON the line, so a registered alias never shadows
                # the \[ \] bracket branch below (1801.00683 lost
                # 24 lines and 1701.00789 lost all 74 of its \[ \]
                # blocks — 322 lines — exactly so)
                for wm in _TEX_CONTROL_WORD_RE.finditer(line):
                    env = begin_alias.get(wm.group(1))
                    if env is not None:
                        mark = True
                        closed = any(
                            em.group(1) + em.group(2) == env
                            and em.start() > wm.start()
                            for em in _TEX_DISPLAY_END_RE.finditer(line)
                        ) or any(
                            end_alias.get(xm.group(1)) == env
                            and xm.start() > wm.start()
                            for xm in _TEX_CONTROL_WORD_RE.finditer(line)
                        )
                        if not closed:
                            open_env = env
                        break
            elif _TEX_BRACKET_OPEN_RE.search(line):
                mark = True
                if not _TEX_BRACKET_CLOSE_RE.search(line):
                    in_bracket = True
        if mark:
            marked.append(idx + 1)
    return marked


def _locate_section_headings(
    lines: list[str],
) -> list[dict[str, Any]]:
    """Standalone section-heading lines, one kind per line, capped.

    First matching kind wins per line (D5); every entry carries
    kind, 1-based line number and a bounded text sample. Pure
    locator — facts only, never structure promotion.
    """
    entries: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        for kind, pattern in _SECTION_HEADING_KINDS:
            if pattern.search(line):
                entries.append({
                    "kind": kind,
                    "line": idx + 1,
                    "text": line.strip()[:_MAX_TEXT_CHARS],
                })
                break
        if len(entries) >= _MAX_SECTION_HEADING_LINES:
            break
    return entries


def _locate_references(
    lines: list[str],
) -> tuple[int | None, str | None]:
    """(1-based line, form) of the references region start.

    Forms: "heading" (rendered plain-text heading),
    "thebibliography" (TeX environment begin), "bibtex_macro"
    (``\bibliography{...}`` include point). Plain headings win over
    TeX forms so rendered text keeps its v1 semantics.
    """
    for pattern, form in (
        (_REFERENCES_LINE_RE, "heading"),
        (_TEX_THEBIB_BEGIN_RE, "thebibliography"),
        (_TEX_BIBMACRO_RE, "bibtex_macro"),
    ):
        line = _line_of_first_match(pattern, lines)
        if line is not None:
            return line, form
    return None, None


def _references_entries(
    lines: list[str],
    references_line: int | None,
    end_line: int | None,
    references_form: str | None,
) -> tuple[list[tuple[int, int]], bool]:
    r"""Entry runs inside the references region, by form (D7).

    "heading": blank-line-separated runs (rendered text).
    "thebibliography": one entry per ``\bibitem`` line plus its
    continuation lines up to the next ``\bibitem`` (trailing blank
    lines trimmed); the ``\end`` line is excluded via ``end_line``.
    "bibtex_macro": zero entries — the entries live in the .bbl,
    not this source; zero is the honest projection, never a guess.

    The region is ``references_line..end_line`` (1-based; content
    excludes both boundary lines; ``end_line`` None means EOF).
    Pure derivation over the ledger's own coordinates — chunking
    consumes these as facts.
    """
    entries: list[tuple[int, int]] = []
    if references_line is None:
        return entries, False
    end_idx = end_line - 1 if end_line is not None else len(lines)
    if references_form == "bibtex_macro":
        return entries, False
    if references_form == "thebibliography":
        start: int | None = None
        for idx in range(references_line, end_idx):  # 0-based
            if _TEX_BIBITEM_RE.search(lines[idx]):
                if start is not None:
                    entries.append((start + 1, idx))
                start = idx
        if start is not None:
            last_end = end_idx  # 1-based last content line
            while last_end > start + 1 and not lines[last_end - 1].strip():
                last_end -= 1
            entries.append((start + 1, last_end))
    else:
        run_start: int | None = None
        for idx in range(references_line, end_idx):  # 0-based, after heading
            if lines[idx].strip():
                if run_start is None:
                    run_start = idx
            elif run_start is not None:
                entries.append((run_start + 1, idx))
                run_start = None
        if run_start is not None:
            entries.append((run_start + 1, end_idx))
    capped = len(entries) >= _MAX_REFERENCE_ENTRIES
    return entries[:_MAX_REFERENCE_ENTRIES], capped


def build_academic_paper_payload(original_text: str) -> dict[str, Any]:
    """Deterministic structure-fact ledger + atomic chunks (v1.7).

    Pure function; no I/O; never raises on empty text (an empty
    ledger with zero stats is a legal, honest outcome).
    """
    lines = original_text.split("\n")
    citation_lines, citation_samples = _lines_matching(
        _CITATION_RE, lines, _MAX_CITATION_LINES
    )
    tex_citation_lines, tex_citation_samples = _lines_matching(
        _TEX_CITE_LINE_RE, lines, _MAX_CITATION_LINES
    )
    # D8: whole-region display math, UNCAPPED — the chunker gets
    # the full list (protection must never depend on the sample
    # cap); the payload reports a capped sample list plus the true
    # count and a capped flag
    formula_all = _display_formula_lines(lines)
    formula_lines = formula_all[:_MAX_FORMULA_LINES]
    formula_samples = [
        lines[no - 1].strip()[:_MAX_TEXT_CHARS] for no in formula_lines
    ]
    # D9: special-character census — counts only (line lists would
    # be pure cap noise on math-heavy papers); counted on
    # comment-stripped lines so commented-out math is not counted
    stripped = [_strip_tex_comment(line) for line in lines]
    tex_subsup_lines = sum(
        1 for line in stripped if _TEX_SUBSUP_RE.search(line)
    )
    tex_symbol_lines = sum(
        1 for line in stripped if _TEX_SYMBOL_RE.search(line)
    )
    tex_accent_lines = sum(
        1 for line in stripped if _TEX_ACCENT_RE.search(line)
    )
    heading_lines, heading_texts = _lines_matching(
        _NUMBERED_HEADING_RE, lines, _MAX_HEADING_LINES
    )
    abstract_line = _line_of_first_match(_ABSTRACT_LINE_RE, lines)
    keywords_line = _line_of_first_match(_KEYWORDS_LINE_RE, lines)
    references_line, references_form = _locate_references(lines)
    section_headings = _locate_section_headings(lines)
    end_candidates = [
        e["line"]
        for e in section_headings
        if e["kind"] == "appendix"
        and references_line is not None
        and e["line"] > references_line
    ]
    if references_form == "thebibliography" and references_line is not None:
        the_end = _line_of_first_match(
            _TEX_THEBIB_END_RE, lines[references_line:]
        )
        if the_end is not None:
            # slice-local 1-based → document 1-based
            end_candidates.append(references_line + the_end)
    references_end_line = min(end_candidates) if end_candidates else None
    references_entries, references_capped = _references_entries(
        lines, references_line, references_end_line, references_form
    )
    chunk_result = chunk_academic_paper_text(
        original_text, formula_all, references_entries
    )
    chunk_dicts = [
        {"start_line": c.start_line, "end_line": c.end_line, "text": c.text}
        for c in chunk_result.chunks
    ]
    chunk_inv = check_chunk_invariants(original_text, chunk_result.chunks)

    return {
        "extractor": "academic_paper_scenario_v1_8",
        "total_lines": len(lines),
        "abstract_line": abstract_line,
        "keywords_line": keywords_line,
        "references_line": references_line,
        "references_end_line": references_end_line,
        "references_form": references_form,
        "references_entries": [
            {"start_line": start, "end_line": end}
            for start, end in references_entries
        ],
        "citation_lines": citation_lines,
        "citation_samples": citation_samples,
        "tex_citation_lines": tex_citation_lines,
        "tex_citation_samples": tex_citation_samples,
        "formula_lines": formula_lines,
        "formula_samples": formula_samples,
        "numbered_heading_lines": heading_lines,
        "numbered_heading_texts": heading_texts,
        "section_heading_lines": [e["line"] for e in section_headings],
        "section_headings": section_headings,
        "chunks": {
            "chunks": chunk_dicts,
            "stats": chunk_result.stats,
        },
        "chunk_invariants": {
            "verdict": chunk_inv.verdict,
            "missing_lines": list(chunk_inv.missing_lines),
            "integrity_violations": list(chunk_inv.integrity_violations),
            "duplicate_lines": list(chunk_inv.duplicate_lines),
            "stats": chunk_inv.stats,
        },
        "stats": {
            "citations": len(citation_lines),
            "tex_citations": len(tex_citation_lines),
            "formula_lines": len(formula_all),
            "numbered_headings": len(heading_lines),
            "references_entries": len(references_entries),
            "section_headings": len(section_headings),
            "tex_subsup_lines": tex_subsup_lines,
            "tex_symbol_lines": tex_symbol_lines,
            "tex_accent_lines": tex_accent_lines,
            "citations_capped": len(citation_lines) >= _MAX_CITATION_LINES,
            "tex_citations_capped": (
                len(tex_citation_lines) >= _MAX_CITATION_LINES
            ),
            "formula_capped": len(formula_all) > _MAX_FORMULA_LINES,
            "headings_capped": len(heading_lines) >= _MAX_HEADING_LINES,
            "references_capped": references_capped,
            "section_headings_capped": (
                len(section_headings) >= _MAX_SECTION_HEADING_LINES
            ),
        },
        # honest boundary, machine-readable
        "note": (
            "structure-fact ledger + atomic chunks: heading detection "
            "is a numbered-line heuristic and section names are "
            "shape-guarded keyword facts, never structure recovery; "
            "capped counts are bounds, not totals; the references "
            "region is references_line..references_end_line by form "
            "(heading: appendix-capped or EOF; thebibliography: "
            "\\end-capped with \\bibitem entries; bibtex_macro: line "
            "recorded, zero entries — they live in the .bbl); "
            "display-math regions span whole environments "
            "(equation/align/gather/multline/eqnarray families, "
            "\\[..\\] and $$ blocks — interiors included, chunker fed "
            "the uncapped list; the scan is comment-aware, "
            "$-stateful and macro-definition-body-guarded against "
            "phantom regions; environment alias pairs like "
            "\\bea/\\eea are tracked as regions); special-character "
            "line counts "
            "(sub/superscript, symbol macros, accents) are census "
            "facts in stats; "
            "chunks never split formula blocks or reference entries "
            "and never modify text"
        ),
    }


def attach_academic_paper_scenario(document: Any) -> None:
    """Stamp the ledger on ``metadata[SCENARIO_METADATA_KEY]``.

    Attach-only: mutates ``document.metadata`` in place, mirroring
    the runtime stamping pattern; document text fields are never
    touched.
    """
    original_text = getattr(document, "original_text", "") or ""
    document.metadata[SCENARIO_METADATA_KEY] = build_academic_paper_payload(
        original_text
    )


__all__ = [
    "SCENARIO_METADATA_KEY",
    "attach_academic_paper_scenario",
    "build_academic_paper_payload",
]
