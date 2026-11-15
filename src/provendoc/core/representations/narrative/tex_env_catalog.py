"""TeX environment dispatch tables (module-level pure data).

Shared by the math-transcription stage and the renderer stage (both
consume this table; the two stages do not depend on each other).
"""

import re

# ============================================================
# Environment dispatch tables
# ============================================================

_VERBATIM_ENVS = {
    "verbatim", "verbatim*", "Verbatim", "lstlisting", "minted",
}
_LIST_ENVS = {"itemize", "enumerate", "description", "list"}
_FLOAT_ENVS = {
    "figure", "figure*", "table", "table*", "wrapfigure",
    "wraptable", "sidewaysfigure", "sidewaystable", "longtable",
    "tabularx", "threeparttable", "subfigure", "subtable",
}
# Theorem family (common \newtheorem predefined names; custom names
# go through the generic container recursion)
_THEOREM_ENVS = {
    "theorem", "lemma", "proposition", "corollary", "definition",
    "remark", "example", "proof", "note", "conjecture", "problem",
    "exercise", "solution", "fact", "claim", "assumption",
    "notation", "observation", "abstract", "acknowledgements",
    "acknowledgments",
}
# Matrix / nested-structure environments (display-math family additions)
_MATRIX_ENVS = {
    "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "Bmatrix",
    "smallmatrix", "cases", "dcases", "array", "aligned", "gathered",
    "alignat", "alignedat", "split", "subarray", "rcases",
}
_TABULAR_ENVS = {"tabular", "tabular*", "longtable", "tabularx"}

# Math environments that need their environment shell to render
# (& alignment / matrix delimiter family) — under the latex profile
# they are rebuilt verbatim as \begin{env}...\end{env} inside $$
# (a bare & inside the body is invalid LaTeX the renderer cannot
# handle).
_LATEX_SHELL_ENVS = _MATRIX_ENVS | {"align", "eqnarray"}

# Readable-view math profile defaults to "latex" — math is emitted
# verbatim wrapped in $/$$ and rendered downstream by MathJax/KaTeX
# as real formulas; no Unicode linear transcription. "unicode" is
# the v2 transcription profile (the full transcription machine is
# kept; render_tex_narrative(math_mode="unicode") switches back).
_TEX_MATH_MODE_DEFAULT = "latex"

# Preamble commands stripped line-by-line in fragment mode
_PREAMBLE_LINE_RE = re.compile(
    r"^\s*\\(?:documentclass|usepackage|RequirePackage|newcommand|"
    r"renewcommand|providecommand|DeclareMathOperator|"
    r"DeclarePairedDelimiter|newenvironment|renewenvironment|def|"
    r"input|include|PassOptionsToPackage|makeatletter|makeatother|"
    r"bibliographystyle|bibliography|hyphenation|title|author|date|"
    r"lstset|graphicspath)\b"
)

# Sectioning commands → level (same mapping as tex_parser._SECTION_COMMANDS)
_SECTION_LEVELS = {
    "part": 1, "chapter": 1, "section": 2, "subsection": 3,
    "subsubsection": 4, "paragraph": 5, "subparagraph": 6,
}
_SECTION_RE = re.compile(
    r"\\(" + "|".join(_SECTION_LEVELS) + r")\*?\s*"
    r"(?:\[[^\]]*\])?\s*\{"
)

# Text commands (single-argument family)
_CITE_RE = re.compile(r"\\(cite[ptpalt]*|citeauthor|citeyear|citealp)\{([^{}]*)\}")
_REF_RE = re.compile(r"\\(autoref|pageref|Cref|cref|vref|ref)\{([^{}]*)\}")
_EQREF_RE = re.compile(r"\\eqref\{([^{}]*)\}")
_LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")
_FOOTNOTE_RE = re.compile(r"\\footnote\{([^{}]*)\}")
_URL_RE = re.compile(r"\\(?:url|path)\{([^{}]*)\}")
_HREF_RE = re.compile(r"\\href\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
_CAPTION_RE = re.compile(r"\\caption\*?\s*(?:\[[^\]]*\])?\s*\{")
# Text-mode command heads (\mbox/\text family) — their argument body
# is a text zone: in-body $...$ and font declarations are natively
# supported by MathJax textmacros; masked during math-mode cleanup.
_TEXT_ARG_CMDS_RE = re.compile(
    r"\\(?:mbox|hbox|text|textrm|texttt|textbf|textit|textsc|"
    r"textsf|textnormal|textmd|textup|emph)\s*\{")
# Display-math splitting inside list items (the tempered dot blocks
# nesting across same-name environments); previously the whole \item
# body went through _render_plain text cleanup, which mangled
# equation interiors (e.g. stripping \end{equation} into a stray
# ".equation" tail).
_ITEM_DISPLAY_RE = re.compile(
    r"\\begin\{([a-zA-Z*]+)\}"
    r"((?:(?!\\begin\{\1\}|\\end\{\1\}).)*?)\\end\{\1\}"
    r"|\\\[((?:(?!\\\]).)*?)\\\]"
    r"|\$\$((?:(?!\$\$).)*?)\$\$",
    re.S,
)
# Layout commands dropped outright when argumentless (empty string)
_DROP_CMDS = {
    "noindent", "indent", "centering", "raggedright", "raggedleft",
    "flushleft", "flushright", "bigskip", "medskip", "smallskip",
    "clearpage", "cleardoublepage", "newpage", "pagebreak",
    "nopagebreak", "hfill", "vfill", "dotfill", "protect", "relax",
    "strut", "today", "printbibliography", "tableofcontents",
    "listoffigures", "listoftables", "appendix", "normalfont",
    "selectfont", "nohyphens", "sloppy", "fussy", "onecolumn",
    "twocolumn", "hrule", "vrule", "smallskip", "maketitle",
    "large", "Large", "LARGE", "huge", "Huge", "small",
    "footnotesize", "scriptsize", "tiny", "normalsize",
    "itshape", "bfseries", "mdseries", "upshape", "scshape",
    "slshape", "ttfamily", "sffamily", "rmfamily", "em",
    "Small",  # smfart-style size switch (between \small and \normalsize)
    "resetcounter", "thicklines", "thinlines",
}
# Text font wrappers → recurse into contents
_TEXT_FONTS = {
    "textbf", "textit", "texttt", "emph", "textrm", "textsf",
    "textsc", "textup", "textnormal", "underline", "mbox",
    "hbox", "fbox", "textsl", "textmd", "textrm",
}

_CMD_RE = re.compile(r"\\([a-zA-Z]+)")
_BEGIN_ENV_RE = re.compile(r"\\begin\s*\{([a-zA-Z*]+)\}")
_END_ENV_RE = re.compile(r"\\end\s*\{([a-zA-Z*]+)\}")
_TAB_BEGIN_RE = re.compile(r"\\begin\{(tabular\*?|longtable|tabularx)\}")
# begin/end scanned together (environment pairing depth counting)
_ENV_TAG_RE = re.compile(r"\\(begin|end)\s*\{([a-zA-Z*]+)\}")
_TAG_RE = re.compile(r"\\tag\{([^{}]*)\}")
# Spacing pseudo-notation \[1mm] / \[2ex] (common in Word-to-TeX
# conversions); the \\[2mm] variant is handled by the \\
# branch — this covers standalone occurrences.
_SPACING_BRACKET_RE = re.compile(r"\\\[\s*\d+\s*[a-zA-Z]+\s*\]")
