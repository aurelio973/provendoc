"""TeX symbol tables (module-level pure data) and the deterministic
macro expansion machine.

Symbol table constants and the shared lexical helper ``_read_braced``
live here, keeping the macro → env/math → renderer → orchestrator
dependency chain unidirectional and acyclic.
"""

import re

# ============================================================
# Transcription tables (module-level pure data)
# ============================================================

# Full Greek alphabet (lowercase + uppercase + var variants)
_GREEK = {
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3",
    "Gamma": "\u0393", "delta": "\u03b4", "Delta": "\u0394",
    "epsilon": "\u03b5", "varepsilon": "\u03b5", "zeta": "\u03b6",
    "eta": "\u03b7", "theta": "\u03b8", "vartheta": "\u03d1",
    "Theta": "\u0398", "iota": "\u03b9", "kappa": "\u03ba",
    "lambda": "\u03bb", "Lambda": "\u039b", "mu": "\u03bc",
    "nu": "\u03bd", "xi": "\u03be", "Xi": "\u039e", "pi": "\u03c0",
    "varpi": "\u03d6", "Pi": "\u03a0", "rho": "\u03c1",
    "varrho": "\u03f1", "sigma": "\u03c3", "varsigma": "\u03c2",
    "Sigma": "\u03a3", "tau": "\u03c4", "upsilon": "\u03c5",
    "Upsilon": "\u03d2", "phi": "\u03c6", "varphi": "\u03d5",
    "Phi": "\u03a6", "chi": "\u03c7", "psi": "\u03c8", "Psi": "\u03a8",
    "omega": "\u03c9", "Omega": "\u03a9", "digamma": "\u03dd",
}

# Upright Greek variants (upphi/uplambda/…): Unicode makes no
# distinction, so map to the same characters.
_UPGREEK = {
    "upalpha": "\u03b1", "upbeta": "\u03b2", "upgamma": "\u03b3",
    "updelta": "\u03b4", "upepsilon": "\u03b5",
    "upvarepsilon": "\u03b5", "upzeta": "\u03b6", "upeta": "\u03b7",
    "uptheta": "\u03b8", "upvartheta": "\u03d1", "upiota": "\u03b9",
    "upkappa": "\u03ba", "uplambda": "\u03bb", "upmu": "\u03bc",
    "upnu": "\u03bd", "upxi": "\u03be", "upomicron": "\u03bf",
    "uppi": "\u03c0", "upvarpi": "\u03d6", "uprho": "\u03c1",
    "upvarrho": "\u03f1", "upsigma": "\u03c3",
    "upvarsigma": "\u03c2", "uptau": "\u03c4",
    "upupsilon": "\u03c5", "upphi": "\u03c6",
    "upvarphi": "\u03d5", "upchi": "\u03c7", "uppsi": "\u03c8",
    "upomega": "\u03c9",
}

