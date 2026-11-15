# ============================================================
# Narrative Domain
# Markdown block-structure facts (evidence-only census)
# ============================================================
"""Markdown block-structure evidence registration — markdown_block_structure_prereg_v1 (finalized).

Discipline (aligned with the txt satellite-module precedent):

- **evidence-only**: register facts only, never rewrite ``text``, never touch representation,
  never change any existing metadata key;
- all keys additive, written only when detected (``markdown_parse_status`` is the always-written exception);
- every census uniformly excludes **fenced code-block ranges** and **front matter ranges**
  (code-block/front-matter content is not parsed as Markdown body — blueprint §4.5);
- local failures never sink the whole document: each sub-census is independently fault-tolerant; overall failure degrades silently to the
  ``markdown_structure_error`` key, and the tri-state derivation counts that key into partial reasons;
- the tri-state is a **derived view** (aligned with the txt_parse_status precedent): no new detection logic,
  it only reads registered evidence keys; reasons are emitted in frozen order.

Dependency posture (verified before the 2026-10-01 sign-off): YAML reuses the existing hard dependency PyYAML
(the same ``import yaml`` as config/yaml_parser); TOML reuses the existing
``tomllib``-first / ``tomli``-fallback posture; JSON uses the stdlib.
"""

import bisect
import json
import re
from pathlib import Path

try:  # 与 config/toml_parser 同款既有姿势
    import tomllib  # noqa: F401  (Python >= 3.11)
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import yaml

# ── Capping discipline (aligned with the txt sensitive-signal/position registry precedent) ──────────
_RAW_LIMIT = 16 * 1024          # front matter raw 字符上限
_LOCATION_LIMIT = 20            # 行号/位置清单每类封顶
_TABLE_ENTRY_LIMIT = 50         # 表格条目封顶
_FM_JSON_PROBE_LINES = 64       # JSON front matter 探测行数上限

# ── Regexes (census-grade loose matching, counting only, no interpretation) ──────────────
_RE_TASK_ITEM = re.compile(r"^\s*[-*+]\s+\[([ xX])\](?:\s|$)")
_RE_REF_DEF = re.compile(r"^\s{0,3}\[([^\]^][^\]]*)\]:\s*(.+)$")
_RE_FOOTNOTE_DEF = re.compile(r"^\s{0,3}\[\^([^\]]+)\]:\s*(.*)$")
_RE_FOOTNOTE_REF = re.compile(r"\[\^([^\]]+)\]")
_RE_INLINE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_RE_INLINE_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]*)\)")
_RE_REF_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\[([^\]]*)\]")
_RE_REF_IMAGE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
_RE_AUTOLINK_URL = re.compile(r"<((?:https?://|mailto:)[^>\s]+)>")
_RE_AUTOLINK_MAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_RE_BARE_URL = re.compile(r"(?<![\(\[<])(?<![\w`])https?://[^\s<>)\]\"']+")
_RE_HTML_IMG = re.compile(r"<img\b", re.IGNORECASE)
_RE_CODE_SPAN = re.compile(r"(`+)[^`]+?\1(?!`)")
_RE_TABLE_DELIM = re.compile(
    r"^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)*\|?\s*$"
)
# Meta-directives (third-batch blueprint §3): <!-- key: value --> and known whole-word directives
_RE_RENDER_DIRECTIVE = re.compile(
    r"<!--\s*([A-Za-z_][\w-]*)\s*:\s*([^>]*?)\s*-->"
)
_RE_RENDER_WORD = re.compile(
    r"<!--\s*(?:pagebreak|toc|more|break|stop)\s*-->"
)
_RE_ATTR_BLOCK = re.compile(r"\{\s*[.#][\w-][^}]*\}\s*$")
# Template expressions (third-batch blueprint §6): shape recognition only, no engine guessing, never execute
_RE_TMPL_DOUBLE = re.compile(r"\{\{.*?\}\}")
_RE_TMPL_PERCENT = re.compile(r"\{%.*?%\}")
_RE_TMPL_DOLLAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_RE_TMPL_ERB = re.compile(r"<%=.*?%>")

# markdown-family dialect profile (routing_prereg F2): extension→profile mapping,
# the ordinary family (incl. .md/.markdown/.mdown/.mkdn/.mdwn) without extension evidence → null, no guessing
_EXTENSION_PROFILES = {
    ".mdx": "mdx",
    ".rmd": "rmarkdown",
    ".qmd": "quarto",
}
_RE_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")

# Git merge-conflict markers (routing_prereg F3): triple (open/separator/close);
# orphan markers are counted, not interpreted; pseudo-conflicts inside fenced blocks are hard-excluded and only counted in_code_blocks
_RE_CONFLICT_OPEN = re.compile(r"^<{7}(?:[ \t]+(.*))?$")
_RE_CONFLICT_SEP = re.compile(r"^={7}[ \t]*$")
_RE_CONFLICT_CLOSE = re.compile(r"^>{7}(?:[ \t]+(.*))?$")

