import re

from provendoc.base.exporter import BaseExporter
from provendoc.base.models.representation_unit import RepresentationUnit
from provendoc.core.exporters.atomic_write import atomic_write_text

# GFM task-list markers ("- [x]" / "- [ ]"): when they appear as
# literal content inside body text, the front-end markdown renderer
# parses them into checkboxes (✓/□) and hijacks the semantics.
# Escape the brackets at export time so they stay literal.
_TASK_LIST_MARKER_RE = re.compile(
    r"^(\s*(?:[-*+]|\d+[.)]) \[)([ xX])(\])",
    re.MULTILINE,
)

# code-fence units (body starts with a fence): hierarchical
# placement is already carried in the raw layer (heading_path
# metadata), the markdown presentation layer does not re-render
# heading prefixes. But if the code block is the first unit of a
# section (heading not rendered yet), the heading must still be
# rendered once — otherwise, in sections whose entire body is
# code/table blocks, the heading would vanish along with the unit.
_CODE_FENCE_PREFIX_RE = re.compile(r"^\s*(?:```|~~~)")

# heading source text may already carry leading hashes (e.g.
# the Chinese "## 补充说明"); strip them before rendering to avoid
# double prefixes like "## ## 补充说明"
_LEADING_HASHES_RE = re.compile(r"^#{1,6}\s*")

# code-domain units (structural_ast chunking): the body is raw
# source code, not markdown; the presentation layer embeds a
# fenced code block (with language tag) + symbol headings, folding
# the tree-sitter structural parse into markdown. The
# chunking=structural_ast marker is only produced by the code-domain
# chunker, so narrative fenced-code units never land here.
_CODE_DOMAIN_MARK = "structural_ast"
# backtick fence runs at the start of a source line (markdown
# examples embedded in docstrings): CommonMark — a fence indented
# 0-3 spaces can close the outer fence; 4+ spaces is literal
# content. The outer fence must be longer than the longest inner
# closable fence run.
_FENCE_RUN_RE = re.compile(r"^ {0,3}(`{3,})", re.MULTILINE)

# outline-only units (heading with no body): the structural fact
# is already carried by the heading line, the body is not
# re-rendered — otherwise markdown would show "#### 1.1.1.
# UTF-8 检测" followed by another "1.1.1. UTF-8 检测" line. The
# raw/chunk layers keep text so the tail hierarchy stays visible.

# Suspected separator-less list items ("1配置文件路径", a common
# AI formatting defect): digits
# immediately followed by content with no space or dot. The
# presentation layer rewrites them into
# proper ordered lists ("1. 配置文件路径"); raw and unit.text stay
# untouched. Only rewritten when ≥2 consecutive lines share the
# shape (to rule out isolated false matches); digits limited to
# 1-3 places (to rule out years / large numbers). The spaced form
# ("1 标题标记后缺少空格": digit followed by a space but no dot,
# common in AI descriptive lists) is rewritten the same way.
_BARE_NUMERIC_ITEM_RE = re.compile(r"^(\d{1,3})([^\d\s.、,，)）].*)$")
_BARE_NUMERIC_SPACED_RE = re.compile(r"^(\d{1,3})[ \t]+(\S.*)$")


def _match_bare_numeric(line: str):
    """Match a bare-digit list item; return (raw m, ordinal) or
    None."""
    m = _BARE_NUMERIC_ITEM_RE.match(line)
    if m:
        return m.group(1), m.group(2)
    m = _BARE_NUMERIC_SPACED_RE.match(line)
    if m:
        return m.group(1), m.group(2)
    return None


def _normalize_bare_numeric_lists(text: str) -> str:
    """Rewrite consecutive suspected separator-less numeric list
    lines into proper ordered lists."""
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if _match_bare_numeric(stripped):
            j = i
            while j < n and _match_bare_numeric(lines[j].strip()):
                j += 1
            if j - i >= 2:
                for k in range(i, j):
                    num, content = _match_bare_numeric(
                        lines[k].strip()
                    )
                    out.append(f"{num}. {content}")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# Suspected separator-less marker items ("-编码置信度", a marker
# followed immediately by content with no space): also rewritten
# only when ≥2 consecutive lines match; excludes thematic breaks
# (---/***/___ and their prefixes), content starting with a digit
# (negative numbers) and with # (headings).
_BARE_MARKER_ITEM_RE = re.compile(
    r"^([-*+])([^\s\-*+#\d].*)$"
)