# Operators / relations / arrows / misc (~90, BMP-safe set)
_OPERATORS = {
    "pm": "\u00b1", "mp": "\u2213", "times": "\u00d7", "div": "\u00f7",
    "ast": "\u2217", "star": "\u2605", "circ": "\u2218",
    "bullet": "\u2219", "cdot": "\u00b7",
    "leq": "\u2264", "le": "\u2264", "geq": "\u2265", "ge": "\u2265",
    "neq": "\u2260", "ne": "\u2260", "equiv": "\u2261",
    "approx": "\u2248", "sim": "\u223c", "simeq": "\u2243",
    "cong": "\u2245", "propto": "\u221d", "ll": "\u226a",
    "gg": "\u226b", "prec": "\u227a", "succ": "\u227b",
    "subset": "\u2282", "supset": "\u2283", "subseteq": "\u2286",
    "supseteq": "\u2287", "in": "\u2208", "notin": "\u2209",
    "ni": "\u220b", "cup": "\u222a", "cap": "\u2229",
    "setminus": "\u2216", "emptyset": "\u2205",
    "mid": "\u2223", "nmid": "\u2224", "divides": "\u2223",
    "varnothing": "\u2205", "forall": "\u2200", "exists": "\u2203",
    "nexists": "\u2204", "neg": "\u00ac", "lnot": "\u00ac",
    "land": "\u2227", "wedge": "\u2227", "lor": "\u2228", "vee": "\u2228",
    "oplus": "\u2295", "ominus": "\u2296", "otimes": "\u2297",
    "odot": "\u2299", "perp": "\u22a5", "parallel": "\u2225",
    "angle": "\u2220", "triangle": "\u25b3", "square": "\u25a1",
    "top": "\u22a4", "bot": "\u22a5", "vdash": "\u22a2",
    "dashv": "\u22a3", "models": "\u22a8", "therefore": "\u2234",
    "because": "\u2235", "ldots": "\u2026", "cdots": "\u22ef",
    "vdots": "\u22ee", "ddots": "\u22f1", "dots": "\u2026",
    "infty": "\u221e", "partial": "\u2202", "nabla": "\u2207",
    "surd": "\u221a", "sum": "\u2211", "prod": "\u220f",
    "coprod": "\u2210", "int": "\u222b", "iint": "\u222c",
    "iiint": "\u222d", "oint": "\u222e",
    "bigcup": "\u22c3", "bigcap": "\u22c2", "bigoplus": "\u2a01",
    "bigotimes": "\u2a02", "bigsqcup": "\u2a06", "bigvee": "\u22c1",
    "bigwedge": "\u22c0", "bigodot": "\u2a00", "biguplus": "\u2a04",
    "aleph": "\u2135", "hbar": "\u210f", "ell": "\u2113",
    "Re": "\u211c", "Im": "\u2111", "wp": "\u2118",
    "prime": "\u2032", "degree": "\u00b0", "circledast": "\u2218",
    "langle": "\u27e8", "rangle": "\u27e9",
    "lfloor": "\u230a", "rfloor": "\u230b",
    "lceil": "\u2308", "rceil": "\u2309",
    "uparrow": "\u2191", "downarrow": "\u2193", "updownarrow": "\u2195",
    "leftarrow": "\u2190", "rightarrow": "\u2192", "to": "\u2192",
    "leftrightarrow": "\u2194", "Leftarrow": "\u21d0",
    "Rightarrow": "\u21d2", "implies": "\u21d2",
    "Leftrightarrow": "\u21d4", "iff": "\u21d4",
    "mapsto": "\u21a6", "longrightarrow": "\u27f6", "longleftarrow": "\u27f5",
    "hookrightarrow": "\u21aa", "hookleftarrow": "\u21a9",
    "nearrow": "\u2197", "searrow": "\u2198",
    "swarrow": "\u2199", "nwarrow": "\u2196",
    "upuparrows": "\u21c8", "downdownarrows": "\u21ca",
}

# --- Readable view v2: standard command coverage ---
# Roman function-name family (\log → literal "log") + misc symbols;
# reuses the symbol-table mechanism in both math and text modes.
_FUNC_LITERALS = {name: name for name in (
"sin", "cos", "tan", "log", "ln", "lg", "exp", "max", "min",
"sup", "inf", "lim", "limsup", "liminf", "det", "dim", "arg",
"deg", "gcd", "lcm", "Pr", "Hom", "Ker",
"arctan", "arcsin", "arccos", "sinh", "cosh", "tanh",
"argmin", "argmax", "esssup", "essinf",
"cot", "csc", "sec", "coth", "ker",
)}
_OPERATORS_V2 = {
    "lesssim": "\u2272", "gtrsim": "\u2273", "S": "\u00a7",
    "qed": "\u220e", "eqdef": "\u225d",
    "asymp": "\u224d", "fint": "\u2a0d", "colon": ":",
    "leqslant": "\u2a7d", "geqslant": "\u2a7e",
    "smallsetminus": "\u2216",
    "subsetneq": "\u228a", "triangleleft": "\u25c1",
    "preceq": "\u227c", "nsim": "\u2241",
    # unicode-math standard mappings.
    "curvearrowright": "\u21b7", "backslash": "\u2216",
    "hookrightarrow": "\u21aa", "hookleftarrow": "\u21a9",
    # Readability: varlimsup → "lim sup"; \xrightarrow → plain arrow.
    "varlimsup": "lim sup", "varliminf": "lim inf",
    "vert": "|", "lvert": "|", "rvert": "|",
    "Vert": "\u2016", "lVert": "\u2016", "rVert": "\u2016",
    "xrightarrow": "\u2192", "xleftarrow": "\u2190",
    # Macro-body commands resolved via the symbol table: the body
    # whitelist includes _SYMBOLS keys, so a missing entry here would
    # reject the whole macro. unicode-math standard mappings.
    "rightsquigarrow": "\u21dd", "leftsquigarrow": "\u219c",
    "twoheadrightarrow": "\u21a0", "twoheadleftarrow": "\u219e",
    "heartsuit": "\u2665", "diamondsuit": "\u2666",
    "clubsuit": "\u2663", "spadesuit": "\u2660",
    "leadsto": "\u21dd",
    # \dag/\ddag appear in math mode ($^{\dag}$): without an entry
    # they stay literal and pass through to MathJax; with it they
    # are deterministically transcribed to Unicode.
    "dag": "\u2020", "ddag": "\u2021",
    "dagger": "\u2020", "ddagger": "\u2021",
}