# Generation provenance (provenance prereg batch 3-A, boundary blueprint §7 + §2 single-file part):
# four probe sources, explicit whitelist — under-detect rather than mis-report; banners inside fenced blocks only count
# excluded_in_code_blocks, never as evidence; free-text sentences are not collected (D2=A)
_RE_HTML_COMMENT_BLOCK = re.compile(r"<!--(.*?)-->", re.DOTALL)
_RE_GEN_BANNER_PHRASE = re.compile(
    r"(?:automatically\s+generated|auto-?generated|generated)"
    r"\s+(?:by|with)\s+(\S.*)", re.IGNORECASE)
_RE_TOOL_VERSION = re.compile(
    r"^(?P<tool>\S.*?)\s+v?(?P<version>\d[\w.\-]*)\s*$")
_RE_TOC_PAIR_OPEN = re.compile(r"<!--\s*toc\s*-->", re.IGNORECASE)
_RE_TOC_PAIR_CLOSE = re.compile(r"<!--\s*/toc\s*-->", re.IGNORECASE)
# mkdocs uppercase placeholder; case-sensitive so as not to confuse with the obsidian wikilink [[toc]]
_RE_TOC_SINGLE = re.compile(r"\[\[\s*TOC\s*\]\]")
_RE_TOC_ENTRY = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_FM_GENERATOR_KEYS = frozenset(
    {"generator", "generated_by", "generated", "generated_at"})
_PROV_RAW_LIMIT = 200        # 证据原文截断上限 (prereg §2.2)
_GEN_TOOL_TEXT_LIMIT = 80    # 工具名捕获保守截断

_KNOWN_RENDER_WORDS = frozenset(
    {"pagebreak", "toc", "more", "break", "stop"}
)


# ── fence / front matter ranges (census exclusion domains) ──────────
def _fenced_line_set(text):
    """Line-number set covered by fenced code blocks (1-based, fence lines included, unclosed block runs to EOF).

    Reuses the shared extractor (markdown_blocks) — fence semantics stay strictly identical with the
    representation layer and embedded parsing; no parallel implementation is invented.
    """
    from provendoc.core.text.markdown_blocks import (
        extract_fenced_code_blocks,
    )

    fenced = set()
    unclosed = 0
    for block in extract_fenced_code_blocks(text):
        if not block["closed"]:
            unclosed += 1
        for ln in range(block["line_start"], block["line_end"] + 1):
            fenced.add(ln)
    return fenced, unclosed


def _detect_front_matter(lines):
    """File-head front matter probing (tri-state boundary, never decided by a single regex).

    Returns:
        dict, or None (nothing detected → the key is not written):
            style: yaml|toml|json
            status: parsed|empty|unclosed|parse_error
            line_start / line_end: 1-based line range (delimiter lines included)
            top_level_keys: top-level keys when parsed as a dict (otherwise None)
            raw: original block text (delimiters included, truncated to ≤16KB)
            raw_truncated: bool
            error: exception summary when parse_error (None otherwise)
    """
    if not lines:
        return None

    first = lines[0].strip()

    if first == "---" or first == "+++":
        style, sep = ("yaml", "---") if first == "---" else ("toml", "+++")
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == sep:
                end = i
                break
        if end is None:
            # Unclosed: no pairing delimiter up to EOF — the boundary does not exist, line_end honestly stays
            # None (no fabricated range end, never enters the census exclusion domain), zero impact on the body
            content = "\n".join(lines[1:])
            raw = "\n".join(lines)
            result = _fm_result(style, "unclosed", 1, None,
                                content, raw, None)
            result["probe_lines"] = len(lines)
            return result
        if end == 1:
            return _fm_result(style, "empty", 1, end + 1, "", "\n".join(lines[:end + 1]), None)
        content = "\n".join(lines[1:end])
        raw = "\n".join(lines[:end + 1])
        if not content.strip():
            return _fm_result(style, "empty", 1, end + 1, content, raw, None)
        if style == "yaml":
            loader = yaml.safe_load
        else:
            loader = tomllib.loads
        try:
            data = loader(content)
        except Exception as exc:  # 非法内容: 保留 raw, 不算解析失败
            return _fm_result(style, "parse_error", 1, end + 1,
                              content, raw, str(exc)[:200])
        keys = list(data.keys()) if isinstance(data, dict) else None
        return _fm_result(style, "parsed", 1, end + 1, content, raw, None,
                          keys)

    if first.startswith("{"):
        # JSON front matter: accumulate line by line and try to parse; probe failure never guesses (None)
        accumulated = []
        limit = min(len(lines), _FM_JSON_PROBE_LINES)
        for idx in range(limit):
            accumulated.append(lines[idx])
            candidate = "\n".join(accumulated)
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                raw = candidate
                return _fm_result("json", "parsed", 1, idx + 1,
                                  candidate, raw, None, list(data.keys()))
        return None

    return None


