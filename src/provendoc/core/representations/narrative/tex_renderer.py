"""Single-pass TeX source → narrative-text renderer (``_TexRenderer``)."""

import re

from provendoc.core.parsers.narrative.tex_structure import (
    _MATH_ENVIRONMENTS,
)
from provendoc.core.representations.narrative.tex_env_catalog import (  # noqa: F401
    _BEGIN_ENV_RE,
    _CAPTION_RE,
    _CMD_RE,
    _DROP_CMDS,
    _ENV_TAG_RE,
    _FLOAT_ENVS,
    _INCLUDEGRAPHICS_RE,
    _ITEM_DISPLAY_RE,
    _LATEX_SHELL_ENVS,
    _LIST_ENVS,
    _MATRIX_ENVS,
    _PREAMBLE_LINE_RE,
    _SECTION_LEVELS,
    _SECTION_RE,
    _SPACING_BRACKET_RE,
    _TAB_BEGIN_RE,
    _TABULAR_ENVS,
    _TEX_MATH_MODE_DEFAULT,
    _TEXT_FONTS,
    _THEOREM_ENVS,
    _VERBATIM_ENVS,
)
from provendoc.core.representations.narrative.tex_macro_expansion import (  # noqa: F401
    _FONT_SWITCHES,
    _GRAPHICS_ENVS,
    _LAYOUT_BOX_CMDS,
    _MACRO_TEXT_BODY_MAX,
    _MACRO_TEXT_STEP_CAP,
    _STAR_VARIANT_CMDS,
    _SYMBOL_RE,
    _SYMBOLS,
    _TEXT_ACCENTS,
    _TYPO_NAME_CMDS,
    _TYPO_ONE_ARG_CMDS,
    _TYPO_TWO_ARG_CMDS,
    _TYPO_ZERO_CMDS,
    _clean_inline_title,
    _expand_macros_in,
    _read_braced,
)
from provendoc.core.representations.narrative.tex_math_transcription import (  # noqa: F401
    _clean_math_latex,
    _has_grouped_amp,
    _rewrite_xymatrix,
    _tabular_cell_text,
    _tex_in_comment,
    transcribe_math,
)
from provendoc.core.structure.heading_patterns import HeadingCandidate

# ============================================================
# source → narrative-text renderer
# ============================================================