_SYMBOLS = {
    **_GREEK, **_UPGREEK, **_OPERATORS, **_OPERATORS_V2,
    **_FUNC_LITERALS,
    # Class-file standard commands with no in-source definition but
    # language-level standard semantics (\og/\fg French quotes;
    # \l/\L Polish letters) — values are standard, not invented.
    "og": "«", "fg": "»",
    "l": "ł", "L": "Ł",
}
# Longest-first: prevents \leftarrow from being swallowed by a \left prefix.
_SYMBOL_RE = re.compile(
    r"\\(" + "|".join(sorted(_SYMBOLS, key=len, reverse=True))
    + r")(?![a-zA-Z])"
)

# Superscript/subscript character map (BMP-safe set + common
# astral subscripts i/r/u/v, kept for readability).
_SUPERSCRIPTS = {
    "0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3",
    "4": "\u2074", "5": "\u2075", "6": "\u2076", "7": "\u2077",
    "8": "\u2078", "9": "\u2079", "+": "\u207a", "-": "\u207b",
    "=": "\u207c", "(": "\u207d", ")": "\u207e", "n": "\u207f",
    "i": "\u2071", "h": "\u02b0", "j": "\u02b2", "r": "\u02b3",
    "l": "\u02e1", "s": "\u02e2", "x": "\u02e3", "w": "\u02b7",
    "y": "\u02b8",
    # Phonetic Extensions superscript letters (BMP; NFKD-verified).
    "a": "\u1d43", "b": "\u1d47", "c": "\u1d9c", "d": "\u1d48",
    "e": "\u1d49", "f": "\u1da0", "g": "\u1d4d", "k": "\u1d4f",
    "m": "\u1d50", "o": "\u1d52", "p": "\u1d56", "t": "\u1d57",
    "u": "\u1d58", "v": "\u1d5b",
    # Modifier letter capitals (U+1D2C block). C/F/Q/S/X/Y/Z have no
    # Unicode capital forms and stay literal as ^{X}.
    "A": "\u1d2c", "B": "\u1d2e", "D": "\u1d9c", "E": "\u1d31",
    "G": "\u1d33", "H": "\u1d34", "I": "\u1d35", "J": "\u1d36",
    "K": "\u1d37", "L": "\u1d38", "M": "\u1d39", "N": "\u1d3a",
    "O": "\u1d3c", "P": "\u1d3e", "R": "\u1d3f", "T": "\u1d40",
    "U": "\u1d41", "V": "\u2c7d", "W": "\u1d42",
    # X^* → X* (no superscript codepoint for *; drop the caret).
    "*": "*",
    # ^{\circ} → ° (degree sign reads as a superscript ring).
    "∘": "\u00b0",
}
_SUBSCRIPTS = {
    "0": "\u2080", "1": "\u2081", "2": "\u2082", "3": "\u2083",
    "4": "\u2084", "5": "\u2085", "6": "\u2086", "7": "\u2087",
    "8": "\u2088", "9": "\u2089", "+": "\u208a", "-": "\u208b",
    "=": "\u208c", "(": "\u208d", ")": "\u208e", "a": "\u2090",
    "e": "\u2091", "o": "\u2092", "x": "\u2093", "h": "\u2095",
    "k": "\u2096", "l": "\u2097", "m": "\u2098", "n": "\u2099",
    "p": "\u209a", "s": "\u209b", "t": "\u209c", "i": "\u1d62",
    "j": "\u2c7c", "r": "\u1d63", "u": "\u1d64", "v": "\u1d65",
}
# Nesting-aware: one level of nested braces is allowed (\sum_{t=2^{j-1}}^{2^j-1}).
# The single-char branch also matches * (identity-mapped, caret dropped).
_SCRIPT_RE = re.compile(
    r"([_^])(?:\{((?:[^{}]|\{[^{}]*\})*)\}|([a-zA-Z0-9*]))")

# BMP-safe subset for \mathbb/\mathcal/\mathfrak; the rest stay verbatim.
_MATHBB = {
    "R": "\u211d", "N": "\u2115", "Z": "\u2124", "Q": "\u2119",
    "C": "\u2102", "H": "\u210d", "P": "\u2119",
}
_MATHCAL = {
    "L": "\u2112", "H": "\u210b", "B": "\u212c", "E": "\u2130",
    "F": "\u2131", "I": "\u2110", "R": "\u211b",
}
# Accent commands (combining chars; argument is a single char/token).
_ACCENTS = {
    "hat": "\u0302", "widehat": "\u0302", "bar": "\u0304",
    "overline": "\u0304", "tilde": "\u0303", "widetilde": "\u0303",
    "vec": "\u20d7", "dot": "\u0307", "ddot": "\u0308",
    "acute": "\u0301", "grave": "\u0300", "check": "\u030c",
    "breve": "\u0306",
    # \slashed (physics package): combining solidus U+0338.
    "slashed": "\u0338",
}