def _fm_result(style, status, line_start, line_end, content, raw, error,
               keys=None):
    truncated = len(raw) > _RAW_LIMIT
    return {
        "style": style,
        "status": status,
        "line_start": line_start,
        "line_end": line_end,
        "top_level_keys": keys,
        "raw": raw[:_RAW_LIMIT],
        "raw_truncated": truncated,
        "error": error,
    }


# ── GFM table census (F2) ────────────────
def _count_cells(line):
    """Column count split on unescaped pipes (GFM convention), returns (cols, escaped_pipe)."""
    escaped = "\\|" in line
    probe = line.replace("\\|", "\x00")
    stripped = probe.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return len(stripped.split("|")), escaped


def _delimiter_alignments(line):
    """Delimiter row → alignment sequence (left/center/right/none)."""
    probe = line.replace("\\|", "\x00").strip()
    if probe.startswith("|"):
        probe = probe[1:]
    if probe.endswith("|"):
        probe = probe[:-1]
    alignments = []
    for cell in probe.split("|"):
        cell = cell.strip()
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            alignments.append("center")
        elif left:
            alignments.append("left")
        elif right:
            alignments.append("right")
        else:
            alignments.append("none")
    return alignments


def _is_table_delimiter(line):
    stripped = line.strip()
    if "|" not in stripped:
        return False  # 纯 --- 是分隔线/underline, 不是表格
    return bool(_RE_TABLE_DELIM.match(stripped))


def _collect_tables(lines, excluded):
    """Header + delimiter-row pairing detection, collecting consecutive pipe rows forward (ragged rows registered as-is, never truncated)."""
    entries = []
    truncated = False
    i = 0
    n = len(lines)
    while i < n:
        line_no = i + 1
        if line_no in excluded:
            i += 1
            continue
        line = lines[i]
        if "|" not in line.strip() or _is_table_delimiter(line):
            i += 1
            continue
        if i + 1 >= n:
            break
        next_line = lines[i + 1]  # 行号 i+2
        if _is_table_delimiter(next_line) and (i + 2) not in excluded:
            if len(entries) >= _TABLE_ENTRY_LIMIT:
                truncated = True
                break
            header_cols, escaped = _count_cells(line)
            alignments = _delimiter_alignments(next_line)
            delim_cols = len(alignments)
            ragged = []
            ragged_truncated = False
            j = i + 2
            max_cols = max(header_cols, delim_cols)
            while j < n:
                if (j + 1) in excluded:
                    break
                body = lines[j]
                if "|" not in body.strip() or not body.strip():
                    break
                cols, body_escaped = _count_cells(body)
                if body_escaped:
                    escaped = True
                max_cols = max(max_cols, cols)
                if cols != header_cols:
                    if len(ragged) < _LOCATION_LIMIT:
                        ragged.append({"line": j + 1, "cols": cols})
                    else:
                        ragged_truncated = True
                j += 1
            entries.append({
                "index": len(entries),
                "line_start": line_no,
                "line_end": j,  # 1-based: 最后一个体行号 (无体行 = 分隔行)
                "header_cols": header_cols,
                "delimiter_cols": delim_cols,
                "max_cols": max_cols,
                "alignments": alignments,
                "ragged_rows": ragged,
                "ragged_rows_truncated": ragged_truncated,
                "escaped_pipe": escaped,
            })
            i = j
            continue
        i += 1

    if not entries:
        return None
    return {"count": len(entries), "entries": entries,
            "entries_truncated": truncated}


# ── Inline census family (F3–F6, F9, F10) ──────────
def _iter_body_lines(lines, excluded):
    """Yields (line number, line text), skipping the fenced / front matter exclusion domains."""
    for idx, line in enumerate(lines, 1):
        if idx not in excluded:
            yield idx, line


def _collect_task_lists(lines, excluded):
    total = checked = unchecked = 0
    markers = {"[x]": 0, "[X]": 0, "[ ]": 0}
    locations = []
    locations_truncated = False
    for line_no, line in _iter_body_lines(lines, excluded):
        m = _RE_TASK_ITEM.match(line)
        if not m:
            continue
        total += 1
        marker = m.group(1)
        if marker == " ":
            unchecked += 1
            markers["[ ]"] += 1
        else:
            checked += 1
            markers[f"[{marker}]"] += 1
        if len(locations) < _LOCATION_LIMIT:
            locations.append(line_no)
        else:
            locations_truncated = True
    if not total:
        return None
    return {
        "total": total,
        "checked": checked,
        "unchecked": unchecked,
        "raw_markers": markers,
        "locations": locations,
        "locations_truncated": locations_truncated,
    }