def _normalize_bare_marker_lists(text: str) -> str:
    """Rewrite consecutive suspected separator-less marker list
    lines into proper unordered lists."""
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if _BARE_MARKER_ITEM_RE.match(stripped):
            j = i
            while j < n and _BARE_MARKER_ITEM_RE.match(
                lines[j].strip()
            ):
                j += 1
            if j - i >= 2:
                for k in range(i, j):
                    m = _BARE_MARKER_ITEM_RE.match(
                        lines[k].strip()
                    )
                    if m is not None:
                        out.append(f"{m.group(1)} {m.group(2)}")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# ---- Hard line breaks and fence closing (presentation-layer
# typography) ----
# Logical lines in the source text are separated by a single \n;
# markdown renderers fold single in-paragraph newlines into one
# line (soft breaks). Only "list-like" lines get a trailing
# double-space (GFM hard break) to keep each line displayed
# separately; prose sentences are left alone so paragraphs fold
# naturally. raw and unit.text stay untouched.
# "List-like" detection:
#   - Chinese ordinal lines ("第一层…" / "阶段一…" / "第3章…"):
#     marker-less lists in line typography (not rewritten to
#     "1." — the original prefix is kept to avoid double
#     numbering);
#   - the next line is a Chinese ordinal line: the current line is
#     a list lead-in ("分为以下阶段:", for instance), gets a hard break so it is
#     not folded into the first item;
#   - short lines (≤40 chars) not ending with sentence/continuation
#     punctuation: fragmented list lines.
# Skipped: inside fences (code verbatim), table rows (would break
# table syntax), list/quote/heading marker lines (markdown itself
# preserves line boundaries there).
_CHINESE_ORDINAL_RE = re.compile(
    r"(?:第[一二三四五六七八九十百零两\d]{1,4}"
    r"[章节层步骤部分项条篇款项]"
    r"|阶段[一二三四五六七八九十百零\d]{1,4})"
)
# sentence-tail / continuation punctuation: lines ending with these
# are prose (or lead-in) lines — no hard break
_SENTENCE_TAIL_CHARS = "。！？.!?…；;，,、：:"
_LIST_OR_QUOTE_RE = re.compile(
    r"^\s*(?:[-*+]\s|\d{1,3}[.)]\s|>|#{1,6}\s)"
)
# bilingual parallel typesetting: adjacent lines with different
# dominant scripts (latin vs CJK) → parallel lines, hard breaks
# keep the English sentence and the Chinese translation each on
# its own line
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# all-caps snake-case marker lines (chunk separators like
# SPLIT_HERE): isolate into their own paragraphs with blank lines
# before and after, so they do not fold into neighbors (at least
# one underscore — ordinary caps words like TODO/API are excluded)
_MARKER_LINE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def _dominant_script(line: str):
    """Dominant script of the line: 'cjk' / 'latin' / None
    (balanced or no letters)."""
    cjk = len(_CJK_RE.findall(line))
    latin = len(_LATIN_RE.findall(line))
    if cjk > latin:
        return "cjk"
    if latin > cjk:
        return "latin"
    return None


def _isolate_marker_lines(text: str) -> str:
    """Isolate all-caps snake-case marker lines (SPLIT_HERE) into
    their own paragraphs with surrounding blank lines."""
    lines = text.split("\n")
    out = []
    in_fence = False
    n = len(lines)
    for i, line in enumerate(lines):
        if _CODE_FENCE_PREFIX_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not _MARKER_LINE_RE.match(line.strip()):
            out.append(line)
            continue
        if out and out[-1].strip():
            out.append("")
        out.append(line)
        nxt = lines[i + 1] if i + 1 < n else ""
        if nxt.strip() and not _MARKER_LINE_RE.match(nxt.strip()):
            out.append("")
    return "\n".join(out)