# Text-mode accent commands (na\"ive → i + combining diaeresis).
# (Math-mode accents go through _ACCENTS/the font-shell pipeline; this table serves text-mode _scan/_render_plain only.)
_TEXT_ACCENTS = {
    '"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302",
    "~": "\u0303", "=": "\u0304", ".": "\u0307",
}

# Plain TeX box/table layout primitives (\hbox{...}/\halign{...}).
# Their content is real prose and $...$ math → scan the content and
# drop the shell (mirrors the _TEXT_FONTS branch); inner template
# pieces (\hfil/\cr) are dropped by _TYPO_ZERO_CMDS.
_LAYOUT_BOX_CMDS = {
    "hbox", "vbox", "vtop", "vcenter", "halign", "valign",
    "line", "leftline", "rightline", "centerline",
    "rlap", "llap", "clap", "phantom", "hphantom", "vphantom",
}

# Graphics environments (tikzpicture etc.): not narrative content;
# dropped whole and counted in graphics disclosure (matches the
# includegraphics convention).
_GRAPHICS_ENVS = {"tikzpicture", "tikzcd", "pspicture", "xy", "picture"}

# Math font wrappers (content kept as-is; mathbb/mathcal special-cased).
# One level of nested braces is allowed in the content: without it,
# \overline{E^{c}} / \mathop{\sum_{...}} shells fail to match and
# later brace-stripping fuses shell and content into \overlineE /
# \textif leaks.
_MATH_FONT_RE = re.compile(
    r"\\(mathbf|mathrm|mathit|mathsf|mathtt|mathcal|mathbb|mathfrak|"
    r"mathchoice|mathscr|mathop|mathclap|mathrlap|mathllap|"
    r"hbox|vtop|vbox|"
    r"boldsymbol|bm|operatorname\*?|text|textrm|textsf|"
    r"texttt|textit|textbf|textnormal|textup|text|emph|mbox|"
    r"hat|widehat|bar|overline|underline|vec|dot|ddot|tilde|"
    r"widetilde|acute|grave|check|breve|slashed)\{((?:[^{}]|\{[^{}]*\})*)\}"
)

# \stackrel{a}{b}: stacks a on top of b (formula annotation).
_STACKREL_RE = re.compile(
    r"\\stackrel\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
    r"\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
)

# \not\X combos (applied before symbol-table replacement; whitespace between \not and \X is allowed).
_NOT_COMBOS = {
    r"\\not\s*\\in(?![a-zA-Z])": "\u2209",
    r"\\not\s*\\subset(?![a-zA-Z])": "\u2284",
    r"\\not\s*\\subseteq": "\u2288",
    r"\\not\s*\\equiv(?![a-zA-Z])": "\u2262",
    r"\\not\s*\\le(?![a-zA-Z])": "\u226e",
    r"\\not\s*\\ge(?![a-zA-Z])": "\u226f",
    r"\\not\s*\\leq(?![a-zA-Z])": "\u226e",
    r"\\not\s*\\geq(?![a-zA-Z])": "\u226f",
    r"\\not\s*\\sim(?![a-zA-Z])": "\u2241",
    r"\\not\s*\\cong(?![a-zA-Z])": "\u2246",
    r"\\not\s*\\exists(?![a-zA-Z])": "\u2204",
    r"\\not\s*\\ni(?![a-zA-Z])": "\u220c",
}

# ============================================================
# Readable view v2: deterministic macro expansion
# ============================================================

# Declaration-style font switches (no braces): shell-stripped
# (command dropped, content kept, counted as disclosure). \em is in
# the same family as \it (legacy declaration-style italic switch).
_FONT_SWITCHES = {"bf", "it", "sl", "sc", "tt", "rm", "em"}

# Typography-primitive drop list (pure layout values, never
# narrative text). Zero-argument family (dropped directly;
# pagebreak/linebreak/displaybreak also swallow an optional [n]).
_TYPO_ZERO_CMDS = {
    "sloppy", "vfil", "vfill", "hfil", "hfill", "noindent",
    "indent", "clearpage", "cleardoublepage", "newpage",
    "smallskip", "medskip", "bigskip", "strut", "relax",
    "protect", "flushbottom", "raggedright", "raggedleft",
    "centering", "allowdisplaybreaks", "displaybreak",
    "nobreakdash", "emergencystretch", "hangindent", "hbadness",
    "vbadness", "tolerance", "pretolerance", "baselineskip",
    "pagebreak", "linebreak", "nopagebreak", "fussy",
    # halign table template pieces (\cr row separator, \noalign, ...).
    "cr", "crcr", "noalign", "offinterlineskip", "tabskip",
    "hsize", "vsize",
}
# Register/counter allocation family: followed by a single \\name.
_TYPO_NAME_CMDS = {
    "newcount", "newdimen", "newskip", "newbox", "newtoks",
    "newread", "newwrite", "newif", "newmuskip", "newinsert",
    "newfam", "newlanguage", "newsavebox", "countdef", "dimendef",
    "skipdef", "toksdef", "boxdef", "chardef", "mathchardef",
}
# Page-style family taking a single {...} argument.
_TYPO_ONE_ARG_CMDS = {
    "thispagestyle", "pagestyle", "pagenumbering", "columnstyle",
    "markright", "excludeversion", "includeversion",
    # Counter stepping: zero-output layout values;
    # setcounter/addtocounter are in the two-arg family below.
    "stepcounter", "refstepcounter",
}
# Two-{...}-argument family (register/length/counter assignment — layout values).
_TYPO_TWO_ARG_CMDS = {
    "setcounter", "addtocounter", "setlength", "addtolength",
    "settowidth", "settoheight", "settodepth", "markboth",
}
# Starred-variant family (\\vspace*{...} / \\pagebreak*[1]).
_STAR_VARIANT_CMDS = {
    "vspace", "hspace", "pagebreak", "linebreak", "newpage",
    "clearpage", "vref", "phantomsection", "thanks",
}