def _collect_link_census(lines, excluded):
    inline = reference = autolink = bare = 0
    html_img = 0
    defined_refs = set()
    used_refs = set()
    javascript_flags = data_flags = 0
    locations = []
    locations_truncated = False

    def _flag(target):
        nonlocal javascript_flags, data_flags
        t = target.strip().lower()
        if t.startswith("javascript:"):
            javascript_flags += 1
        elif t.startswith("data:"):
            data_flags += 1

    def _hit(line_no):
        nonlocal locations_truncated
        if len(locations) < _LOCATION_LIMIT:
            locations.append(line_no)
        else:
            locations_truncated = True

    for line_no, line in _iter_body_lines(lines, excluded):
        before = (inline + reference + autolink + bare + html_img)
        inline += len(_RE_INLINE_LINK.findall(line))
        reference += len(_RE_REF_LINK.findall(line))
        autolink += len(_RE_AUTOLINK_URL.findall(line))
        autolink += len(_RE_AUTOLINK_MAIL.findall(line))
        bare += len(_RE_BARE_URL.findall(line))
        html_img += len(_RE_HTML_IMG.findall(line))
        for m in _RE_INLINE_LINK.finditer(line):
            _flag(m.group(2))
        for m in _RE_REF_IMAGE.finditer(line):
            used_refs.add(m.group(2) or m.group(1))
        for m in _RE_REF_LINK.finditer(line):
            used_refs.add(m.group(2) or m.group(1))
        d = _RE_REF_DEF.match(line)
        if d:
            defined_refs.add(d.group(1))
            _flag(d.group(2))
        after = (inline + reference + autolink + bare + html_img)
        if after > before:
            _hit(line_no)

    result = {
        "inline": inline,
        "reference": reference,
        "autolink": autolink,
        "bare": bare,
        "reference_definitions": len(defined_refs),
        "undefined_references": len(used_refs - defined_refs),
        "unreferenced_definitions": len(defined_refs - used_refs),
        "protocol_flags": {"javascript": javascript_flags,
                           "data": data_flags},
        "html_img": html_img,
        "locations": locations,
        "locations_truncated": locations_truncated,
    }
    if not any([inline, reference, autolink, bare, defined_refs, html_img]):
        return None
    return result


def _collect_image_census(lines, excluded):
    inline = reference = 0
    locations = []
    locations_truncated = False
    for line_no, line in _iter_body_lines(lines, excluded):
        before = inline + reference
        inline += len(_RE_INLINE_IMAGE.findall(line))
        reference += len(_RE_REF_IMAGE.findall(line))
        if inline + reference > before:
            if len(locations) < _LOCATION_LIMIT:
                locations.append(line_no)
            else:
                locations_truncated = True
    if not (inline or reference):
        return None
    return {"inline": inline, "reference": reference,
            "locations": locations, "locations_truncated": locations_truncated}


def _collect_footnotes(lines, excluded):
    definitions = {}
    references = {}
    def_locations = []
    ref_locations = []
    truncated = False
    for line_no, line in _iter_body_lines(lines, excluded):
        d = _RE_FOOTNOTE_DEF.match(line)
        remainder = line
        if d:
            fid = d.group(1)
            definitions[fid] = definitions.get(fid, 0) + 1
            if len(def_locations) < _LOCATION_LIMIT:
                def_locations.append(line_no)
            else:
                truncated = True
            remainder = line[:d.start()] + line[d.end():]
        for m in _RE_FOOTNOTE_REF.finditer(remainder):
            fid = m.group(1)
            references[fid] = references.get(fid, 0) + 1
            if len(ref_locations) < _LOCATION_LIMIT:
                ref_locations.append(line_no)
            else:
                truncated = True
    if not (definitions or references):
        return None
    duplicate_ids = sum(1 for v in definitions.values() if v > 1)
    return {
        "definitions": len(definitions),
        "references": sum(references.values()),
        "undefined_references": len(set(references) - set(definitions)),
        "unreferenced_definitions": len(set(definitions) - set(references)),
        "duplicate_ids": duplicate_ids,
        "definition_lines": def_locations,
        "reference_lines": ref_locations,
        "locations_truncated": truncated,
    }