def _apply_hard_line_breaks(text: str) -> str:
    """Append a double-space (GFM hard break) to list-like lines;
    leave prose sentences alone.

    Prose lines (ending with sentence/continuation punctuation or
    long) let in-paragraph soft breaks fold naturally, avoiding
    fake one-sentence-per-line "pseudo lists".
    """
    lines = text.split("\n")
    out = []
    in_fence = False
    n = len(lines)
    for i, line in enumerate(lines):
        if _CODE_FENCE_PREFIX_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not line.strip():
            out.append(line)
            continue
        if (
            line.lstrip().startswith("|")
            or bool(_LIST_OR_QUOTE_RE.match(line))
        ):
            # table/list/quote marker lines: markdown itself
            # preserves line boundaries there (bare "#" without a
            # space — pseudo headings — are not here; they go
            # through the list-like check)
            out.append(line)
            continue
        next_line = lines[i + 1] if i + 1 < n else ""
        next_stripped = next_line.strip()
        next_is_text = (
            bool(next_stripped)
            and not _CODE_FENCE_PREFIX_RE.match(next_line)
            and not next_line.lstrip().startswith("|")
        )
        if not next_is_text:
            out.append(line)
            continue
        stripped = line.strip()
        ordinal_now = bool(_CHINESE_ORDINAL_RE.match(stripped))
        ordinal_next = bool(
            _CHINESE_ORDINAL_RE.match(next_stripped)
        )
        short_listy = (
            len(stripped) <= 40
            and stripped[-1] not in _SENTENCE_TAIL_CHARS
        )
        script_now = _dominant_script(stripped)
        script_next = _dominant_script(next_stripped)
        cross_script = bool(
            script_now and script_next and script_now != script_next
        )
        if (
            (
                ordinal_now
                or ordinal_next
                or short_listy
                or cross_script
            )
            and not line.endswith(" ")
            and not line.endswith("\\")
        ):
            out.append(line + "  ")
        else:
            out.append(line)
    return "\n".join(out)


def _ensure_fence_closed(text: str) -> str:
    """Append a closing fence when the unit body has an unpaired
    opening fence.

    An unclosed fence (a common AI defect) makes downstream markdown
    renderers swallow everything after it (including the next
    heading) into the code block — the presentation layer closes
    the fence to restore the normal document flow. The closing
    marker matches the last opening fence.
    """
    opens = 0
    last_marker = "```"
    for line in text.split("\n"):
        m = _CODE_FENCE_PREFIX_RE.match(line)
        if m:
            opens += 1
            last_marker = m.group(0).strip()[:3]
    if opens % 2 == 1:
        if text and not text.endswith("\n"):
            text += "\n"
        text += last_marker
    return text