# Whitelist of commands allowed inside macro bodies (expandability test).
_MACRO_ALLOWED_CMDS = {
"frac", "sqrt", "binom", "left", "right", "displaystyle",
"textstyle", "scriptstyle", "scriptscriptstyle",
"limits", "nolimits", "cite", "ref", "eqref", "label", "quad",
"qquad", "ldots", "cdots", "dots", "not",
"mathrm", "mathit", "mathsf", "mathtt", "mathcal", "mathbb",
"mathfrak", "mathbf", "boldsymbol", "bm", "operatorname", "text",
"textrm", "textsf", "texttt", "textit", "textbf", "textnormal",
"textup", "emph", "mbox", "underline", "overline",
"mathop", "mathscr", "mathclap", "mathrlap", "mathllap",
"colon", "thinspace", "stackrel",
# textcolor/color are shell-stripped by the rendering layer
# (_consume_arg_cmds), so macro bodies containing them expand safely.
"textcolor", "color",
"bf", "it", "sl", "sc", "tt", "rm", "sf", "cal",
# \em is in the \it family (declaration-style font switch, shell-stripped in text mode).
"em",
# Common custom macros whose bodies contain font-size/spacing/box
# commands; all are shell-stripped or dropped in math mode, so
# expanding them is safe (no execution risk).
"tiny", "scriptsize", "footnotesize", "small", "normalsize",
"large", "Large", "LARGE", "huge", "Huge",
"hspace", "hskip", "mskip", "mkern", "vspace", "vskip", "kern",
"raisebox", "strut", "phantom", "vfil", "hfil", "cr",
"xrightarrow", "xleftarrow",
# Zero-argument section-alias macros: when the body is a sectioning
# command, the call site emits a heading candidate by reading the
# argument group on its behalf (pure structural emission, no
# control flow/IO, no execution risk).
    "part", "chapter", "section", "subsection", "subsubsection",
    "paragraph", "subparagraph",
    # Table structure commands (\multirow{...}{*}{...} bodies): a
    # missing entry would make such macros unexpandable. Expanded
    # output still goes through deterministic transcription
    # (transcribe_math swallows environment tags). No control flow/IO.
    "multirow", "multicolumn", "begin", "end", "array",
    "hline", "cline", "toprule", "midrule", "bottomrule",
} | set(_FUNC_LITERALS) | set(_ACCENTS) | set(_SYMBOLS)

# Commands forbidden in macro bodies (control flow + I/O; hard line for pure string replacement).
_MACRO_FORBIDDEN_RE = re.compile(
    r"\\(?:if[a-zA-Z]*|fi|else|or|expandafter|csname|noexpand|"
    r"futurelet|catcode|obeyspaces|obeypunct|write|openin|openout|"
    r"read|input|include|special|output)\b"
)

_MACRO_DEF_HEAD_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\*?\s*"
    r"(?:\{\\([a-zA-Z]+|\d)\}|\\([a-zA-Z]+|\d))"
    r"\s*(?:\[(\d)\])?"
    r"\s*(?:\[([^\]]*)\])?"
    r"\s*(?=\{)"
)
_MACRO_DEFOP_HEAD_RE = re.compile(
    r"\\DeclareMathOperator\*?\s*"
    r"(?:\{\\([a-zA-Z]+|\d)\}|\\([a-zA-Z]+|\d))"
    r"\s*(?=\{)"
)
_MACRO_DEF_TEX_RE = re.compile(
    r"\\def\s*\\([a-zA-Z]+|\d)\s*((?:#\d\s*)*)\{"
)

_MACRO_BODY_MAX = 200          # max body length (over = layout code, not expanded)
_MACRO_EXPAND_BUDGET = 2000    # expansion instance budget per transcription
_MACRO_TEXT_STEP_CAP = 100_000  # global text-mode expansion step cap
_MACRO_TEXT_BODY_MAX = 5000     # text-mode single-expansion output length cap