def _collect_inline_code(lines, excluded, unclosed_fenced):
    pairs = 0
    unclosed_markers = 0
    for line_no, line in _iter_body_lines(lines, excluded):
        pairs += len(_RE_CODE_SPAN.findall(line))
        if line.count("`") % 2 == 1:
            unclosed_markers += 1
    if not (pairs or unclosed_markers or unclosed_fenced):
        return None
    return {"count": pairs, "unclosed_markers": unclosed_markers,
            "unclosed_fenced_blocks": unclosed_fenced}


def _collect_meta_directives(lines, excluded):
    directives = 0
    keys = []
    attribute_blocks = 0
    locations = []
    locations_truncated = False
    for line_no, line in _iter_body_lines(lines, excluded):
        before = directives + attribute_blocks
        for m in _RE_RENDER_DIRECTIVE.finditer(line):
            directives += 1
            if len(keys) < _LOCATION_LIMIT:
                keys.append(m.group(1))
        for _ in _RE_RENDER_WORD.finditer(line):
            directives += 1
        if _RE_ATTR_BLOCK.search(line):
            attribute_blocks += 1
        if directives + attribute_blocks > before:
            if len(locations) < _LOCATION_LIMIT:
                locations.append(line_no)
            else:
                locations_truncated = True
    if not (directives or attribute_blocks):
        return None
    return {
        "html_comment_directives": directives,
        "directive_keys": keys,
        "attribute_blocks": attribute_blocks,
        "locations": locations,
        "locations_truncated": locations_truncated,
    }


def _collect_template_expressions(lines, excluded):
    double_brace = percent_tag = dollar_var = erb = 0
    locations = []
    locations_truncated = False
    for line_no, line in _iter_body_lines(lines, excluded):
        before = double_brace + percent_tag + dollar_var + erb
        double_brace += len(_RE_TMPL_DOUBLE.findall(line))
        percent_tag += len(_RE_TMPL_PERCENT.findall(line))
        dollar_var += len(_RE_TMPL_DOLLAR.findall(line))
        erb += len(_RE_TMPL_ERB.findall(line))
        if double_brace + percent_tag + dollar_var + erb > before:
            if len(locations) < _LOCATION_LIMIT:
                locations.append(line_no)
            else:
                locations_truncated = True
    total = double_brace + percent_tag + dollar_var + erb
    if not total:
        return None
    # executed=false is a discipline declaration, not a runtime result: template expressions are never executed, never expanded
    return {
        "total": total,
        "double_brace": double_brace,
        "percent_tag": percent_tag,
        "dollar_var": dollar_var,
        "erb": erb,
        "template_engine": "unknown",
        "executed": False,
        "locations": locations,
        "locations_truncated": locations_truncated,
    }


# ── HTML comment census (r25 adversarial-repair batch) ─────────────
def _collect_html_comments(lines, excluded, fenced):
    """Generic HTML comment census (r25 adversarial-repair batch, evidence-only).

    The existence and verbatim content of ``<!-- ... -->`` comments are facts the document declares:
    comments do not enter the narrative text by contract, but an undisclosed omission is a registration
    gap (same family as the EPUB XHTML comment census). Multi-line comments are scanned over the full text
    with line numbers derived back (the same posture as provenance S2); comments inside fenced code
    blocks only count ``excluded_in_code_blocks``, comments inside the front matter exclusion domain are skipped;
    counts are full true values, samples bounded by ≤_LOCATION_LIMIT (body verbatim registration,
    over-long ones truncated by _PROV_RAW_LIMIT). All-zero (including zero fenced hits)
    → no key written (clean-control zero-new-key discipline).
    """
    joined = "\n".join(lines)
    newline_offsets = [i for i, ch in enumerate(joined) if ch == "\n"]
    count = 0
    in_code_blocks = 0
    samples = []
    samples_truncated = False
    for m in _RE_HTML_COMMENT_BLOCK.finditer(joined):
        start_line = _line_of(newline_offsets, m.start())
        end_line = _line_of(newline_offsets, m.end() - 1)
        if any(ln in fenced for ln in range(start_line, end_line + 1)):
            in_code_blocks += 1
            continue
        if start_line in excluded:  # front matter 域: 非正文事实
            continue
        count += 1
        if len(samples) < _LOCATION_LIMIT:
            samples.append({
                "line": start_line,
                "text": m.group(1).strip()[:_PROV_RAW_LIMIT],
            })
        else:
            samples_truncated = True
    if not (count or in_code_blocks):
        return None
    result = {
        "count": count,
        "samples": samples,
        "excluded_in_code_blocks": in_code_blocks,
    }
    if samples_truncated:
        result["samples_truncated"] = True
    return result


# ── Generation-provenance census (provenance prereg batch 3-A) ──────
def _line_of(newline_offsets, pos):
    """Character offset → 1-based line number (newline_offsets is the sorted newline-offset table)."""
    return bisect.bisect_right(newline_offsets, pos) + 1