class MarkdownExporter(BaseExporter):
    """Markdown-layer exporter — pure presentation layer (contract:
    docs/*/internal/artifact_contract.md §2.4).

    Contract:
        - markdown is only a readable rendering of base-granularity
          units, not the fact source; structured facts live in
          representation/{base}_repr.json.
        - Writes no "# Chunk N" headings and no META HTML comments.
        - A unit's heading renders as a Markdown heading at
          heading_level; the body is unit.text verbatim.
    """

    def export(
            self,
            data,
            path,
            unresolved_registry=None,
            render_unresolved_markers: bool = False,
    ):
        """Render markdown and atomically write it to path.

        Args:
            data: list of base-granularity RepresentationUnits (see
                to_markdown).
            path: output file path.
            unresolved_registry: optional object-level unresolved
                registry (the ``*_unresolved`` key family in
                document.metadata, marker_contract.md §1).
            render_unresolved_markers: marker rendering policy,
                default False (marker_contract.md §3: the default
                human-reading version renders no markers, output is
                byte-identical to before the feature existed).
        """
        markdown = self.to_markdown(
            data,
            unresolved_registry=unresolved_registry,
            render_unresolved_markers=render_unresolved_markers,
        )
        # atomic write: temp + os.replace — an interrupted process
        # never leaves a partial markdown.
        atomic_write_text(path, markdown)

    @staticmethod
    def _code_fence_length(body: str) -> int:
        """Outer fence length: starts at 3, exceeds the longest
        closable inner fence run in the body."""
        longest = 3
        for m in _FENCE_RUN_RE.finditer(body):
            longest = max(longest, len(m.group(1)) + 1)
        return longest

    def _render_code_unit(self, chunk, meta: dict, last_heading_key):
        """Presentation of a code-domain unit: symbol heading +
        language-tagged fenced code block.

        The unit body is raw source code (no fence): the
        presentation layer embeds it into complete markdown —
        - symbol units: the symbol name renders as a heading
          (top level ###, nested #### with the parent-symbol
          prefix Class.method), so the tree-sitter structural
          parse enters the markdown outline;
        - module_fragment / component_block: no heading, fence
          only;
        - the fence length adapts (upgraded when the body nests
          fences), the language tag comes from
          metadata.language;
        - narrative typography transforms are bypassed (bare-list
          rewriting / hard breaks / marker-line isolation would
          corrupt source code rather than fix it — source must be
          presented verbatim).

        Returns (rendered line list, new last_heading_key).
        """
        parts = []
        heading_key = last_heading_key
        chunk_kind = meta.get("chunk_kind")
        name = meta.get("name") or ""
        if chunk_kind == "symbol" and name and not name.startswith("<"):
            parent = meta.get("parent")
            label = f"{parent}.{name}" if parent else name
            level = 4 if parent else 3
            heading_key = (level, label)
            if heading_key != last_heading_key:
                parts.append(f"{'#' * level} {label}\n")
        body = (chunk.text or "").rstrip("\n")
        if body:
            if _CODE_FENCE_PREFIX_RE.match(body.lstrip()):
                # narrative-domain embedded code blocks keep their
                # original fence after upgrade (fence lines are
                # source-document lines, c4 line-level integrity) —
                # a body already fenced is presented directly, no
                # extra wrapping (no double-nesting contract).
                parts.append(body)
            else:
                # splitting only applies to chunks with explicit
                # narrative-prefix evidence
                # (context_prefix_count: breadcrumb inheritance
                # from isolate; orphan_merged_into_code: orphaned
                # lead-in sentences merged in). Pure source units
                # (bodies containing column-0 inner fence strings,
                # e.g. examples embedded in docstrings) are not
                # split candidates — the heuristic would
                # misclassify source lines as leading narrative,
                # and would also bypass the adaptive outer-fence
                # upgrade contract; keep the adaptive upgrade
                # path.
                has_narrative_prefix = bool(
                    meta.get("context_prefix_count")
                    or meta.get("orphan_merged_into_code")
                )
                fence_idx = None
                if has_narrative_prefix:
                    body_lines = body.split("\n")
                    fence_idx = next(
                        (
                            i for i, ln in enumerate(body_lines)
                            if _CODE_FENCE_PREFIX_RE.match(ln)
                        ),
                        None,
                    )
                if fence_idx is not None and fence_idx > 0:
                    # leading narrative (breadcrumbs "[Context:]" /
                    # lead-in sentences / orphan heading lines,
                    # brought in by isolate breadcrumb inheritance
                    # and orphan merging) renders outside the fence —
                    # wrapping it into the fence would turn those
                    # source-document lines into code comments (c4
                    # line-level integrity lost) and break the
                    # markdown outline structure.
                    prefix = "\n".join(body_lines[:fence_idx]).strip("\n")
                    code_part = "\n".join(body_lines[fence_idx:])
                    if prefix:
                        parts.append(prefix)
                        parts.append("")
                    parts.append(code_part)
                else:
                    fence = "`" * self._code_fence_length(body)
                    language = meta.get("language") or ""
                    parts.append(f"{fence}{language}\n{body}\n{fence}")
        parts.append("")
        return parts, heading_key

    _UNRESOLVED_APPENDIX_TITLE = (
        "未解析对象登记（object-level unresolved registry）"
    )

    def _append_unresolved_marker_appendix(
            self,
            markdown: str,
            unresolved_registry,
            enabled: bool,
    ) -> str:
        """Append the object-level unresolved registry as a
        quote-block note, per policy.

        marker_contract.md §3:
            - disabled by default (enabled=False or empty
              registry) returns the input unchanged — the default
              markdown output is byte-identical to before the
              feature existed (§5.3 acceptance);
            - the rendered form is an end-of-document quote-block
              note (one of the contract-allowed forms; in-body
              inline injection depends on unit-level
              resolution_status and is a Phase-2 duty — this layer
              invents no positions);
            - marker syntax [[{RESOLUTION}:{TYPE}:{object_id}]],
              status words derived from the record's
              resolution/object_type; REDACTED is not in the value
              domain (§0 rejected item — parsers never produce
              it).
        """
        if not enabled or not unresolved_registry:
            return markdown
        lines = [self._UNRESOLVED_APPENDIX_TITLE]
        for block in unresolved_registry.values():
            records = (
                (block or {}).get("records")
                if isinstance(block, dict) else None
            )
            if not records:
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                marker = "[[{}:{}:{}]]".format(
                    str(record.get("resolution", "unresolved")).upper(),
                    str(record.get("object_type", "unknown")).upper(),
                    record.get("object_id", "unknown"),
                )
                detail = str(record.get("reason_code") or "")
                source = record.get("source")
                if source:
                    detail = (
                        f"{detail} — {source}" if detail else str(source)
                    )
                lines.append(f"{marker} {detail}".rstrip())
        # registry with no renderable records (empty records)
        # appends nothing
        if len(lines) == 1:
            return markdown
        block_text = "\n".join(f"> {ln}" for ln in lines)
        body = markdown.rstrip("\n")
        sep = "\n\n" if body else ""
        return body + sep + block_text + "\n"

    def to_markdown(
            self,
            data,
            unresolved_registry=None,
            render_unresolved_markers: bool = False,
    ):
        """Render base-granularity units into readable markdown
        text.

        Pure presentation transform: unit headings render as
        Markdown headings at heading_level, bodies take unit.text
        verbatim; code-domain units go through the dedicated
        fenced-code + symbol-heading presentation (bypassing the
        narrative typography transforms). Consecutive duplicate
        headings render only the first occurrence (AI repetition
        guard).

        Args:
            data: list of RepresentationUnits (the runtime has
                already normalized them to base granularity).
            unresolved_registry: optional object-level unresolved
                registry (``*_unresolved`` key family,
                marker_contract.md §1); consumed only when
                render_unresolved_markers=True.
            render_unresolved_markers: marker rendering policy,
                default False (default output has zero diff, §5.3).

        Returns:
            str: the full rendered markdown.
        """
        # base units list (main path: runtime passes normalized
        # base units)
        if isinstance(data, list):
            parts = []
            # consecutive-heading window: (level, heading) of the
            # last unit that actually rendered a heading.
            # - same as the previous heading → AI repetition, keep
            #   only the first rendering;
            # - a different heading appears → the window ends, later
            #   same-named headings may render again.
            # Code-block units also join the window: only when the
            # block is the first unit of a section (heading not yet
            # rendered) does the heading render; after that it is
            # not repeated.
            # Deduplication is based on "actually rendered" — the
            # window must not track units whose heading was
            # skipped, otherwise the next same-named body heading
            # would be wrongly suppressed and the whole section
            # would vanish.
            last_heading_key = None
            for chunk in data:
                if isinstance(chunk, RepresentationUnit):
                    meta = getattr(chunk, "metadata", None) or {}
                    # code-domain unit: source embedded in fence +
                    # symbol heading, bypassing the narrative
                    # typography transforms (list rewriting / hard
                    # breaks would rewrite source code)
                    if meta.get("chunking") == _CODE_DOMAIN_MARK:
                        code_parts, last_heading_key = (
                            self._render_code_unit(
                                chunk, meta, last_heading_key
                            )
                        )
                        parts.extend(code_parts)
                        continue
                    outline_only = bool(meta.get("outline_only"))
                    heading = _LEADING_HASHES_RE.sub(
                        "", (getattr(chunk, "heading", "") or "").strip()
                    )
                    if heading:
                        level = 2
                        try:
                            level = chunk.get_heading_level() or 2
                        # heading-level probe failure falls back to
                        # level 2 (export-layer cosmetics, carries no
                        # semantic promise).
                        except Exception:
                            pass
                        level = max(1, min(6, int(level)))
                        key = (level, heading)
                        if key != last_heading_key:
                            parts.append(f"{'#' * level} {heading}\n")
                        last_heading_key = key
                    # outline-only unit: render only the heading
                    # line; the body (which is the heading text
                    # itself) is not emitted again
                    if outline_only:
                        continue
                    # task-list marker literal escaping: "- [x]" is
                    # data text, not GFM semantics
                    body = _TASK_LIST_MARKER_RE.sub(
                        lambda m: m.group(1).replace("[", "\\[")
                        + m.group(2)
                        + m.group(3).replace("]", "\\]"),
                        chunk.text,
                    )
                    # suspected separator-less numeric/marker lists:
                    # rewritten into proper lists at the presentation
                    # layer
                    body = _normalize_bare_numeric_lists(body)
                    body = _normalize_bare_marker_lists(body)
                    # chunk marker lines (SPLIT_HERE) isolated into
                    # their own paragraphs
                    body = _isolate_marker_lines(body)
                    # hard breaks: keep line typography within the
                    # paragraph (each line displayed independently)
                    body = _apply_hard_line_breaks(body)
                    # unclosed fences get a closing fence so the
                    # renderer cannot swallow the following document
                    # flow
                    body = _ensure_fence_closed(body)
                    parts.append(body)
                    parts.append("")
            # strip trailing blank lines
            text = "\n".join(parts)
            markdown = (
                text.rstrip() + "\n" if text.strip() else ""
            )

        # document dict (legacy pass-through)
        elif isinstance(data, dict):
            markdown = data.get("content", "")

        else:
            markdown = str(data)

        # marker rendering policy (off by default): when enabled,
        # append the object-level unresolved registry as an
        # end-of-document quote-block note (marker_contract.md §3).
        return self._append_unresolved_marker_appendix(
            markdown,
            unresolved_registry,
            render_unresolved_markers,
        )