class _TexRenderer:
    """Single-pass cursor renderer: token stream → narrative text + heading candidates + math spans."""

    MAX_DEPTH = 8

    def __init__(self):
        self.parts = []
        self._len = 0
        self.candidates = []
        self.display_math_blocks = []  # (start, end, text)
        self.ev = {
            "extractor": "tex_render_v1",
            "display_math_blocks": 0,
            "inline_math_count": 0,
            "verbatim_blocks": 0,
            "comments_stripped": 0,
            "preamble_stripped": False,
            "graphics": 0,
            "unknown_commands": 0,
            "unknown_command_samples": [],
            "environments": {},
            # --- macro disclosure counters (readable view v2) ---
            "macros_defined": 0,
            "macros_expanded": 0,
            "macros_redefined": 0,
            "macros_unexpandable": 0,
            "macros_unexpandable_samples": [],
            "declarative_font_switches": 0,
            "bibitem_entries": 0,
        }
        self.macros = {}          # name -> (nargs, body)
        self._macro_steps = 0     # global text-mode expansion steps
        self._pending_title = {}
        self._title_emitted = False  # \maketitle title block emitted
        self.math_mode = _TEX_MATH_MODE_DEFAULT  # latex|unicode

    # ---------- emit helpers ----------

    def _emit(self, s):
        if not s:
            return
        self.parts.append(s)
        self._len += len(s)

    def _at_paragraph_break(self):
        """Whether the output already sits at a paragraph boundary
        (empty, or ends with a blank line)."""
        if self._len == 0:
            return True
        return self.parts[-1].endswith("\n\n")

    def _emit_block(self, s):
        """Emit ``s`` as a standalone paragraph (break-aware: no
        stacked blank lines, so offsets stay stable).

        Returns (start, end) — offsets of ``s`` in the final text
        (trailing blank lines excluded), or None for empty text.
        Stacked blank lines would shift HeadingCandidate / display_math
        offsets after the final normalize, so the prefix newline is
        emitted only when needed.
        """
        s = s.strip()
        if not s:
            return None
        if not self._at_paragraph_break():
            self._emit("\n\n")
        start = self._len
        self._emit(s)
        end = self._len
        self._emit("\n\n")
        return start, end

    def _env_count(self, env):
        envs = self.ev["environments"]
        envs[env] = envs.get(env, 0) + 1

    def _count_unknown(self, cmd):
        self.ev["unknown_commands"] += 1
        samples = self.ev["unknown_command_samples"]
        if len(samples) < 12 and "\\" + cmd not in samples:
            samples.append("\\" + cmd)

    # ---------- preamble / body split ----------

    @staticmethod
    def _commented(prefix):
        r"""Whether the inline prefix has already entered a TeX comment.

        An even number (incl. 0) of leading backslashes before % makes
        it a comment marker; an odd number means a literal \%. In \\%
        the \\ is a control sequence, so % still opens a comment.
        """
        i = prefix.find("%")
        while i != -1:
            bs = 0
            j = i - 1
            while j >= 0 and prefix[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                return True
            i = prefix.find("%", i + 1)
        return False

    @classmethod
    def _find_doc_boundary(cls, source, pattern):
        r"""First document-boundary command not inside a comment.

        A commented-out %\end{document} (a superseded draft ending)
        must not terminate the body — matching it would silently
        drop everything after it.
        """
        for m in re.finditer(pattern, source):
            line_start = source.rfind("\n", 0, m.start()) + 1
            if cls._commented(source[line_start:m.start()]):
                continue
            return m
        return None

    def _split_preamble(self, source):
        m = self._find_doc_boundary(source, r"\\begin\s*\{document\}")
        title_meta = {}
        if m:
            pre = source[:m.start()]
            body_start = m.end()
            end_m = self._find_doc_boundary(
                source[body_start:], r"\\end\s*\{document\}"
            )
            body_end = (
                body_start + end_m.start() if end_m else len(source)
            )
            self.ev["preamble_stripped"] = True
            title_meta = self._preamble_titles(pre)
            return source[body_start:body_end], title_meta
        # fragment: strip preamble-style lines (no \begin{document} anchor)
        kept = []
        stripped = 0
        for ln in source.split("\n"):
            if _PREAMBLE_LINE_RE.match(ln):
                stripped += 1
                continue
            kept.append(ln)
        if stripped:
            self.ev["preamble_stripped"] = True
            self.ev["preamble_lines_stripped"] = stripped
        return "\n".join(kept), {}

    @staticmethod
    def _preamble_titles(pre):
        meta = {}
        # optional [..] arg may sit between command and {;
        # \author can repeat per author (amsart)
        m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*(?=\{)", pre)
        if m:
            content, _ = _read_braced(pre, m.end())
            if content:
                meta["title"] = content
        authors = []
        for m in re.finditer(
            r"\\author\s*(?:\[[^\]]*\])?\s*(?=\{)", pre
        ):
            content, _ = _read_braced(pre, m.end())
            if content:
                authors.append(content)
        if authors:
            meta["author"] = authors
        m = re.search(r"\\date\s*(?:\[[^\]]*\])?\s*(?=\{)", pre)
        if m:
            content, _ = _read_braced(pre, m.end())
            if content:
                meta["date"] = content
        # ams frontmatter metadata (repeatable) → title block
        for key in ("address", "email", "keywords", "subjclass",
                    "dedicating", "dedication"):
            for km in re.finditer(
                r"\\" + key + r"\s*(?:\[[^\]]*\])?\s*(?=\{)", pre
            ):
                content, _ = _read_braced(pre, km.end())
                if content:
                    meta.setdefault(key, []).append(content)
        return meta

    # ---------- title block ----------

    @staticmethod
    def _skip_opt(src, j):
        """Skip an optional [..] argument (with surrounding whitespace);
        return j unchanged when absent."""
        n = len(src)
        i = j
        while i < n and src[i] in " \t":
            i += 1
        if i < n and src[i] == "[":
            k = src.find("]", i)
            if k != -1:
                i = k + 1
                while i < n and src[i] in " \t":
                    i += 1
                return i
        return j

    def _emit_title_block(self, title_meta):
        if not title_meta:
            return
        self._title_emitted = True
        lines = []
        if "title" in title_meta:
            lines.append(self._render_title_field(title_meta["title"]))
        if "author" in title_meta:
            authors = title_meta["author"]
            if isinstance(authors, str):
                authors = [authors]
            for a in authors:
                lines.append(self._render_title_field(a))
        # frontmatter metadata joins the title block
        for key, label in (
            ("address", None), ("email", None),
            ("keywords", "关键词"), ("subjclass", "分类"),
            ("dedication", "题献"), ("dedicating", "题献"),
        ):
            vals = title_meta.get(key) or []
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                r = self._render_plain(v)
                if r:
                    lines.append((label + ": " + r) if label else r)
        if "date" in title_meta:
            lines.append(self._render_plain(title_meta["date"]))
        lines = [ln.strip() for ln in lines if ln.strip()]
        if lines:
            self._emit_block("\n".join(lines))

    def _render_title_field(self, s):
        """Render a title field over multiple lines: \\ and \\And
        separators become line breaks."""
        s = re.sub(r"\\(?:AND|And|and)\b", "\n", s)
        s = s.replace("\\\\", "\n")
        out = []
        for ln in s.split("\n"):
            r = self._render_plain(ln)
            if r:
                out.append(r)
        return "\n".join(out)

    # ---------- environment scanning ----------

    @staticmethod
    def _find_env_end(src, i, env):
        """``i`` sits after \\begin{env}; return (body_start, body_end,
        end_after, closed) — counting nested same-name begin/end pairs.

        Without nesting awareness the outer env would match the inner
        \\end, and the leftover \\end{...} leaks into verbatim output.
        """
        body_start = i
        depth = 1
        for m in _ENV_TAG_RE.finditer(src, body_start):
            if m.group(2) != env:
                continue
            # commented-out begin/end tags do not pair
            # (%\end{table} shape, see _tex_in_comment)
            if _tex_in_comment(src, m.start()):
                continue
            if m.group(1) == "begin":
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    return (body_start, m.start(), m.end(), True)
        return body_start, len(src), len(src), False

    # ---------- main scan ----------

    @staticmethod
    def _find_math_close(src, start, n):
        """Closing $ of inline math must sit at brace depth 0 (TeX:
        a nested $ inside \\mbox{...} does not close the outer math).
        Escaped \\$ is skipped; with no depth-0 $ anywhere, fall back
        to the first $ (pathological-input fallback, legacy behavior)."""
        depth = 0
        i = start
        first = -1
        while i < n:
            ch = src[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
            elif ch == "$":
                if depth == 0:
                    return i
                if first == -1:
                    first = i
            i += 1
        return first

    def _scan(self, src, depth=0, start=0, end=None):
        """Token scan in text mode.

        Structural tokens (envs / display math / sections) are handled
        only at depth==0; deeper recursion (font wrappers, list items,
        theorem bodies) uses the inline shapes.
        """
        if end is None:
            end = len(src)
        structural = depth == 0
        i = start
        n = end
        while i < n:
            ch = src[i]

            # comment: % through end of line (escaped \% doesn't count)
            if ch == "%" and (i == 0 or src[i - 1] != "\\"):
                nl = src.find("\n", i)
                i = n if (nl == -1 or nl >= n) else nl
                self.ev["comments_stripped"] += 1
                continue

            # \begin{env} dispatch at any depth — an equation inside a
            # theorem/list body must not fall into the unknown-command
            # path as shell-stripped garbage
            if src.startswith("\\begin{", i):
                m = _BEGIN_ENV_RE.match(src, i)
                if m:
                    i = self._dispatch_env(src, i, m, depth)
                    continue

            # spacing pseudo-token \[1mm] / \[2ex] (Word conversion
            # artifact): consume so the display delimiter isn't
            # mispaired and left to swallow the following text
            if src.startswith("\\[", i):
                m_sp = _SPACING_BRACKET_RE.match(src, i)
                if m_sp:
                    i = m_sp.end()
                    continue

            # display math: \[ ... \] (any depth, same reason)
            if src.startswith("\\[", i):
                close = src.find("\\]", i + 2)
                if close != -1:
                    self._emit_display(
                        src[i + 2:close], "bracket"
                    )
                    i = close + 2
                    continue

            # display math: $$ ... $$ (any depth, same reason)
            if src.startswith("$$", i):
                close = src.find("$$", i + 2)
                if close != -1:
                    self._emit_display(src[i + 2:close], "dollar")
                    i = close + 2
                    continue

            # inline math: \( ... \)
            if src.startswith("\\(", i):
                close = src.find("\\)", i + 2)
                if close != -1:
                    self._emit_inline(src[i + 2:close])
                    i = close + 2
                    continue

            # inline math: $ ... $
            if ch == "$" and (i == 0 or src[i - 1] != "\\"):
                close = self._find_math_close(src, i + 1, n)
                if close != -1 and close < n:
                    self._emit_inline(src[i + 1:close])
                    i = close + 1
                    continue

            # \\ line break
            if src.startswith("\\\\", i) and ch == "\\":
                # \\[2mm] spacing variant
                j = i + 2
                if j < n and src[j] == "[":
                    k = src.find("]", j)
                    j = n if k == -1 else k + 1
                self._emit("\n")
                i = j
                continue

            # text-mode accents (na\"ive → naive+combining; \'ecole → école)
            if ch == "\\" and i + 1 < n and src[i + 1] in _TEXT_ACCENTS:
                mark = _TEXT_ACCENTS[src[i + 1]]
                j = i + 2
                if j < n and src[j] == "{":
                    content, c_end = _read_braced(src, j)
                    if content is not None and content[:1].isalpha():
                        self._emit(content[:1] + mark + content[1:])
                        i = c_end
                        continue
                elif j < n and src[j].isalpha():
                    self._emit(src[j] + mark)
                    i = j + 1
                    continue
                # no-letter argument (rare) → generic verbatim branch

            # sectioning commands (structural layer)
            if structural:
                m = _SECTION_RE.match(src, i)
                if m:
                    title, brace_end = _read_braced(src, m.end() - 1)
                    if title is None:
                        i = m.end()
                        continue
                    level = _SECTION_LEVELS.get(m.group(1))
                    if level is None:
                        i = brace_end
                        continue
                    rendered = self._render_plain(title)
                    if rendered:
                        self._emit_heading(rendered, level)
                    i = brace_end
                    continue

            # \verb|...| / \verb*|...| (delimiter = any non-letter)
            if (
                src.startswith("\\verb", i)
                and i + 5 < n
                and not src[i + 5].isalpha()
            ):
                j = i + 5
                if j < n and src[j] == "*":
                    j += 1
                if j < n:
                    delim = src[j]
                    close = src.find(delim, j + 1)
                    if close != -1 and close < n:
                        self._emit(src[j + 1:close])
                        i = close + 1
                        continue

            # \maketitle
            if src.startswith("\\maketitle", i):
                self._emit_title_block(self._pending_title or {})
                i += len("\\maketitle")
                continue

            # generic command dispatch
            if ch == "\\":
                m = _CMD_RE.match(src, i)
                if m:
                    i = self._dispatch_cmd(src, i, m, depth)
                    continue
                # single-char escapes (\, \; \!) emit nothing; \ +
                # newline is control space too (swallow so a literal \
                # never leaks as a standalone text line); \@ likewise.
                # \ + space is a forced TeX space (loc.\ cit.) → " ".
                if i + 1 < n and src[i + 1] == " ":
                    self._emit(" ")
                    i += 2
                    continue
                # \- is a zero-width discretionary hyphen — dropped
                # with the other single-char escapes
                if i + 1 < n and src[i + 1] in ",;:! \n\r@-":
                    i += 2
                    continue
                # literal escapes (\% \{ \} \_ \& \# \$) typeset as the
                # bare character (emitted chars are not rescanned; the
                # comment branch above only matches in the source)
                if i + 1 < n and src[i + 1] in "%{}_&#$":
                    self._emit(src[i + 1])
                    i += 2
                    continue
                self._emit(src[i:i + 2])
                i += 2
                continue

            # non-breaking space
            if ch == "~":
                self._emit(" ")
                i += 1
                continue

            # TeX quote ligatures (`` → “ / '' → ”)
            if ch == "`" and src.startswith("``", i):
                self._emit("\u201c")
                i += 2
                continue
            if ch == "'" and src.startswith("''", i):
                self._emit("\u201d")
                i += 2
                continue

            # bare # (halign template placeholder) is pure typesetting;
            # the escaped \\# is handled by the \\ branch above
            if ch == "#":
                i += 1
                continue

            # grouping braces are stripped (content stays in the stream)
            if ch == "{" or ch == "}":
                i += 1
                continue

            self._emit(ch)
            i += 1

    # ---------- environment dispatch ----------

    def _dispatch_env(self, src, i, m, depth):
        env = m.group(1)
        base = env.rstrip("*")
        body_start, body_end, end_after, closed = self._find_env_end(
            src, m.end(), env
        )
        self._env_count(env)
        if not closed:
            self.ev.setdefault("unclosed_environments", []).append(env)
        body = src[body_start:body_end]

        if base in _MATH_ENVIRONMENTS or base in _MATRIX_ENVS:
            self._emit_display(body, env)
        elif base in _VERBATIM_ENVS:
            self.ev["verbatim_blocks"] += 1
            self._emit_block("```\n" + body.strip("\n") + "\n```")
        elif base in _LIST_ENVS:
            self._render_list(body, base)
        elif base in _FLOAT_ENVS:
            self._render_float(body, base)
        elif base in _TABULAR_ENVS:
            self._render_tabular(body)
        elif base in _GRAPHICS_ENVS:
            # tikz/pspicture/picture graphics environments are not
            # narrative content — dropped whole, counted in the
            # graphics disclosure (same convention as includegraphics)
            self.ev["graphics"] += 1
        elif base in ("comment", "ignore"):
            # comment env content is invisible in compiled LaTeX —
            # dropped whole
            self.ev["comment_envs_dropped"] = (
                self.ev.get("comment_envs_dropped", 0) + 1
            )
        elif base in ("appendix", "appendices"):
            # appendix is a structural wrapper: pass through at depth=0
            # so inner \section commands stay effective
            self._scan(body, 0)
        elif base == "thebibliography":
            self._render_bibliography(body)
        elif base in _THEOREM_ENVS:
            self._render_theorem(body, base)
        else:
            # generic container (custom env): scan the content
            # recursively, drop the shell. Leading length-with-unit
            # argument groups are consumed conservatively (bare numbers
            # left untouched), disclosed in ev["env_args_dropped"]
            k = 0
            while True:
                nk = k
                while nk < len(body) and body[nk] in " \t\r\n":
                    nk += 1
                content, c_end = _read_braced(body, nk)
                if content is None:
                    break
                if not re.fullmatch(
                    r"[-+.\d\s]+(?:cm|mm|pt|em|ex|in|bp|pc|sp|mu|%)",
                    content,
                ):
                    break
                self.ev["env_args_dropped"] = (
                    self.ev.get("env_args_dropped", 0) + 1
                )
                k = c_end
            rest = body[k:]
            # a custom env whose body starts with \item renders as an
            # unnumbered list
            if re.match(r"\s*\\item\b", rest):
                self._render_list(rest, "itemize")
            else:
                self._scan(rest, depth + 1)
        return end_after

    def _render_item(self, piece):
        r"""Extract display math from an \item body, then clean the text.

        Math segments (equation/align/$$/\[\]) go through the math
        path; non-math envs (nested lists etc.) stay in the text
        segment. Returns (item text, math lines).
        """
        math_lines = []
        parts = []
        pos = 0
        for m in _ITEM_DISPLAY_RE.finditer(piece):
            env_kind = None
            if m.group(1) is not None:
                base = m.group(1).rstrip("*")
                env_kind = m.group(1)
                if base not in _MATH_ENVIRONMENTS and base not in _MATRIX_ENVS:
                    continue  # non-math envs stay in the text segment
                inner = m.group(2)
            else:
                # \[...\] / $$...$$ bodies live in groups 3 / 4
                inner = m.group(2) or m.group(3) or m.group(4)
            seg = piece[pos:m.start()]
            if seg.strip():
                parts.append(self._render_plain(seg))
            if self.math_mode == "latex":
                # pass the env name through so the \begin{env} shell
                # is rebuilt (a bare & inside $$ breaks MathJax)
                rendered = self._render_math_latex(
                    inner, True, env_kind
                )
            else:
                rendered = transcribe_math(
                    inner, True, self.ev, 0, self.macros
                )
            if rendered:
                self.ev["display_math_blocks"] += 1
                math_lines.append(rendered)
            pos = m.end()
        tail = piece[pos:]
        if tail.strip():
            parts.append(self._render_plain(tail))
        rendered = " ".join(p for p in parts if p).strip()
        return rendered, math_lines

    def _render_list(self, body, kind):
        # split into \item [label] pieces
        pieces = re.split(r"\\item\b\s*", body)
        head = pieces[0].strip() if pieces else ""
        if head:
            self._emit_block(self._render_plain(head))
        idx = 0
        lines = []
        for piece in pieces[1:]:
            idx += 1
            label_m = re.match(r"\[([^\]]*)\]\s*", piece)
            label = None
            if label_m:
                label = label_m.group(1).strip()
                piece = piece[label_m.end():]
            rendered, math_lines = self._render_item(piece)
            if not rendered and not math_lines:
                continue
            if kind == "enumerate":
                prefix = "%d. " % idx
            elif label:
                prefix = label + ": "
            else:
                prefix = "- "
            if rendered:
                lines.append(prefix + rendered)
            # math inside an item continues on indented lines
            lines.extend("  " + ml for ml in math_lines)
        if lines:
            self._emit_block("\n".join(lines))

    def _render_float(self, body, kind):
        """Emit every caption and embedded tabular in document order.

        A float may hold several tabulars/captions; taking only the
        first of each silently drops the rest. Tabular bodies also
        need the column-spec group stripped, not glued into the cells.
        """
        prefix = "图" if "figure" in kind or "wrapfig" in kind else "表"
        events = []
        for m in _CAPTION_RE.finditer(body):
            if _tex_in_comment(body, m.start()):
                continue
            content, _ = _read_braced(body, m.end() - 1)
            if content is not None:
                cap = prefix + ": " + self._render_plain(content).strip()
                if cap.strip():
                    events.append((m.start(), cap))
        for m in _TAB_BEGIN_RE.finditer(body):
            env = m.group(1)
            pos = m.end()
            t_end = None
            end_re = re.compile(
                r"\\end\s*\{" + re.escape(env) + r"\}"
            )
            for em in end_re.finditer(body, pos):
                # finditer(body, pos) match positions are already absolute
                if not _tex_in_comment(body, em.start()):
                    t_end = em.start()
                    break
            t_body = body[pos:t_end] if t_end is not None else body[pos:]
            col_groups = 2 if env in ("tabular*", "tabularx") else 1
            tab = "\n".join(self._tabular_lines(t_body, col_groups))
            if tab:
                events.append((m.start(), tab))
        self.ev["graphics"] += len(_INCLUDEGRAPHICS_RE.findall(body))
        parts = [p for _, p in sorted(events, key=lambda e: e[0])]
        if parts:
            self._emit_block("\n".join(parts))

    @staticmethod
    def _split_tabular_rows(s):
        r"""Split rows at \\ only at brace depth 0.

        A naive re.split cuts \\ inside multi-line cell macros
        (\tabincell{c}{LOC \\ method}), truncating the call and
        leaving the second argument group unclosed. With depth
        awareness the in-argument \\ stays with macro expansion;
        \{ \} literal escapes don't count depth; \\ at depth>0
        keeps its place.
        """
        rows = []
        buf = []
        i, n = 0, len(s)
        depth = 0
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                nxt = s[i + 1]
                if nxt == "\\":
                    # \\[dimen] optional spacing: at depth 0 consume
                    # it and split the row; at depth>0 (math arrays)
                    # strip the argument but keep \\ for
                    # transcribe_math as the row separator
                    j = i + 2
                    k = j
                    while k < n and s[k] in " \t":
                        k += 1
                    if k < n and s[k] == "[":
                        e = s.find("]", k)
                        if e != -1:
                            j = e + 1
                    if depth == 0:
                        rows.append("".join(buf))
                        buf = []
                    else:
                        buf.append(s[i : i + 2])
                    i = j
                    continue
                buf.append(s[i : i + 2])
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)
            buf.append(c)
            i += 1
        rows.append("".join(buf))
        return rows

    def _render_tabular(self, body):
        # column-spec stripping is centralized in _tabular_lines
        lines = self._tabular_lines(body, 1)
        if lines:
            self._emit_block("\n".join(lines))

    def _tabular_lines(self, body, col_groups=0):
        r"""Render a tabular body as "a | b | c" rows.

        Leading column-spec groups are stripped (tabular*/tabularx
        carry two, {width}{cols}); cells are macro-expanded first,
        then multicolumn/multirow headers stripped for the content
        argument (order matters: expansion can itself produce
        \multirow); cite → [key].
        """
        s = body.lstrip()
        for _ in range(col_groups):
            content, endpos = _read_braced(s, 0)
            if content is None:
                break
            s = s[endpos:].lstrip()
        lines = []
        # depth-aware row split (see _split_tabular_rows)
        for row in self._split_tabular_rows(s):
            row = re.sub(r"\\(?:hline|cline\s*\{[^{}]*\}|toprule|"
                         r"midrule|bottomrule|noalign\s*\{[^{}]*\})"
                         r"(?![a-zA-Z])", "", row)
            row = row.strip()
            if not row:
                continue
            cells = [transcribe_math(
                _tabular_cell_text(
                    _expand_macros_in(c, self.macros, self.ev)
                ),
                False, self.ev, 0, self.macros,
            ).strip() for c in row.split("&")]
            # a literal # landing at line start (empty leading cell
            # dropped, "\# layers" promoted) would be read downstream
            # as an ATX heading; escape it. Real headings travel via
            # HeadingCandidate metadata, so this can't misfire.
            line = " | ".join(c for c in cells if c)
            if line.startswith("#"):
                line = "\\" + line
            lines.append(line)
        return lines

    def _render_bibliography(self, body):
        """thebibliography → a numbered reference list.

        Numbers are compile-time derived; the key is kept as a
        （key: …） trailing note for traceability.
        """
        # leading width argument [99]
        body = re.sub(r"^\s*\[\d+\]", "", body)
        self._emit_block("[参考文献]")
        parts = re.split(
            r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}", body
        )
        idx = 0
        for k in range(1, len(parts) - 1, 2):
            key = parts[k].strip()
            rendered = self._render_plain(parts[k + 1])
            if not rendered:
                continue
            idx += 1
            if key:
                line = "[%d] %s（key: %s）" % (idx, rendered, key)
            else:
                line = "[%d] %s" % (idx, rendered)
            self._emit_block(line)
        self.ev["bibitem_entries"] = idx

    def _render_theorem(self, body, base):
        # optional title argument: \begin{theorem}[Extra]
        # \ref/\eqref/cite inside it resolve to numbers, not raw source
        title = None
        opt = re.match(r"\s*\[([^\]]*)\]", body)
        if opt:
            # expand macros in the title before stripping text shells
            title = (
                _clean_inline_title(opt.group(1), self.macros, self.ev)
                or None
            )
            body = body[opt.end():]
        label = {
            "abstract": "摘要", "proof": "证明",
            "acknowledgements": "致谢", "acknowledgments": "致谢",
        }.get(base, base)
        head = "[" + label + ((": " + title) if title else "") + "]"
        self._emit_block(head)
        self._scan(body, 1)

    # ---------- generic command dispatch ----------

    def _dispatch_cmd(self, src, i, m, depth):
        cmd = m.group(1)
        j = m.end()

        # normalize star variants (\vspace*{..} → \vspace{..}; the
        # star sits at j) so the following arg reads hit the brace
        if cmd in _STAR_VARIANT_CMDS and j < len(src) and src[j] == "*":
            j += 1
            self.ev["star_variants_normalized"] = (
                self.ev.get("star_variants_normalized", 0) + 1
            )

        # title commands found in the body are collected and emitted
        # by \maketitle; the optional [short] arg is consumed too
        if cmd in ("title", "author", "date"):
            pos = self._skip_opt(src, j)
            content, c_end = _read_braced(src, pos)
            if content is not None:
                if cmd == "author":
                    self._pending_title.setdefault(
                        "author", []
                    ).append(content)
                elif cmd not in self._pending_title:
                    self._pending_title[cmd] = content
                return c_end
            return pos

        # \newcolumntype{..}[..]{..} is a pure typesetting declaration
        # — consume it whole to keep shell-stripped debris out
        if cmd == "newcolumntype":
            a, a_end = _read_braced(src, j)
            if a is None:
                return j
            pos = self._skip_opt(src, a_end)
            b, b_end = _read_braced(src, pos)
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return b_end if b is not None else pos

        # \fontsize{..}{..}\selectfont is pure typesetting — consume
        # both args plus the trailing \selectfont
        if cmd == "fontsize":
            a, a_end = _read_braced(src, j)
            if a is None:
                return j
            b, b_end = _read_braced(src, a_end)
            end = b_end if b is not None else a_end
            m2 = re.match(r"\s*\\selectfont(?![a-zA-Z])", src[end:])
            if m2:
                end += m2.end()
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return end

        if cmd in ("bibliography",
                   "bibliographystyle", "input", "include",
                   "usepackage", "documentclass",
                   "lstset", "graphicspath",
                   "newenvironment", "renewenvironment"):
            content, c_end = _read_braced(src, j)
            if content is not None:
                return c_end
            return j

        # plain-TeX box/align primitives (\hbox{...}/\halign{...}):
        # scan the content, drop the shell (same as _TEXT_FONTS)
        if cmd in _LAYOUT_BOX_CMDS:
            content, c_end = _read_braced(src, j)
            if content is None:
                return j
            if depth + 1 <= self.MAX_DEPTH:
                self._scan(content, depth + 1)
            else:
                self._emit(self._render_plain(content))
            return c_end

        # typesetting primitives (newcount/setcounter/thispagestyle/vfil/…)
        # produce no narrative text; consume their args whole
        if cmd in _TYPO_TWO_ARG_CMDS:
            a, a_end = _read_braced(src, j)
            b, b_end = (None, a_end)
            if a is not None:
                b, b_end = _read_braced(src, a_end)
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return b_end if b is not None else (a_end if a is not None else j)
        if cmd in _TYPO_ONE_ARG_CMDS:
            pos = self._skip_opt(src, j)
            content, c_end = _read_braced(src, pos)
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return c_end if content is not None else pos
        if cmd in _TYPO_NAME_CMDS:
            nm = re.match(r"\\[a-zA-Z]+", src[j:])
            if nm:
                j += nm.end()
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return j
        if cmd in _TYPO_ZERO_CMDS:
            if cmd in ("displaybreak", "pagebreak", "linebreak"):
                j = self._skip_opt(src, j)
            # direct assignments like \tolerance=100 are swallowed too
            rest = re.match(r"\s*=\s*[-\d.]+\w*", src[j:])
            if rest:
                j += rest.end()
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return j

        # macro-definition commands are consumed whole, second brace
        # group included
        if cmd in ("newcommand", "renewcommand", "providecommand",
                   "DeclareMathOperator", "def"):
            return self._consume_macro_def(src, j)

        if cmd in _DROP_CMDS:
            if cmd == "maketitle":
                return j
            return j

        if cmd in ("term", "noemphterm", "termnoindex"):
            # \term[short]{text} family: drop the optional index arg,
            # recurse into the main argument (same shell-drop as
            # _TEXT_FONTS)
            k = self._skip_opt(src, j)
            content, c_end = _read_braced(src, k)
            if content is None:
                return j
            if depth + 1 <= self.MAX_DEPTH:
                self._scan(content, depth + 1)
            else:
                self._emit(self._render_plain(content))
            self.ev["term_index_dropped"] = (
                self.ev.get("term_index_dropped", 0) + 1
            )
            return c_end

        if cmd in _TEXT_FONTS:
            content, c_end = _read_braced(src, j)
            if content is None:
                return j
            if depth + 1 <= self.MAX_DEPTH:
                self._scan(content, depth + 1)
            else:
                self._emit(self._render_plain(content))
            return c_end

        if cmd in ("cite", "citep", "citet", "citealt", "citealp",
                   "citeauthor", "citeyear", "citeyearpar", "citep",
                   "parencite", "textcite", "autocite", "onlinecite"):
            content, c_end = _read_braced(src, j)
            if content is None:
                return j
            keys = ", ".join(
                k.strip() for k in content.split(",") if k.strip()
            )
            self._emit("[" + keys + "]")
            return c_end

        if cmd in ("ref", "autoref", "pageref", "Cref", "cref",
                   "vref", "eqref", "label", "namecref", "index",
                   "nocite"):
            content, c_end = _read_braced(src, j)
            if content is None:
                return j
            # normalize text accents inside label keys before emitting
            content = re.sub(
                r"\\(['\"`^~=.])\s*\{?([a-zA-Z])\}?",
                lambda mm: mm.group(2) + _TEXT_ACCENTS[mm.group(1)],
                content)
            if cmd == "eqref":
                self._emit("([" + content.strip() + "])")
            elif cmd == "label":
                pass
            elif cmd in ("index", "nocite"):
                # index/nocite are invisible in compiled output —
                # dropped with disclosure
                self.ev["index_nocite_dropped"] = (
                    self.ev.get("index_nocite_dropped", 0) + 1
                )
            else:
                self._emit("[" + content.strip() + "]")
            return c_end

        if cmd == "includegraphics":
            # text-mode \includegraphics: dropped, counted in the
            # graphics disclosure (same as the math/float paths)
            _ig_pos = self._skip_opt(src, j)
            _ig_arg, _ig_end = _read_braced(src, _ig_pos)
            self.ev["graphics"] = self.ev.get("graphics", 0) + 1
            return _ig_end if _ig_arg is not None else _ig_pos
        if cmd == "raisebox":
            # \raisebox{dim}{content}: render the second argument,
            # drop dim
            _rb_dim, _rb_d_end = _read_braced(src, j)
            if _rb_dim is None:
                return j
            _rb_body, _rb_b_end = _read_braced(src, _rb_d_end)
            if _rb_body is not None:
                if depth + 1 <= self.MAX_DEPTH:
                    self._scan(_rb_body, depth + 1)
                else:
                    self._emit(self._render_plain(_rb_body))
                return _rb_b_end
            return _rb_d_end
        if cmd == "rule":
            # \rule[raise]{w}{h} is a zero-output typesetting primitive
            # (the tabular path strips it in _tabular_cell_text)
            _ru_pos = self._skip_opt(src, j)
            for _ in range(2):
                _ru_a, _ru_e = _read_braced(src, _ru_pos)
                if _ru_a is None:
                    break
                _ru_pos = _ru_e
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return _ru_pos
        if cmd in ("url", "path", "footnote", "thanks", "hspace",
                   "vspace", "texorpdfstring", "textcolor", "href",
                   "color", "definecolor", "cline", "multicolumn",
                   "multirow"):
            return self._consume_arg_cmds(src, i, j, cmd)

        if cmd == "par":
            if not self._at_paragraph_break():
                self._emit("\n\n")
            return j

        # declarative font switches: shell only (content stays in the
        # scan stream)
        if cmd in _FONT_SWITCHES:
            self.ev["declarative_font_switches"] = (
                self.ev.get("declarative_font_switches", 0) + 1
            )
            return j

        # symbol-table commands emit their text form directly
        # (\S § / \qed ∎ / \log log), but a user \renewcommand of
        # the same name wins — the macro branch below takes it first
        if cmd in _SYMBOLS and cmd not in self.macros:
            self._emit(_SYMBOLS[cmd])
            return j

        # ams/elsarticle frontmatter commands appearing in the body →
        # title block (\ead feeds the author-email metadata)
        if cmd in ("address", "email", "keywords", "subjclass",
                   "dedicating", "dedication", "ead"):
            pos = self._skip_opt(src, j)
            content, c_end = _read_braced(src, pos)
            if content is not None:
                # \ead reuses the email slot the title renderer knows
                slot = "email" if cmd == "ead" else cmd
                self._pending_title.setdefault(slot, []).append(content)
                return c_end
            return pos

        if cmd == "cortext":
            # \cortext[..]{..} is a corresponding-author footnote
            # marker with no output (the email already sits in the
            # title block) — dropped with disclosure
            _ct_pos = self._skip_opt(src, j)
            _ct_a, _ct_e = _read_braced(src, _ct_pos)
            self.ev["cortext_dropped"] = (
                self.ev.get("cortext_dropped", 0) + 1
            )
            return _ct_e if _ct_a is not None else _ct_pos

        # AGU title-metadata command family
        if cmd == "authors":
            content, c_end = _read_braced(src, j)
            if content is not None:
                # strip embedded \affil{n}/\thanks{...} markers
                content = re.sub(
                    r"\s*\\(?:affil|thanks)\s*\{[^{}]*\}", "", content)
                self._pending_title.setdefault(
                    "author", []).append(content)
                return c_end
            return j
        if cmd == "affiliation":
            # {number}{address} → address slot (number dropped)
            _af_num, _af_n_end = _read_braced(src, j)
            if _af_num is not None:
                _af_txt, _af_t_end = _read_braced(src, _af_n_end)
                if _af_txt is not None:
                    self._pending_title.setdefault(
                        "address", []).append(_af_txt)
                    return _af_t_end
                return _af_n_end
            return j
        if cmd == "correspondingauthor":
            # {name}{email} → email slot (kept as "name (email)")
            _ca_name, _ca_n_end = _read_braced(src, j)
            if _ca_name is not None:
                _ca_mail, _ca_m_end = _read_braced(src, _ca_n_end)
                if _ca_mail is not None:
                    self._pending_title.setdefault(
                        "email", []).append(
                            "%s (%s)" % (_ca_name, _ca_mail))
                    return _ca_m_end
                return _ca_n_end
            return j
        if cmd == "affil":
            # affiliation superscript index — zero output, dropped
            # with disclosure (the embedded form is stripped in the
            # authors branch; this is the body-wide fallback)
            _af_a, _af_e = _read_braced(src, j)
            self.ev["affil_dropped"] = (
                self.ev.get("affil_dropped", 0) + 1
            )
            return _af_e if _af_a is not None else j
        if cmd == "acknowledgments":
            # AGU unnumbered section command — emit its conventional
            # name as a heading (the env form is in _THEOREM_ENVS)
            self._emit_heading(
                "Acknowledgments", _SECTION_LEVELS["section"])
            return j

        # \bibitem outside thebibliography: consume the numbered form,
        # leak no braces
        if cmd == "bibitem":
            pos = j
            if pos < len(src) and src[pos] == "[":
                k = src.find("]", pos)
                pos = (k + 1) if k != -1 else pos
            content, c_end = _read_braced(src, pos)
            if content is not None:
                self.ev["bibitem_fallback"] = (
                    self.ev.get("bibitem_fallback", 0) + 1
                )
                return c_end
            return pos

        # deterministic macro expansion (step/depth/length guards)
        if cmd in self.macros:
            nargs, body = self.macros[cmd]
            args, a_end = self._read_macro_args(src, j, nargs)
            if (
                args is not None
                and depth + 1 <= self.MAX_DEPTH
                and self._macro_steps < _MACRO_TEXT_STEP_CAP
            ):
                expanded = body
                for k, a in enumerate(args, 1):
                    expanded = expanded.replace("#" + str(k), a)
                if len(expanded) <= _MACRO_TEXT_BODY_MAX:
                    # zero-arg macro whose body is a bare sectioning
                    # command: scanned alone, the expansion can't see
                    # the argument group after the call site, so the
                    # command would be dropped for lack of a
                    # parameter. Read the argument group here and emit
                    # the heading; without one, fall back to the
                    # normal expansion scan.
                    if not args:
                        _sec_m = re.fullmatch(
                            r"\\(part|chapter|section|subsection"
                            r"|subsubsection|paragraph|subparagraph)"
                            r"\*?\s*",
                            expanded,
                        )
                        if _sec_m:
                            pos = self._skip_opt(src, a_end)
                            # star variant: consume the star too
                            if pos < len(src) and src[pos] == "*":
                                pos += 1
                            title, t_end = _read_braced(src, pos)
                            if title is not None:
                                rendered = self._render_plain(title)
                                if rendered:
                                    self._macro_steps += 1
                                    self.ev["macros_expanded"] = (
                                        self.ev.get(
                                            "macros_expanded", 0
                                        ) + 1
                                    )
                                    self._emit_heading(
                                        rendered,
                                        _SECTION_LEVELS[
                                            _sec_m.group(1)
                                        ],
                                    )
                                return t_end
                            if pos > a_end:
                                # star seen but no title argument:
                                # consume the star anyway
                                a_end = pos
                    self._macro_steps += 1
                    self.ev["macros_expanded"] = (
                        self.ev.get("macros_expanded", 0) + 1
                    )
                    self._scan(expanded, depth + 1)
                    return a_end

        # text-mode cedilla (\c{c} → ç)
        if cmd == "c":
            content, c_end = _read_braced(src, j)
            if content is not None and content[:1].isalpha():
                self._emit(content[:1] + "\u0327" + content[1:])
                return c_end
            if j < len(src) and src[j].isalpha():
                self._emit(src[j] + "\u0327")
                return j + 1

        # text-mode breve accent (\u{C} → C+̆)
        if cmd == "u":
            content, c_end = _read_braced(src, j)
            if content is not None and content[:1].isalpha():
                self._emit(content[:1] + "\u0306" + content[1:])
                return c_end
            if j < len(src) and src[j].isalpha():
                self._emit(src[j] + "\u0306")
                return j + 1

        # unknown command: keep verbatim + disclose (never invent a
        # value); any immediately following [..]/{..} argument groups
        # are kept verbatim too
        self._count_unknown(cmd)
        out = src[i:j]
        pos = j
        m_opt = re.match(r"\[[^\]\n]*\]", src[pos:])
        if m_opt:
            out += m_opt.group(0)
            pos += m_opt.end()
        grp, g_end = _read_braced(src, pos)
        if grp is not None:
            self._emit(out + src[pos:g_end])
            return g_end
        self._emit(out)
        return pos if m_opt else j

    def _read_macro_args(self, src, j, nargs):
        """Read n macro arguments (braced group first, single token
        as fallback)."""
        args = []
        pos = j
        n = len(src)
        for _ in range(nargs):
            while pos < n and src[pos] in " \t":
                pos += 1
            a, a_end = _read_braced(src, pos)
            if a is None and pos < n and src[pos].isalnum():
                a, a_end = src[pos], pos + 1
            if a is None:
                return None, j
            args.append(a)
            pos = a_end
        return args, pos

    def _consume_macro_def(self, src, j):
        """Consume macro-definition source (optional * / {name} /
        #params / [n] / [default] / {body}).

        The definition itself was collected by _collect_macro_defs;
        this only keeps the definition source out of the narrative.
        """
        n = len(src)
        i = j
        if i < n and src[i] == "*":
            i += 1
        if i < n and src[i] == "{":
            _name, i = _read_braced(src, i)
        elif i < n and src[i] == "\\":
            nm = re.match(r"\\[a-zA-Z]+", src[i:])
            if nm:
                i += nm.end()
        pm = re.match(r"(?:#\d\s*)+", src[i:]) if i < n else None
        if pm:
            i += pm.end()
        while i < n and src[i] in " \t":
            i += 1
        while i < n and src[i] == "[":
            k = src.find("]", i)
            if k == -1:
                break
            i = k + 1
            while i < n and src[i] in " \t":
                i += 1
        body, b_end = _read_braced(src, i)
        if body is not None:
            return b_end
        return i

    def _consume_arg_cmds(self, src, i, j, cmd):
        """Consume miscellaneous commands taking 1-2 {...} arguments."""
        first, f_end = _read_braced(src, j)
        if first is None:
            self._count_unknown(cmd)
            self._emit(src[i:j])
            return j
        if cmd in ("url", "path"):
            self._emit(first)
            return f_end
        if cmd == "footnote":
            self._emit("（" + self._render_plain(first).strip() + "）")
            return f_end
        if cmd == "thanks":
            return f_end
        if cmd in ("hspace", "vspace"):
            self._emit(" ")
            return f_end
        if cmd == "texorpdfstring":
            # {tex}{pdf} → first argument
            self._scan(first, 9)
            second, s_end = _read_braced(src, f_end)
            return s_end if second is not None else f_end
        if cmd in ("textcolor", "color"):
            # {color}{text} → second argument
            second, s_end = _read_braced(src, f_end)
            if second is not None:
                self._scan(second, 9)
                return s_end
            return f_end
        if cmd == "href":
            # {url}{text} → second argument
            second, s_end = _read_braced(src, f_end)
            if second is not None:
                self._scan(second, 9)
                return s_end
            return f_end
        if cmd in ("definecolor",):
            return f_end
        if cmd in ("cline",):
            return f_end
        if cmd in ("multicolumn", "multirow"):
            # {n}{fmt}{text} → third argument
            second, s_end = _read_braced(src, f_end)
            if second is not None:
                third, t_end = _read_braced(src, s_end)
                if third is not None:
                    self._scan(third, 9)
                    return t_end
            return f_end
        self._count_unknown(cmd)
        self._emit(src[i:f_end])
        return f_end

    # ---------- math / heading / plain-text rendering ----------

    def _render_math_latex(self, math_src, display, kind=None):
        """latex mode: emit the cleaned math source wrapped in $/$$.

        kind: the display source ("bracket"/"dollar" or an env name);
        align/matrix envs are re-wrapped in their \begin{env} shell
        (bare & outside it is invalid LaTeX). Header argument groups
        (\begin{alignat}{2}) are kept with the body.
        """
        cleaned = _clean_math_latex(math_src, self.ev, self.macros)
        if not cleaned:
            return None
        # \xymatrix: try a deterministic rewrite into an array first
        # (MathJax-renderable); unsupported shapes return None and
        # fall through to the verbatim branch below (no content loss)
        if "xymatrix" in cleaned:
            _xy = _rewrite_xymatrix(cleaned)
            if _xy is not None:
                cleaned = _xy
        # beyond MathJax's reach → verbatim code block (no content
        # loss): \xymatrix, tikzpicture, and a grouped & (legal TeX
        # but Misplaced & in aligned). Shell-env sources get their
        # \begin{env}/\end{env} wrapper back — cleaned holds only
        # the body.
        if (
            "xymatrix" in cleaned
            or "tikzpicture" in cleaned
            or (kind and ("tikzpicture" in kind or "xymatrix" in kind))
            or _has_grouped_amp(cleaned)
        ):
            if (
                kind
                and kind not in ("bracket", "dollar")
                and kind.rstrip("*") in _LATEX_SHELL_ENVS
            ):
                cleaned = (
                    "\\begin{" + kind + "}" + cleaned
                    + "\\end{" + kind + "}"
                )
            return "```\n" + cleaned + "\n```"
        if not display:
            return "$" + cleaned + "$"
        if (
            kind
            and kind not in ("bracket", "dollar")
            and kind.rstrip("*") in _LATEX_SHELL_ENVS
        ):
            return (
                "$$\n\\begin{" + kind + "}" + cleaned
                + "\\end{" + kind + "}\n$$"
            )
        return "$$\n" + cleaned + "\n$$"

    def _emit_display(self, math_src, kind):
        if self.math_mode == "latex":
            rendered = self._render_math_latex(math_src, True, kind)
        else:
            rendered = transcribe_math(
                math_src, True, self.ev, 0, self.macros
            )
        if not rendered:
            return
        self.ev["display_math_blocks"] += 1
        span = self._emit_block(rendered)
        if span:
            self.display_math_blocks.append((span[0], span[1], rendered))

    def _emit_inline(self, math_src):
        if self.math_mode == "latex":
            rendered = self._render_math_latex(math_src, False)
        else:
            rendered = transcribe_math(
                math_src, False, self.ev, 0, self.macros
            )
        if rendered:
            self.ev["inline_math_count"] += 1
            self._emit(rendered)

    def _emit_heading(self, title, level):
        span = self._emit_block(title)
        if span is None:
            return
        start, end = span
        self.candidates.append(
            HeadingCandidate(
                raw=title,
                pattern="markdown",
                depth_hint=level,
                start=start,
                end=end,
                provenance="declared",
            )
        )

    def _render_plain(self, s):
        """Lightweight render for short text (headings, list
        items): inline math + command cleanup."""
        # strip TeX line comments (except escaped \%)
        s = re.sub(r"(?<!\\)%[^\n]*", "", s)
        # zero-output typographic residue (this path skips
        # _scan's handling): soft hyphens, forced breaks, control
        # spaces
        s = s.replace("\\-", "")
        s = re.sub(r"\\\\(?:\[[^\]\n]*\])?", " ", s)
        s = re.sub(r"\\ ", " ", s)
        # line-end backslash (TeX newline → space; the r"\\ "
        # rule above doesn't span newlines)
        s = re.sub(r"\\[ \t]*\r?\n", " ", s)
        # drop single-argument declarative color:
        # {\color{blue}URL} → URL
        s = re.sub(r"\\color\s*\{[^{}]*\}", " ", s)
        stash = []  # stash for latex-rendered inline formulas
        def repl_inline(m):
            if self.math_mode == "latex":
                r = self._render_math_latex(m.group(1), False) or ""
                # stash the rendered output behind a placeholder
                # the cleanup regexes below can't touch, so the
                # $...$ formula isn't rewritten a second time;
                # unicode mode keeps the original path
                stash.append(r)
                return "\x00%d\x00" % (len(stash) - 1)
            return transcribe_math(
                m.group(1), False, self.ev, 0, self.macros
            )

        s = re.sub(r"\$([^$]*)\$", repl_inline, s)
        # text-mode accents (na\"ive → combining)
        s = re.sub(
            r'\\(["\'`^~=.])\s*\{?([a-zA-Z])\}?',
            lambda mm: mm.group(2) + _TEXT_ACCENTS[mm.group(1)],
            s,
        )
        s = re.sub(
            r"\\c\s*\{([a-zA-Z])\}",
            lambda mm: mm.group(1) + "\u0327",
            s,
        )
        # \href{URL}{text} → [text](URL); the text group may
        # nest one brace level, font declarations in it are
        # pre-stripped
        _href_text_clean = re.compile(
            r"\\(?:ttfamily|rmfamily|sffamily|bfseries|itshape|scshape)\s*"
        )
        s = re.sub(
            r"\\href\s*\{([^{}]*)\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
            lambda mm: "[%s](%s)" % (
                _href_text_clean.sub("", mm.group(2)), mm.group(1)),
            s,
        )
        s = re.sub(r"\\(?:textbf|textit|texttt|emph|textrm|textsf|"
                   r"textsc|textnormal|mbox)\{([^{}]*)\}", r"\1", s)
        s = re.sub(
            r"\\(?:cite|citep|citet)\{([^{}]*)\}", r"[\1]", s
        )
        s = re.sub(r"\\(?:ref|autoref|Cref)\{([^{}]*)\}", r"[\1]", s)
        s = re.sub(r"\\(?:label|thanks)\{[^{}]*\}", "", s)
        # symbol-table fallback (\alpha etc. bare in headings)
        s = _SYMBOL_RE.sub(lambda m: _SYMBOLS[m.group(1)], s)
        # strip spacing/font-size shells together with their
        # arguments (counted in typography_primitives_dropped)
        def _drop_fontsize(mm):
            self.ev["typography_primitives_dropped"] = (
                self.ev.get("typography_primitives_dropped", 0) + 1
            )
            return " "
        s = re.sub(
            r"\\fontsize\s*\{[^{}]*\}\s*\{[^{}]*\}\s*"
            r"(?:\\selectfont(?![a-zA-Z])\s*)?", _drop_fontsize, s)
        s = re.sub(
            r"\\(?:vspace|hspace|hskip|vskip|kern|mskip)\s*\*?\s*"
            r"\{[^{}]*\}", " ", s)
        # unescape literal characters (\% \{ \} \_ \& \# \$)
        # before command-name stripping so the backslash
        # doesn't linger
        s = re.sub(r"\\([%{}_&#$])", r"\1", s)
        s = re.sub(r"\\@", "", s)
        # TeX quote ligatures (same branch as in _scan)
        s = s.replace("``", "\u201c").replace("''", "\u201d")
        s = s.replace("~", " ")
        s = re.sub(r"\\[a-zA-Z]+", "", s)
        s = s.replace("{", "").replace("}", "")
        if stash:  # restore latex formulas after the cleanup
            s = re.sub(
                r"\x00(\d+)\x00",
                lambda m: stash[int(m.group(1))], s,
            )
        s = re.sub(r"\s+", " ", s)
        return s.strip()