def _collect_generation_provenance(metadata, lines, excluded, fenced):
    """Generation provenance and render-artifact recognition — evidence-only, explicit whitelist, no guessing.

    - S1 front matter generator keys: whitelist hits on key names only (values not extracted,
      the fm raw is the evidence, no re-parsing);
    - S2 HTML comment banners: generated by/with phrases inside the comment domain +
      conservative tool-name/version capture (trailing numeric tokens count as versions);
    - S3 do-not-edit advisories: exact phrase whitelist (English case-insensitive) +
      Chinese paired phrases, same-line decision;
    - S4 generated TOC: `<!-- toc -->`…`<!-- /toc -->` pairing
      (case-insensitive) and the mkdocs uppercase `[[TOC]]` single marker;
    - hits inside fenced blocks only count excluded_in_code_blocks, never as evidence;
    - free-text sentences are not collected (D2=A, under-detect rather than mis-report);
    - no candidate evidence at all → no key written.
    """
    candidates = []
    candidates_truncated = False
    in_code_blocks = 0

    # S1: front matter generator keys
    fm = metadata.get("front_matter")
    if isinstance(fm, dict) and fm.get("status") == "parsed":
        keys = fm.get("top_level_keys")
        if isinstance(keys, list):
            for key in keys:
                if str(key).lower() not in _FM_GENERATOR_KEYS:
                    continue
                if len(candidates) < _LOCATION_LIMIT:
                    candidates.append({
                        "source": "front_matter_key",
                        "key": str(key),
                        "tool": None,
                        "version": None,
                        "raw": None,
                        "line_start": fm.get("line_start"),
                        "line_end": fm.get("line_end"),
                    })
                else:
                    candidates_truncated = True

    # S2: HTML comment banners (multi-line comments scanned over the full text, line numbers derived back)
    joined = "\n".join(lines)
    newline_offsets = [i for i, ch in enumerate(joined) if ch == "\n"]
    for m in _RE_HTML_COMMENT_BLOCK.finditer(joined):
        start_line = _line_of(newline_offsets, m.start())
        end_line = _line_of(newline_offsets, m.end() - 1)
        if any(ln in fenced for ln in range(start_line, end_line + 1)):
            in_code_blocks += 1
            continue
        if start_line in excluded:  # front matter 域: 非正文事实
            continue
        bm = _RE_GEN_BANNER_PHRASE.search(m.group(1))
        if not bm:
            continue
        head = bm.group(1).strip().splitlines()[0].strip()
        head = head.split(",")[0].split(";")[0].strip()[:_GEN_TOOL_TEXT_LIMIT]
        vm = _RE_TOOL_VERSION.match(head)
        tool = vm.group("tool").strip() if vm else head
        version = vm.group("version") if vm else None
        if len(candidates) < _LOCATION_LIMIT:
            candidates.append({
                "source": "html_comment",
                "key": None,
                "tool": tool or None,
                "version": version,
                "raw": m.group(0)[:_PROV_RAW_LIMIT],
                "line_start": start_line,
                "line_end": end_line,
            })
        else:
            candidates_truncated = True

    # S3: do-not-edit advisories (exact phrases in the body; fenced hits counted only)
    edit_advisory = False
    for idx, line in enumerate(lines, 1):
        low = line.lower()
        if not ("do not edit" in low or "machine generated" in low
                or "auto-generated file" in low
                or ("此文件由" in line and "自动生成" in line)
                or ("自动生成" in line and "请勿" in line)):
            continue
        if idx in fenced:
            in_code_blocks += 1
            continue
        if idx in excluded:
            continue
        edit_advisory = True

    # S4: generated TOC (paired markers scanned first, then paired in order; no close → no fabricated range)
    toc_blocks = []
    toc_single = []
    toc_truncated = False
    opens = []
    closes = []
    for idx, line in enumerate(lines, 1):
        if _RE_TOC_PAIR_OPEN.search(line):
            if idx in fenced:
                in_code_blocks += 1
            elif idx not in excluded:
                opens.append(idx)
        if _RE_TOC_PAIR_CLOSE.search(line):
            if idx in fenced:
                in_code_blocks += 1
            elif idx not in excluded:
                closes.append(idx)
        if _RE_TOC_SINGLE.search(line):
            if idx in fenced:
                in_code_blocks += 1
            elif idx not in excluded:
                if len(toc_single) < _LOCATION_LIMIT:
                    toc_single.append(idx)
                else:
                    toc_truncated = True
    consumed = set()
    for o in opens:
        close = None
        for c in closes:
            if c > o and c not in consumed:
                close = c
                break
        if close is None:
            continue
        consumed.add(close)
        entries = sum(
            1 for ln in range(o + 1, close)
            if ln not in fenced and ln not in excluded
            and _RE_TOC_ENTRY.match(lines[ln - 1]))
        if len(toc_blocks) < _LOCATION_LIMIT:
            toc_blocks.append({"line_start": o, "line_end": close,
                               "entries": entries})
        else:
            toc_truncated = True

    if not (candidates or edit_advisory or toc_blocks or toc_single):
        return None
    result = {}
    if candidates:
        result["generator_candidates"] = candidates
        if candidates_truncated:
            result["generator_candidates_truncated"] = True
    if edit_advisory:
        result["edit_advisory"] = True
    if toc_blocks or toc_single:
        toc = {}
        if toc_blocks:
            toc["blocks"] = toc_blocks
        if toc_single:
            toc["single_markers"] = toc_single
        if toc_truncated:
            toc["entries_truncated"] = True
        result["generated_toc"] = toc
    result["excluded_in_code_blocks"] = in_code_blocks
    return result