def _clean_inline_title(title, macros=None, evidence=None):
    """Fold reference commands in optional theorem/proof titles
    into labels (~ collapses to a space).

    The title is spliced directly into the "[Proof: ...]" header and
    bypasses text-mode command dispatch; \\ref{lem:x} → [lem:x],
    \\cite{k} → [k], other structure stays as-is. Document macros are
    expanded first (via ``macros``), then text-series font commands
    are shell-stripped.
    """
    t = title
    if macros:
        t = _expand_macros_in(t, macros, evidence)
    t = re.sub(r"\\eqref\s*\{([^{}]*)\}", r"[\1]", t)
    t = re.sub(
        r"\\(?:ref|cref|Cref|autoref|vref)\s*\{([^{}]*)\}", r"[\1]", t
    )
    t = re.sub(r"\\(?:label|footnote)\s*\{[^{}]*\}", "", t)
    t = re.sub(r"\\(?:cite|citep|citet)\s*\{([^{}]*)\}", r"[\1]", t)
    # Shell-strip text-series font commands (drop command, keep content).
    t = re.sub(
        r"\\(?:text|textnormal|textup|textrm|textit|textbf"
        r"|textsf|texttt|emph|mbox)\s*\{([^{}]*)\}", r"\1", t
    )
    t = t.replace("~", " ").replace("\\ ", " ")
    # TeX quote ligatures: `` → “, '' → ”.
    t = t.replace("``", "\u201c").replace("''", "\u201d")
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def _macro_body_allowed(body, macros=None, _depth=0):
    r"""Expandability test: the body contains only whitelisted
    commands, no control flow/I/O.

    Closure-aware: when a body references other document macros,
    their bodies are checked recursively — chained expansion is
    naturally supported by the fixpoint loop in _expand_macros_in,
    so the test must follow. Depth cap 6 guards against cycles
    (a cycle = unexpandable).
    """
    if len(body) > _MACRO_BODY_MAX:
        return False
    if _MACRO_FORBIDDEN_RE.search(body):
        return False
    for cmd in re.findall(r"\\([a-zA-Z]+)", body):
        if macros and cmd in macros and _depth < 6:
            if not _macro_body_allowed(
                    macros[cmd][1], macros, _depth + 1):
                return False
            continue
        if cmd not in _MACRO_ALLOWED_CMDS:
            return False
    return True


def _normalize_meta_aliases(source, stats=None):
    r"""Normalize meta-alias macros: after \newcommand{\nc}{\newcommand},
    the document defines macros via \nc{\cF}{\mathcal F}. The alias
    macro itself is correctly blocked (its body contains \newcommand,
    not whitelisted), but the collector does not recognize its call
    sites, so every custom macro in the document would go uncollected.
    Fix: rewrite alias call sites whose expansion is exactly a single
    definition command back into standard form (deterministic rewrite,
    zero execution/evaluation; the ≤3-round cap guards pathological
    alias chains). Returns (normalized source, rounds).
    """
    _META_TARGETS = ("newcommand", "renewcommand", "providecommand",
                     "DeclareMathOperator", "def")
    rounds = 0
    for _round in range(3):
        alias_map = {}
        for m in _MACRO_DEF_HEAD_RE.finditer(source):
            name = m.group(1) or m.group(2)
            nargs_s, default = m.group(3), m.group(4)
            body, _ = _read_braced(source, m.end())
            if body is None:
                continue
            body_s = body.strip()
            if (
                nargs_s is None and default is None
                and body_s.startswith("\\")
            ):
                tgt = body_s[1:]
                if tgt in _META_TARGETS:
                    alias_map[name] = tgt
        # \def\foo{\newcommand}-style aliases.
        for m in _MACRO_DEF_TEX_RE.finditer(source):
            name = m.group(1)
            body, _ = _read_braced(source, m.end() - 1)
            if body is None:
                continue
            body_s = body.strip()
            if body_s.startswith("\\") and body_s[1:] in _META_TARGETS:
                alias_map[name] = body_s[1:]
        if not alias_map:
            break
        # Rewrite call sites: \nc{\foo}{body} → \newcommand{\foo}{body}
        # (the {..}{..} groups after the alias are isomorphic to the
        # standard form, only the head differs). The trailing group
        # may also be a bare \\name, so unbraced alias definitions
        # are collected too.
        def _alias_repl(m):
            tgt = alias_map.get(m.group(1))
            if tgt is None:
                return m.group(0)
            return "\\" + tgt + m.group(2)

        alias_pat = re.compile(
            r"\\(" + "|".join(re.escape(a) for a in alias_map)
            + r")(\s*(?:\*\s*)?(?:\[\d\]\s*)?(?:\[[^\]]*\]\s*)?"
            r"(?:\\[a-zA-Z]+|(?=\{)))"
        )
        new_source = alias_pat.sub(_alias_repl, source)
        if new_source == source:
            break
        source = new_source
        rounds += 1
        if stats is not None:
            stats["meta_aliases_normalized"] = (
                stats.get("meta_aliases_normalized", 0) + 1
            )
    return source, rounds


