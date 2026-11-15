# ============================================================
# Code Domain
# Embedded-language re-parsing (HTML / Vue script & style blocks)
# ============================================================
"""Delegate <script>/<style> embedded content and frontend template expressions to the matching language grammar for a second-pass parse.

The top-level HTML and Vue grammars expose script/style content as ``raw_text``.
This module selects the target language (javascript/typescript/css/scss) from the
block attributes (type/lang), runs a second fault-tolerant parse of the embedded
text with TreeSitterCodeBackend, then merges symbols, comments, imports and diagnostics
back into the host CodeStructure by byte/row-col offsets — all spans stay anchored on the host file's parse-stage text, so consumers need no re-mapping.

It also handles expression-level parsing of three kinds of frontend template expressions (P3):
    - Vue interpolations ``{{ expr }}`` (the vue grammar's interpolation node)
    - attribute values of expression-style Vue directives (v-if / v-for / @click / :href ...)
    - attribute values of HTML inline handlers (onclick / oninput ...)

Design principles (consistent with contracts.py):
    - Non-executable blocks (type="text/template", lang="less"/"sass", etc.) are not
      force-parsed: tree-sitter-language-pack currently ships no less/sass grammar,
      and for unknown languages we prefer under-reporting to fabricating symbols or noisy diagnostics.
    - Syntax errors in the embedded language likewise fold into CodeDiagnostic
      (the context prefix marks the source block); the host status degrades to recovered rather than failing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from provendoc.core.parsers.code.contracts import (
    CodeDiagnostic,
    CodeStructure,
    CommentUnit,
    ParseStatus,
    SourceSpan,
    StructuralUnit,
    SymbolUnit,
)
from provendoc.core.parsers.code.generic_backend import TreeSitterCodeBackend

# script type values (lowercase) → executable JavaScript. Everything else (text/template,
# application/json, importmap, etc.) is treated as a data block and skips JS parsing.
_JS_TYPE_VALUES = {
    "",
    "text/javascript",
    "application/javascript",
    "text/x-javascript",
    "application/x-javascript",
    "module",
    "text/ecmascript",
    "application/ecmascript",
}

_TS_LANG_VALUES = {"ts", "typescript"}
_TS_TYPE_VALUES = {"ts", "typescript", "text/typescript", "application/typescript"}
_JS_LANG_VALUES = {"", "js", "javascript"}
_SCSS_LANG_VALUES = {"scss"}
# tree-sitter-language-pack currently provides no less/sass grammar. They are explicitly
# recognized instead of being mistaken for unknown attributes; element_block_info emits an auditable skip reason.
_UNAVAILABLE_STYLE_LANGUAGES = {"less", "sass"}

# ------------------------------------------
# template expressions (P3)
# ------------------------------------------

# Vue directives whose attribute value is a JavaScript expression (after normalization;
# non-expression directives like v-slot / # are excluded and not parsed)
_EXPRESSION_DIRECTIVES = {
    "v-if", "v-else-if", "v-show", "v-for", "v-model",
    "v-text", "v-html", "v-on", "v-bind",
}
_DIRECTIVE_SHORTHANDS = {"@": "v-on", ":": "v-bind"}

# HTML inline handler attributes (W3C content event attributes, lowercase match)
_INLINE_EVENT_HANDLERS = {
    "onabort", "onauxclick", "onbeforeinput", "onblur", "oncancel",
    "oncanplay", "oncanplaythrough", "onchange", "onclick", "onclose",
    "oncontextmenu", "oncopy", "oncuechange", "oncut", "ondblclick",
    "ondrag", "ondragend", "ondragenter", "ondragleave", "ondragover",
    "ondragstart", "ondrop", "ondurationchange", "onemptied", "onended",
    "onerror", "onfocus", "onformdata", "oninput", "oninvalid",
    "onkeydown", "onkeypress", "onkeyup", "onload", "onloadeddata",
    "onloadedmetadata", "onloadstart", "onmousedown", "onmouseenter",
    "onmouseleave", "onmousemove", "onmouseout", "onmouseover", "onmouseup",
    "onpaste", "onpause", "onplay", "onplaying", "onpointercancel",
    "onpointerdown", "onpointerenter", "onpointerleave", "onpointermove",
    "onpointerout", "onpointerover", "onpointerup", "onprogress",
    "onratechange", "onreset", "onresize", "onscroll", "onsearch",
    "onseeked", "onseeking", "onselect", "onselectstart", "onstalled",
    "onsubmit", "onsuspend", "ontimeupdate", "ontoggle", "onvolumechange",
    "onwaiting", "onwheel",
}

_TEMPLATE_EXPRESSION_NAME_LIMIT = 120

# Shared stateless fault-tolerant backend — same collection contract as the per-language parsers
_shared_backend = TreeSitterCodeBackend()


def attribute_value(start_tag: Any, source: bytes, name: str) -> Optional[str]:
    """Read a start_tag attribute value (case-insensitive).

    Returns:
        str  — the attribute value (quotes stripped)
        ""   — a boolean attribute (e.g. vue's ``<script setup>``)
        None — attribute absent
    """
    if start_tag is None:
        return None
    for child in start_tag.children:
        if child.type != "attribute":
            continue
        name_node = None
        value_node = None
        for part in child.children:
            if part.type == "attribute_name":
                name_node = part
            elif part.type == "attribute_value":
                value_node = part
            elif part.type == "quoted_attribute_value":
                for quoted in part.children:
                    if quoted.type == "attribute_value":
                        value_node = quoted
        if name_node is None:
            continue
        attr_name = source[name_node.start_byte:name_node.end_byte]
        if attr_name.decode("utf-8", errors="replace").strip().lower() != name.lower():
            continue
        if value_node is None:
            return ""
        raw = source[value_node.start_byte:value_node.end_byte]
        return raw.decode("utf-8", errors="replace").strip().strip("'\"")
    return None


def script_language(start_tag: Any, source: bytes) -> Optional[str]:
    """Decide the target tree-sitter language for a script block.

    Returns:
        "javascript" | "typescript" — the embedded language to parse
        None — a non-executable block (template/data/unknown language); the caller should skip
    """
    lang = attribute_value(start_tag, source, "lang")
    if lang is not None:
        value = lang.strip().lower()
        if value in _TS_LANG_VALUES:
            return "typescript"
        if value in _JS_LANG_VALUES:
            return "javascript"
        return None
    type_attr = attribute_value(start_tag, source, "type")
    if type_attr is None:
        return "javascript"
    value = type_attr.strip().lower()
    if value in _TS_TYPE_VALUES:
        return "typescript"
    if value in _JS_TYPE_VALUES:
        return "javascript"
    return None


def style_language(start_tag: Any, source: bytes) -> Optional[str]:
    """Decide the target tree-sitter language for a style block.

    The runtime currently ships only css/scss grammars; ``lang=less`` and ``lang=sass``
    are explicitly recognized and return None — the caller keeps the block and records
    "grammar unavailable" instead of misreading dialect text as CSS or fabricating structure.
    """
    lang = attribute_value(start_tag, source, "lang")
    if lang is None:
        return "css"
    value = lang.strip().lower()
    if value in ("", "css"):
        return "css"
    if value in _SCSS_LANG_VALUES:
        return "scss"
    if value in _UNAVAILABLE_STYLE_LANGUAGES:
        return None
    return None


def _raw_text_child(node: Any) -> Optional[Any]:
    for child in node.children:
        if child.type == "raw_text":
            return child
    return None


def _start_tag_child(node: Any) -> Optional[Any]:
    for child in node.children:
        if child.type == "start_tag":
            return child
    return None


def _anchor_point(source: bytes, byte_offset: int) -> Tuple[int, int]:
    """Byte offset in the host source → (0-based row, col)."""
    prefix = source[:byte_offset]
    row = prefix.count(b"\n")
    col = byte_offset - (prefix.rfind(b"\n") + 1)
    return row, col


def _translate_span(
    span: SourceSpan,
    byte_offset: int,
    anchor_row: int,
    anchor_col: int,
) -> SourceSpan:
    """Shift embedded-parse spans into the host source coordinate system."""

    def line_col(line_1based: int, col: int) -> Tuple[int, int]:
        """Shift an embedded-source (row, col) into the host coordinate system."""
        if line_1based <= 1:
            return anchor_row + 1, col + anchor_col
        return anchor_row + line_1based, col

    line_start, col_start = line_col(span.line_start, span.col_start)
    line_end, col_end = line_col(span.line_end, span.col_end)
    return SourceSpan(
        line_start=line_start,
        line_end=line_end,
        col_start=col_start,
        col_end=col_end,
        byte_start=span.byte_start + byte_offset,
        byte_end=span.byte_end + byte_offset,
    )


def parse_embedded_block(
    host: CodeStructure,
    node: Any,
    source: bytes,
    language: str,
    context: str,
) -> bool:
    """Parse an element's raw_text and merge the structure into the host CodeStructure.

    Args:
        host: the host (HTML/Vue) CodeStructure, merged in place
        node: the script_element / style_element node
        source: the host file's UTF-8 byte stream
        language: target tree-sitter language name
        context: diagnostic message prefix, e.g. ``<script lang="ts">``

    Returns:
        bool — whether an embedded parse actually ran (empty content returns False)
    """
    raw = _raw_text_child(node)
    if raw is None:
        return False
    content = source[raw.start_byte:raw.end_byte]
    if not content.strip():
        return False

    inner = _shared_backend.parse(
        content.decode("utf-8", errors="replace"), language
    ).structure

    byte_offset = raw.start_byte
    anchor = _anchor_point(source, byte_offset)

    _merge_inner_structure(
        host,
        inner,
        byte_offset,
        anchor,
        context=context,
        language=language,
        count_key=f"embedded_{language}_blocks",
    )
    return True


def _merge_inner_structure(
    host: CodeStructure,
    inner: CodeStructure,
    byte_offset: int,
    anchor: Tuple[int, int],
    context: str,
    language: str,
    count_key: Optional[str] = None,
) -> None:
    """Merge an embedded-language parse result into the host CodeStructure by byte/row-col offsets.

    Args:
        host: the host CodeStructure, merged in place
        inner: the parse result for the embedded text (coordinates are the embedded text's own)
        byte_offset: start byte offset of the embedded text in the host source
        anchor: (0-based row, col) of byte_offset in the host source
        context: diagnostic message prefix, e.g. ``<script lang="ts">``
        language: embedded tree-sitter language name (written to the unit detail)
        count_key: stats key for counting (None skips counting)
    """
    anchor_row, anchor_col = anchor

    # Dedup key for entries the host already collected (some grammar versions
    # emit embedded symbols directly via injection; the shifted byte range then overlaps)
    seen_diag = {
        (d.code, d.span.byte_start, d.span.byte_end)
        for d in host.diagnostics if d.span
    }
    seen_comment = {
        (c.span.byte_start, c.span.byte_end) for c in host.comments
    }
    seen_symbol = {
        (s.kind, s.name, s.span.byte_start, s.span.byte_end)
        for s in host.symbols
    }
    seen_unit = {
        (u.kind, u.span.byte_start, u.span.byte_end)
        for u in host.structural_units
    }

    for diagnostic in inner.diagnostics:
        span = (
            _translate_span(diagnostic.span, byte_offset, anchor_row, anchor_col)
            if diagnostic.span else None
        )
        key = (diagnostic.code, span.byte_start, span.byte_end) if span else None
        if key is not None and key in seen_diag:
            continue
        if key is not None:
            seen_diag.add(key)
        host.diagnostics.append(
            CodeDiagnostic(
                code=diagnostic.code,
                message=f"{context}: {diagnostic.message}",
                severity=diagnostic.severity,
                span=span,
                snippet=diagnostic.snippet,
            )
        )

    for comment in inner.comments:
        span = _translate_span(comment.span, byte_offset, anchor_row, anchor_col)
        comment_key = (span.byte_start, span.byte_end)
        if comment_key in seen_comment:
            continue
        seen_comment.add(comment_key)
        host.comments.append(
            CommentUnit(kind=comment.kind, text=comment.text, span=span)
        )

    for symbol in inner.symbols:
        span = _translate_span(symbol.span, byte_offset, anchor_row, anchor_col)
        symbol_key = (symbol.kind, symbol.name, span.byte_start, span.byte_end)
        if symbol_key in seen_symbol:
            continue
        seen_symbol.add(symbol_key)
        host.symbols.append(
            SymbolUnit(
                name=symbol.name,
                kind=symbol.kind,
                span=span,
                parent=symbol.parent,
            )
        )

    for unit in inner.structural_units:
        span = _translate_span(unit.span, byte_offset, anchor_row, anchor_col)
        unit_key = (unit.kind, span.byte_start, span.byte_end)
        if unit_key in seen_unit:
            continue
        seen_unit.add(unit_key)
        detail: Optional[Dict[str, Any]] = dict(unit.detail) if unit.detail else None
        if detail is None:
            detail = {}
        detail["embedded_language"] = language
        host.structural_units.append(
            StructuralUnit(
                kind=unit.kind,
                name=unit.name,
                span=span,
                detail=detail,
            )
        )

    # stats: merge embedded-block counts + node-traversal volume (to_metadata derives
    # *_count from list lengths automatically, no manual accumulation needed)
    if count_key:
        host.stats[count_key] = host.stats.get(count_key, 0) + 1
    visited = inner.stats.get("visited_node_count")
    if isinstance(visited, int):
        host.stats["visited_node_count"] = (
            host.stats.get("visited_node_count", 0) + visited
        )

    if inner.diagnostics and host.status == ParseStatus.OK.value:
        host.status = ParseStatus.RECOVERED.value


def parse_template_expressions(
    host: CodeStructure,
    root: Any,
    source: bytes,
    host_language: str,
) -> int:
    """Parse Vue templates and HTML inline handlers at the expression level.

    Only nodes explicitly exposed by the grammar tree are handled — no free-text scanning:
    Vue's ``interpolation`` and ``directive_attribute``, plus standard HTML/Vue event
    attributes (``onclick`` etc.). Every executable value first leaves ``template_expression``
    structural evidence, then goes to the JavaScript backend to extract call/reference facts.

    The left-hand side of ``v-for`` is a Vue template binding, not a standalone JavaScript
    expression; only the executable ``in`` / ``of`` right-hand side is parsed, avoiding
    bogus JS diagnostics for ``item in items``.
    """
    stack = [root]
    seen_ranges: set[Tuple[int, int]] = set()
    count = 0
    while stack:
        node = stack.pop()
        expression_range: Optional[Tuple[int, int]] = None
        context = ""

        if host_language == "vue" and node.type == "interpolation":
            raw = _raw_text_child(node)
            if raw is not None:
                expression_range = (raw.start_byte, raw.end_byte)
                context = "vue_interpolation"
        elif host_language == "vue" and node.type == "directive_attribute":
            directive = _directive_name(node, source)
            value = _attribute_value_child(node)
            if directive in _EXPRESSION_DIRECTIVES and value is not None:
                start, end = value.start_byte, value.end_byte
                if directive == "v-for":
                    start, end = _v_for_iterable_range(source, start, end)
                expression_range = (start, end)
                context = f"vue_directive:{directive}"
        elif node.type == "attribute":
            name_node = _direct_child(node, "attribute_name")
            value = _attribute_value_child(node)
            if name_node is not None and value is not None:
                attr_name = _node_text(source, name_node).lower()
                if attr_name in _INLINE_EVENT_HANDLERS:
                    expression_range = (value.start_byte, value.end_byte)
                    context = f"html_inline_handler:{attr_name}"

        if expression_range is not None and expression_range not in seen_ranges:
            seen_ranges.add(expression_range)
            if _parse_template_expression(
                host,
                source,
                expression_range[0],
                expression_range[1],
                context,
            ):
                count += 1
        stack.extend(reversed(node.children))

    if count:
        host.stats["template_expression_count"] = (
            host.stats.get("template_expression_count", 0) + count
        )
    return count


def _parse_template_expression(
    host: CodeStructure,
    source: bytes,
    start_byte: int,
    end_byte: int,
    context: str,
) -> bool:
    """Parse a single template expression and merge its JS AST structural facts."""
    raw = source[start_byte:end_byte]
    content = raw.strip()
    if not content:
        return False
    leading = raw.find(content)
    expression_start = start_byte + leading
    expression_end = expression_start + len(content)
    expression = content.decode("utf-8", errors="replace")
    name = expression
    if len(name) > _TEMPLATE_EXPRESSION_NAME_LIMIT:
        name = name[: _TEMPLATE_EXPRESSION_NAME_LIMIT - 1] + "…"

    span = _span_for_range(source, expression_start, expression_end)
    host.structural_units.append(
        StructuralUnit(
            kind="template_expression",
            name=name,
            span=span,
            detail={
                "context": context,
                "embedded_language": "javascript",
            },
        )
    )
    inner = _shared_backend.parse(expression, "javascript").structure
    _merge_inner_structure(
        host,
        inner,
        expression_start,
        _anchor_point(source, expression_start),
        context=f"<{context}>",
        language="javascript",
        count_key="template_javascript_expressions",
    )
    return True


def _direct_child(node: Any, child_type: str) -> Optional[Any]:
    for child in node.children:
        if child.type == child_type:
            return child
    return None


def _attribute_value_child(node: Any) -> Optional[Any]:
    """Get the actual value node of an attribute/directive_attribute (quotes excluded)."""
    direct = _direct_child(node, "attribute_value")
    if direct is not None:
        return direct
    quoted = _direct_child(node, "quoted_attribute_value")
    return _direct_child(quoted, "attribute_value") if quoted is not None else None


def _directive_name(node: Any, source: bytes) -> str:
    """Normalize a Vue directive node to its main directive name (v-if / v-bind / v-on etc.)."""
    name_node = _direct_child(node, "directive_name")
    if name_node is not None:
        raw = _node_text(source, name_node).lower()
        if raw in _DIRECTIVE_SHORTHANDS:
            return _DIRECTIVE_SHORTHANDS[raw]
        return raw.split(":", 1)[0]
    # Shorthand syntax (@click / :key) has no directive_name child: the grammar tree
    # splits the shorthand symbol into an anonymous node ("@" or ":") + directive_value.
    for child in node.children:
        if child.type in _DIRECTIVE_SHORTHANDS:
            return _DIRECTIVE_SHORTHANDS[child.type]
    return ""


def _v_for_iterable_range(
    source: bytes,
    start_byte: int,
    end_byte: int,
) -> Tuple[int, int]:
    """Return the executable expression range of the right-hand side of a v-for ``alias in/of iterable``."""
    raw = source[start_byte:end_byte]
    for separator in (b" in ", b" of "):
        offset = raw.find(separator)
        if offset >= 0:
            return start_byte + offset + len(separator), end_byte
    # Malformed/nonstandard directives conservatively hand the full text to JS; diagnostics stay auditable.
    return start_byte, end_byte


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _span_for_range(source: bytes, start_byte: int, end_byte: int) -> SourceSpan:
    start_row, start_col = _anchor_point(source, start_byte)
    end_row, end_col = _anchor_point(source, end_byte)
    return SourceSpan(
        line_start=start_row + 1,
        line_end=end_row + 1,
        col_start=start_col,
        col_end=end_col,
        byte_start=start_byte,
        byte_end=end_byte,
    )


def element_block_info(
    node: Any,
    source: bytes,
    is_script: bool,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Return (embedded language name or None, structural-unit detail dict)."""
    start_tag = _start_tag_child(node)
    language = (
        script_language(start_tag, source)
        if is_script else style_language(start_tag, source)
    )
    detail: Dict[str, Any] = {}
    if language is not None:
        detail["language"] = language
    else:
        detail["language"] = None
        requested = attribute_value(start_tag, source, "lang")
        requested = requested.strip().lower() if requested is not None else ""
        if not is_script and requested in _UNAVAILABLE_STYLE_LANGUAGES:
            detail["skipped"] = f"embedded grammar unavailable: {requested}"
        else:
            detail["skipped"] = "unsupported embedded language"
    return language, detail