# ── markdown-family dialect profile (F2) ──────────
def _collect_syntax_profile(metadata, lines, excluded):
    """syntax_profile evidence registration (evidence-only, routing_prereg F2).

    - extension_profile: derived from the extension only (.mdx/.rmd/.qmd);
      the ordinary family never guesses commonmark/gfm without content evidence;
    - content_signals: gfm_candidates = first-batch census hits
      (tables/task lists), obsidian_wikilinks counted independently;
    - profile always equals extension_profile (content signals only corroborate, never upgrade);
    - profile_source two states: extension / extension+content;
    - no extension evidence and no content signal → no key written.
    """
    ext_profile = None
    file_path = metadata.get("file_path")
    if file_path:
        suffix = Path(str(file_path)).suffix.lower()
        ext_profile = _EXTENSION_PROFILES.get(suffix)
    gfm_candidates = bool(
        metadata.get("markdown_tables") or metadata.get("task_lists")
    )
    wikilinks = 0
    for _line_no, line in _iter_body_lines(lines, excluded):
        wikilinks += len(_RE_WIKILINK.findall(line))
    if ext_profile is None and not gfm_candidates and not wikilinks:
        return None
    if ext_profile is None:
        profile_source = None
    elif gfm_candidates:
        profile_source = "extension+content"
    else:
        profile_source = "extension"
    return {
        "extension_profile": ext_profile,
        "content_signals": {
            "gfm_candidates": gfm_candidates,
            "obsidian_wikilinks": wikilinks,
        },
        "profile": ext_profile,
        "profile_source": profile_source,
        "note": "profile 是登记事实不是判定承诺；"
        "未做 JSX/chunk 语义解析，仅登记 profile 事实",
    }


# ── Git merge-conflict marker census (F3) ───────────
def _collect_merge_conflicts(lines, excluded, fenced):
    """Conflict blocks are registered, never resolved (evidence-only, routing_prereg F3).

    Discipline: no merging, no stripping, no body stitching; pseudo-conflicts inside fenced blocks are hard-excluded
    (counted in_code_blocks only, never entering the census main body, never triggering partial);
    orphan ======= (Setext/divider ambiguity) only counts unbalanced, never interpreted.
    """
    entries = []
    truncated = False
    unbalanced = 0
    in_code_blocks = 0
    current = None

    for idx, line in enumerate(lines, 1):
        m_open = _RE_CONFLICT_OPEN.match(line)
        m_close = _RE_CONFLICT_CLOSE.match(line)
        is_sep = _RE_CONFLICT_SEP.match(line)
        if not (m_open or m_close or is_sep):
            continue
        if idx in fenced:
            in_code_blocks += 1
            continue
        if idx in excluded:
            continue  # front matter 排除域: 不是正文事实
        if m_open:
            if current is not None:  # 前一块无 close 被新 open 打断
                current["balanced"] = False
                if len(entries) < _LOCATION_LIMIT:
                    entries.append(current)
                else:
                    truncated = True
            current = {
                "index": len(entries),
                "line_start": idx,
                "line_end": None,
                "ours_label": m_open.group(1),
                "theirs_label": None,
                "separator_line": None,
                "balanced": True,
            }
        elif is_sep:
            if current is not None and current["separator_line"] is None:
                current["separator_line"] = idx
            else:
                unbalanced += 1
        else:
            if current is not None:
                current["line_end"] = idx
                current["theirs_label"] = m_close.group(1)
                current["balanced"] = current["separator_line"] is not None
                if len(entries) < _LOCATION_LIMIT:
                    entries.append(current)
                else:
                    truncated = True
                current = None
            else:
                unbalanced += 1
    if current is not None:  # EOF 无 close
        current["balanced"] = False
        if len(entries) < _LOCATION_LIMIT:
            entries.append(current)
        else:
            truncated = True
    if not (entries or unbalanced or in_code_blocks):
        return None
    return {
        "count": len(entries),
        "entries": entries,
        "entries_truncated": truncated,
        "unbalanced_markers": unbalanced,
        "in_code_blocks": in_code_blocks,
    }