_ENV_ALIAS_BODY_RE = re.compile(r"\\(?:begin|end)\{([a-zA-Z*]+)\}")


def _normalize_env_aliases(source):
    r"""Normalize environment open/close alias macros.

    Aliases like \def\bea{\begin{eqnarray}} / \def\eea{\end{eqnarray}}
    are blocked because their bodies contain \begin/\end (not
    whitelisted); their call sites then leak verbatim and the
    environment body goes through text cleanup (& alignment chars
    and spacing commands leak). When an alias body is exactly a
    single \begin{X}/\end{X} literal (or resolves to one through ≤3
    alias-chain levels), call sites are rewritten to that literal and
    the environment dispatcher takes over (eqnarray/align etc. enter
    the math pipeline). Definition heads stay untouched (consumed by
    _consume_macro_def, no \def\begin{...} fragment names). Purely
    deterministic rewrite, zero execution/evaluation. Returns
    (normalized source, rewrite rounds).
    """
    rewritten = 0
    for _round in range(3):
        defs = {}
        for m in _MACRO_DEF_TEX_RE.finditer(source):
            body, _ = _read_braced(source, m.end() - 1)
            if body is not None:
                defs.setdefault(m.group(1), body.strip())
        for m in _MACRO_DEF_HEAD_RE.finditer(source):
            body, _ = _read_braced(source, m.end())
            if body is not None:
                defs.setdefault(m.group(1) or m.group(2), body.strip())
        # Alias chain resolution: name → \begin{X}/\end{X} literal.
        literal = {}
        for _ in range(4):
            changed = False
            for name, body in defs.items():
                if name in literal or not body.startswith("\\"):
                    continue
                m2 = _ENV_ALIAS_BODY_RE.fullmatch(body)
                if m2:
                    literal[name] = body
                    changed = True
                else:
                    tgt = body[1:]
                    if tgt in literal:
                        literal[name] = literal[tgt]
                        changed = True
            if not changed:
                break
        if not literal:
            break
        names = sorted(literal, key=len, reverse=True)
        alt = "|".join(re.escape(nm) for nm in names)
        # Aliases inside definition heads (\def\name{ or
        # \newcommand{\name}) are not rewritten, to avoid registering
        # a \def\begin{eqnarray}{...} fragment macro that would break
        # the \begin dispatch.
        pat = re.compile(
            r"(?P<defhead>\\(?:def|newcommand|renewcommand|providecommand)"
            r"\*?\s*\{?\s*)\\(?:" + alt + r")(?![a-zA-Z])"
            r"|\\(?P<call>" + alt + r")(?![a-zA-Z])"
        )

        def _repl(m):
            if m.group("call"):
                return literal[m.group("call")]
            return m.group(0)

        new_source = pat.sub(_repl, source)
        if new_source == source:
            break
        rewritten += 1
        source = new_source
    return source, rewritten


def _collect_macro_defs(source):
    """Collect macro definitions (deterministic expansion, readable
    layer only; the evidence ledger is not aware).

    Returns (macros, stats): macros = {name: (nargs, body)};
    stats = defined / redefined / unexpandable / unexpandable_samples.
    """
    macros = {}
    stats = {"defined": 0, "redefined": 0, "unexpandable": 0,
             "unexpandable_samples": []}
    # Structurally valid candidates; the whitelist test is deferred
    # (closure checking needs the full macro table).
    pending = []

    # Normalize meta aliases first (rewrite call sites to standard
    # definition form, then collect).
    source, _ = _normalize_meta_aliases(source, stats)

    def _register(name, nargs, default, body):
        if not name:
            return
        # Strip font-size layout shells from macro bodies (pure
        # layout, zero semantics; what remains may be a bare {#1}).
        body = re.sub(
            r"\\fontsize\s*\{[^{}]*\}\s*\{[^{}]*\}\s*(?:\\selectfont\b)?",
            "", body)
        body = re.sub(r"\\selectfont(?![a-zA-Z])", "", body)
        stats["defined"] += 1
        # Argument cap is 9 (TeX \newcommand semantic limit);
        # _expand_macros_in reads arguments generically by nargs.
        # The whitelist test is unaffected.
        ok = default is None and nargs is not None and nargs <= 9
        if ok and nargs == 0 and "#" in body:
            ok = False
        if ok:
            for k in re.findall(r"#(\d)", body):
                if int(k) > nargs:
                    ok = False
                    break
        if ok:
            pending.append((name, nargs, body))
        else:
            stats["unexpandable"] += 1
            samples = stats["unexpandable_samples"]
            if len(samples) < 12 and ("\\" + name) not in samples:
                samples.append("\\" + name)

    for m in _MACRO_DEF_HEAD_RE.finditer(source):
        name = m.group(1) or m.group(2)
        nargs_s, default = m.group(3), m.group(4)
        nargs = int(nargs_s) if nargs_s else 0
        body, _ = _read_braced(source, m.end())
        if body is None:
            continue
        _register(name, nargs, default, body)

    for m in _MACRO_DEFOP_HEAD_RE.finditer(source):
        name = m.group(1) or m.group(2)
        body, _ = _read_braced(source, m.end())
        if body is None:
            continue
        # DeclareMathOperator: the body is a roman operator name; literal replacement suffices.
        _register(name, 0, None, body.strip())

    for m in _MACRO_DEF_TEX_RE.finditer(source):
        name = m.group(1)
        params = m.group(2) or ""
        nargs = len(re.findall(r"#\d", params))
        body, _ = _read_braced(source, m.end() - 1)
        if body is None:
            continue
        _register(name, nargs, None, body)

    # Closure-aware whitelist: the test runs on the full candidate
    # set (mutual-reference chains and self-loops are both covered).
    full = {n: (na, b) for n, na, b in pending}
    for name, nargs, body in pending:
        if _macro_body_allowed(body, full):
            if name in macros:
                stats["redefined"] += 1
            macros[name] = (nargs, body)
        else:
            stats["unexpandable"] += 1
            samples = stats["unexpandable_samples"]
            if len(samples) < 12 and ("\\" + name) not in samples:
                samples.append("\\" + name)
    return macros, stats


def _expand_macros_in(s, macros, evidence):
    """Macro expansion (pure string replacement; round/budget guards, loops to a fixpoint)."""
    if not macros:
        return s
    names = sorted(macros, key=len, reverse=True)
    pat = re.compile(
        r"\\(" + "|".join(re.escape(n) for n in names)
        + r")(?![a-zA-Z])"
    )
    total = 0
    for _round in range(20):
        if not pat.search(s):
            break
        out = []
        i = 0
        n = len(s)
        changed = False
        while i < n:
            mm = pat.match(s, i)
            if mm is None:
                nxt = s.find("\\", i + (1 if s[i] == "\\" else 0))
                if nxt == -1:
                    out.append(s[i:])
                    break
                out.append(s[i:nxt])
                i = nxt
                continue
            name = mm.group(1)
            nargs, body = macros[name]
            j = mm.end()
            args = []
            ok = True
            for _ in range(nargs):
                while j < n and s[j] in " \t":
                    j += 1
                a, a_end = _read_braced(s, j)
                if a is None and j < n and (
                    s[j].isalnum() or s[j] == "\\"
                ):
                    if s[j] == "\\":
                        sm = re.match(r"\\[a-zA-Z]+", s[j:])
                        if sm:
                            a, a_end = sm.group(0), j + sm.end()
                    elif s[j].isalnum():
                        a, a_end = s[j], j + 1
                if a is None:
                    ok = False
                    break
                args.append(a)
                j = a_end
            if not ok:
                out.append(mm.group(0))
                i = mm.end()
                continue
            repl = body
            for k, a in enumerate(args, 1):
                repl = repl.replace("#" + str(k), a)
            # Prefix a space when the expanded body starts with a
            # letter/digit: the `\` token boundary of the macro name
            # vanishes with the replacement, so adjacent commands
            # like \partial\Bword fuse into \partialB (which the
            # negative lookahead would block from symbol
            # transcription). Math-mode spacing is absorbed by the
            # trailing normalization; no prefix right after _/^{
            # (script syntax must survive).
            if (
                repl and repl[0].isalnum()
                and not (i > 0 and s[i - 1] in "_^{")
            ):
                repl = " " + repl
            out.append(repl)
            # TeX swallows whitespace after macro names: insert a
            # space when a letter follows the expansion (prevents
            # token fusion).
            if (
                j < n and s[j].isalpha()
                and repl and repl[-1].isalnum()
            ):
                out.append(" ")
            total += 1
            changed = True
            i = j
            if total >= _MACRO_EXPAND_BUDGET:
                out.append(s[i:])
                i = n
                break
        s = "".join(out)
        if not changed or total >= _MACRO_EXPAND_BUDGET:
            break
    if total and isinstance(evidence, dict):
        evidence["macros_expanded"] = (
            evidence.get("macros_expanded", 0) + total
        )
    return s

def _read_braced(text, start):
    """Read the {...} content at ``start`` (nesting- and escape-aware); returns (content, end)."""
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