# ── Tri-state derivation (F7, derived-view discipline) ─────────────
def derive_markdown_parse_status(metadata):
    """Derives the document-level tri-state from registered evidence keys — reasons in frozen order.

    failed  = the ``parse_failed`` true value;
    partial reasons frozen order: front_matter → unclosed_fenced_block →
    ragged_table → undefined_footnote → replacement_injected →
    encoding_fallback → markdown_structure_error;
    complete = none of the above.
    """
    reasons = []

    fm = metadata.get("front_matter")
    if isinstance(fm, dict) and fm.get("status") in ("unclosed", "parse_error"):
        reasons.append("front_matter")

    ic = metadata.get("inline_code")
    if isinstance(ic, dict) and ic.get("unclosed_fenced_blocks"):
        reasons.append("unclosed_fenced_block")

    tables = metadata.get("markdown_tables")
    if isinstance(tables, dict):
        if any(e.get("ragged_rows") or e.get("ragged_rows_truncated")
               for e in tables.get("entries", [])):
            reasons.append("ragged_table")

    foot = metadata.get("footnotes")
    if isinstance(foot, dict) and foot.get("undefined_references"):
        reasons.append("undefined_footnote")

    sanitization = metadata.get("sanitization")
    if isinstance(sanitization, dict) and \
            (sanitization.get("replacement_injected") or 0) > 0:
        reasons.append("replacement_injected")

    if metadata.get("encoding_fallback"):
        reasons.append("encoding_fallback")

    if metadata.get("markdown_structure_error"):
        reasons.append("markdown_structure_error")

    if metadata.get("parse_failed"):
        return {"status": "failed", "reasons": ["parse_failed", *reasons]}
    if reasons:
        return {"status": "partial", "reasons": reasons}
    return {"status": "complete", "reasons": []}


# ── Main entry ─────────────
def collect_markdown_structure_facts(text, metadata):
    """Markdown block-structure evidence registration main entry (evidence-only, parser wiring point).

    Each sub-census is independently fault-tolerant; any sub-failure degrades silently to
    ``markdown_structure_error``; the ``markdown_parse_status`` derived view is always written last.
    The whole function never raises, never rewrites text.
    """
    lines = text.splitlines()

    fenced, unclosed_fenced = _fenced_line_set(text)
    excluded = set(fenced)

    fm = None
    try:
        fm = _detect_front_matter(lines)
        if fm and fm.get("status") in ("parsed", "empty"):
            # Enter the census exclusion domain only when the boundary is determined (unclosed/parse_error never fabricate a boundary)
            for ln in range(fm["line_start"], fm["line_end"] + 1):
                excluded.add(ln)
            metadata["front_matter"] = fm
        elif fm:
            metadata["front_matter"] = fm
    except Exception as exc:  # pragma: no cover - 防御
        metadata["markdown_structure_error"] = f"front_matter: {exc}"

    for name, fn, args in (
        ("markdown_tables", _collect_tables, (lines, excluded)),
        ("task_lists", _collect_task_lists, (lines, excluded)),
        ("link_census", _collect_link_census, (lines, excluded)),
        ("image_census", _collect_image_census, (lines, excluded)),
        ("footnotes", _collect_footnotes, (lines, excluded)),
        ("inline_code", _collect_inline_code, (lines, excluded, unclosed_fenced)),
        ("meta_directives", _collect_meta_directives, (lines, excluded)),
        ("html_comments", _collect_html_comments, (lines, excluded, fenced)),
        ("template_expressions", _collect_template_expressions, (lines, excluded)),
        ("markdown_provenance", _collect_generation_provenance,
         (metadata, lines, excluded, fenced)),
        ("merge_conflicts", _collect_merge_conflicts,
         (lines, excluded, fenced)),
        ("syntax_profile", _collect_syntax_profile, (metadata, lines, excluded)),
    ):
        try:
            facts = fn(*args)
            if facts:
                metadata[name] = facts
        except Exception as exc:  # 子 census 失败不拖垮整篇
            metadata["markdown_structure_error"] = f"{name}: {exc}"

    # The derived view is always written (including the exception path: markdown_structure_error is already counted in reasons)
    try:
        metadata["markdown_parse_status"] = derive_markdown_parse_status(metadata)
    except Exception as exc:  # pragma: no cover - 防御
        metadata["markdown_parse_status"] = {
            "status": "partial", "reasons": ["markdown_structure_error"],
        }
        metadata["markdown_structure_error"] = f"derive: {exc}"
