# -*- coding: utf-8 -*-
"""JSON5 config parser (standalone profile).

Deliberately NOT a "JSONC parser + lax switch": .json → strict json,
.jsonc → jsonc, .json5 → json5 — three separate formats, each
extension capability explicitly declared.

Capability surface:
- object / array / property + identifier keys (ECMAScript
  IdentifierName approximation: start with $/_/Unicode letter,
  continue with $/_/letter/digit/ZWNJ/ZWJ; reserved words allowed
  as keys)
- single/double-quoted strings (JSON5 escape set ``' " \\ b f n r
  t v 0 xXX uXXXX``; surrogate pair merging; a raw newline means
  unterminated)
- four-way comment binding (leading/inline/trailing/orphan, per
  the json family convention)
- trailing commas (natively tolerated in objects/arrays, recorded
  honestly, never stripped)
- basic numbers + hex (0xFF → 255, value_type=hex_integer, raw
  lexeme preserved); leading zeros accepted + disclosed per entry
  (not blindly following JavaScript runtime behavior)
- booleans and null; source mapping (0-based line/col, family
  convention)
- duplicate keys: judged on decoded key names (identifier/single-
  quoted/double-quoted/escaped keys with the same name group
  together); all occurrences preserved in override chains (policy
  disclosed via contract["duplicate_key_policy"], never silent)
- raw lexemes preserved: raw_key / key_style (identifier |
  single_quoted | double_quoted | escaped_key) / quote_style /
  raw_value / number lexeme
- safety resource limits: depth/nodes/key count/array length/
  string length (exceeding → honest degradation with
  nesting_too_deep / unsupported disclosure, no crash, nothing
  fabricated)
- zero-execution red line: templates/$ref registered but never
  expanded, no environment reads, no external files, no code
  execution; prototype-pollution keys (__proto__ /
  constructor.prototype) disclosed, not blocked

Lax lexicon (supported + disclosed):
- ±Infinity / NaN: syntactically valid (json5_valid) but not JSON-
  outputable (json_compatible:false + serialization_policy
  explicitly null_with_diagnostic — never silently coerced to
  null)
- +sign numbers / leading dot ``.5`` / trailing dot ``5.`` (raw
  lexemes preserved)
- backslash line continuation: physical newline stripped, logical
  content preserved + line_continuation flag; a bare newline is
  still unterminated
- -0 sign information preserved (value_type=negative_zero +
  sign_preserved)
- Unicode identifier visual-confusion disclosure: mixed scripts
  (Latin/Cyrillic/Greek within one key) and zero-width chars →
  security warning reason (disclosed, not blocked)

Still an immediate error on encounter: JavaScript bare words
(undefined/function) — JSON5 is not JavaScript; never silently
accepted as ordinary lexing.

Family-layer reuse:
- domain schema: domain_hints weak declaration (kubernetes/
  compose/ci, empty hits omit the key) + schema tri-state (family
  layer cc.apply_schema_status: declared without a profile →
  absent + unverifiable reason; structural expectations →
  satisfied/violated)
- config merging: parse_many_merged multi-file overlay merge
  (later shadows earlier, core converges in the family layer
  cc.merge_contracts; cross-file schema/hints propagation + key
  re-runs + structured_data deep-merge)
- JSON5 vs JSON diff: success-path lexical-difference summary
  reason (json5_vs_json_diff — the success-path dual of the
  failure path's json5_extension_evidence; pure JSON subsets
  don't trigger it)

Honest non-goals (no parser-side fact source, nothing fabricated):
format-faithful rewriting / normalized output / version differences
(a rewriter is not parse evidence; omitted under the evidence-only
red line).

Family helper reuse (single source of truth): $ref/JSON Pointer
detection and JSON5 feature scanning come from json_parser (shared
within the family, zero second implementations).
"""

import logging
import math
import re
import time
import unicodedata

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)

# Family single-source-of-truth reuse (json-family private helpers
# shared across modules in this package)
from provendoc.core.parsers.config.json_parser import (
    _attach_path_display,
    _attach_sensitive_disclosure,
    _comment_text,
    _description_from,
    _detect_json5_extensions,
    _detect_json_pointer_ref,
    _finalize_p1_addenda,
    _value_type_of,
)

_log = logging.getLogger(__name__)

# ------------------------------------------------------------
# Resource limits (json-family convention; depth is an explicit
# guard, not a RecursionError backstop)
# ------------------------------------------------------------
_MAX_DEPTH = 512
_MAX_NODES = 500_000
_MAX_STRING_LENGTH = 1_000_000
_MAX_ARRAY_LENGTH = 1_000_000
_MAX_OBJECT_KEYS = 100_000

_JSON5_ESCAPES = {
    "'": "'", '"': '"', "\\": "\\",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "0": "\0",
}
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_KEYWORD_VALUES = {"true": True, "false": False, "null": None}
# bare-word special numbers in value position (syntactically
# valid + JSON-incompatible projection)
_SPECIAL_WORDS = {"Infinity": math.inf, "NaN": math.nan}
_JS_WORDS = {
    "undefined": "javascript_syntax",
    "function": "javascript_syntax",
}
# mixed-script visual-confusion domain (Latin/Cyrillic/Greek —
# the high-risk homoglyph trio; other scripts like Han mixed with
# Latin are common legitimate shapes and excluded — avoid false
# positives)
_CONFUSABLE_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")
# zero-width chars (ZWNJ/ZWJ are legal ID_Continue but visually
# invisible — disclosed)
_ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\u2060\ufeff")
# template shapes (registered, never expanded — default deny)
_TEMPLATE_RE = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}")
# already-supported JSON5 features (the diff baseline for the
# failure-path unsupported disclosure)
_P0_SUPPORTED_FEATURES = {"single_quotes", "bare_keys", "hex_numbers"}
_IDENT_CONT_EXTRA = "\u200c\u200d"  # ZWNJ / ZWJ（ECMAScript ID_Continue）


class _Json5Error(Exception):
    """JSON5 parse error (honest-degradation carrier: line/col/feature disclosure)."""

    def __init__(self, line, col, message, feature=None):
        super().__init__(message)
        self.line = line
        self.col = col
        self.message = message
        self.feature = feature

    def __str__(self):
        return f"line {self.line + 1} col {self.col}: {self.message}"


class _Tok:
    """Lexical token (0-based line/col, raw source preserved)."""

    __slots__ = ("kind", "value", "raw", "line", "col",
                 "end_line", "end_col", "quote", "has_escape",
                 "leading_zero", "is_hex", "is_special",
                 "negative_zero", "has_continuation")

    def __init__(self, kind, value, raw, line, col, end_line, end_col,
                 quote=None, has_escape=False, leading_zero=False,
                 is_hex=False, is_special=False, negative_zero=False,
                 has_continuation=False):
        self.kind = kind
        self.value = value
        self.raw = raw
        self.line = line
        self.col = col
        self.end_line = end_line
        self.end_col = end_col
        self.quote = quote          # 字符串 token：引号字符
        self.has_escape = has_escape
        self.leading_zero = leading_zero
        self.is_hex = is_hex
        self.is_special = is_special          # ±Infinity / NaN
        self.negative_zero = negative_zero     # -0 符号保全
        self.has_continuation = has_continuation  # 反斜杠续行


def _pos_after(sl, sc, seg):
    """(line, col) after consuming seg (0-based; family convention)."""
    lines = seg.count("\n")
    if lines:
        return sl + lines, len(seg) - seg.rfind("\n") - 1
    return sl, sc + len(seg)


def _ident_start(ch):
    return ch == "$" or ch == "_" or ch.isalpha()


def _ident_cont(ch):
    return _ident_start(ch) or ch.isdigit() or ch in _IDENT_CONT_EXTRA


def _lex_string(text, i, n, sl, sc, quote):
    """JSON5 string lexing (escape set + surrogate pair merging +
    continuation).

    Backslash line continuation: the physical newline sequence
    (``\\`` + ``\n`` / ``\r\n`` / ``\r`` / ``\u2028`` /
    ``\u2029``) is stripped whole, so logical content contains no
    physical newline; the continuation flag is disclosed by the
    caller. A bare newline (no backslash) is still unterminated —
    not all cross-line strings are legal multiline text. ``\0``
    followed by a digit still errors honestly. Returns (value,
    end, has_escape, has_continuation).
    """
    i += 1  # 跳过开引号
    buf = []
    has_escape = False
    has_continuation = False
    while i < n:
        c = text[i]
        if c == quote:
            return ''.join(buf), i + 1, has_escape, has_continuation
        if c == "\\" and i + 1 < n:
            e = text[i + 1]
            has_escape = True
            if e in "\n\r\u2028\u2029":
                # backslash continuation: the physical newline sequence
                # is stripped whole (\r\n counts as one)
                has_continuation = True
                i += 2
                if e == "\r" and i < n and text[i] == "\n":
                    i += 1
                continue
            i += 2
            if e in _JSON5_ESCAPES:
                # \0 followed by a digit diverges across
                # implementations — error honestly, don't apply
                # JS/C/Python escape rules
                if e == "0" and i < n and text[i].isdigit():
                    raise _Json5Error(
                        sl, sc,
                        r"invalid \0 escape followed by digit",
                        feature="null_escape_followed_by_digit",
                    )
                buf.append(_JSON5_ESCAPES[e])
            elif e == "x":
                if i + 2 > n or any(
                    text[i + k] not in _HEX_DIGITS
                    for k in range(min(2, n - i))
                ):
                    raise _Json5Error(
                        sl, sc,
                        r"invalid \x escape (needs 2 hex digits)",
                        feature="invalid_escape",
                    )
                buf.append(chr(int(text[i:i + 2], 16)))
                i += 2
            elif e == "u":
                if i + 4 > n or any(
                    text[i + k] not in _HEX_DIGITS
                    for k in range(min(4, n - i))
                ):
                    raise _Json5Error(
                        sl, sc,
                        r"invalid \u escape (needs 4 hex digits)",
                        feature="invalid_escape",
                    )
                cp = int(text[i:i + 4], 16)
                i += 4
                if (
                    0xD800 <= cp <= 0xDBFF
                    and text[i:i + 2] == "\\u"
                    and i + 6 <= n
                    and all(text[i + 2 + k] in _HEX_DIGITS
                            for k in range(4))
                ):
                    lo = int(text[i + 2:i + 6], 16)
                    if 0xDC00 <= lo <= 0xDFFF:
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                        i += 6
                buf.append(chr(cp))
            else:
                raise _Json5Error(
                    sl, sc, f"invalid escape \\{e}",
                    feature="invalid_escape",
                )
        elif c in "\n\r\u2028\u2029":
            raise _Json5Error(
                sl, sc, "unterminated string (raw line terminator)",
                feature="unterminated_string",
            )
        else:
            buf.append(c)
            i += 1
    raise _Json5Error(sl, sc, "unterminated string",
                      feature="unterminated_string")


def _lex_number(text, i, n, sl, sc):
    """JSON5 lax number lexing: all decimal shapes + hex.

    +sign / leading dot ``.5`` / trailing dot ``5.`` / leading
    zero; -0 sign information preserved via the negative_zero flag
    (not normalized away). Returns (value, end, leading_zero,
    is_hex, negative_zero).
    """
    start = i
    leading_zero = False
    is_hex = False
    negative_zero = False
    j = i
    signed = text[j] in "+-"
    if signed:
        j += 1
    # hex (0x/0X prefix)
    if text[j:j + 1] == "0" and text[j + 1:j + 2] in "xX":
        k = j + 2
        if k >= n or text[k] not in _HEX_DIGITS:
            raise _Json5Error(sl, sc, "invalid hex number",
                              feature="invalid_number")
        while k < n and text[k] in _HEX_DIGITS:
            k += 1
        raw = text[start:k]
        return int(raw, 16), k, False, True, False
    is_float = False
    if j >= n or (text[j] not in "." and not text[j].isdigit()):
        raise _Json5Error(sl, sc, "invalid number", feature="invalid_number")
    if text[j] == ".":
        # leading dot: .5 — a digit must immediately follow the dot
        if j + 1 >= n or not text[j + 1].isdigit():
            raise _Json5Error(sl, sc, "invalid number (leading dot)",
                              feature="invalid_number")
        is_float = True
        j += 1
        while j < n and text[j].isdigit():
            j += 1
    else:
        if text[j] == "0" and j + 1 < n and text[j + 1].isdigit():
            # JSON5 allows leading zeros (accepted but disclosed
            # per entry, never silent)
            leading_zero = True
        while j < n and text[j].isdigit():
            j += 1
        if j < n and text[j] == ".":
            # dot after the integer part: trailing dot (5. is
            # legal) or a normal fraction
            is_float = True
            j += 1
            while j < n and text[j].isdigit():
                j += 1
    if j < n and text[j] in "eE":
        j += 1
        if j < n and text[j] in "+-":
            j += 1
        if j >= n or not text[j].isdigit():
            raise _Json5Error(sl, sc, "invalid number (exponent)",
                              feature="invalid_number")
        is_float = True
        while j < n and text[j].isdigit():
            j += 1
    raw = text[start:j]
    value = float(raw) if is_float else int(raw)
    if raw.startswith("-") and value == 0 and not isinstance(value, bool):
        negative_zero = True
    return value, j, leading_zero, is_hex, negative_zero


def _lex_json5(text):
    """JSON5 lexer (comments become tokens, never dropped).

    Returns:
        (toks, comments): lexical errors raise _Json5Error directly.
    """
    toks = []
    comments = []
    i, n = 0, len(text)
    line, col = 0, 0
    while i < n:
        ch = text[i]
        si, sl, sc = i, line, col
        if ch in " \t\r\n\u2028\u2029":
            if ch in "\n\u2028\u2029":
                line += 1
                col = 0
            else:
                col += 1
            i += 1
            continue
        if ch == "/" and text[i + 1:i + 2] == "/":
            i += 2
            while i < n and text[i] not in "\r\n\u2028\u2029":
                i += 1
            raw = text[si:i]
            el, ec = _pos_after(sl, sc, raw)
            tok = _Tok("comment", _comment_text(raw), raw, sl, sc, el, ec)
            toks.append(tok)
            comments.append(tok)
            line, col = el, ec
            continue
        if ch == "/" and text[i + 1:i + 2] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            if i + 1 >= n:
                raise _Json5Error(
                    sl, sc, "unterminated block comment",
                    feature="unterminated_comment",
                )
            i += 2
            raw = text[si:i]
            el, ec = _pos_after(sl, sc, raw)
            tok = _Tok("comment", _comment_text(raw), raw, sl, sc, el, ec)
            toks.append(tok)
            comments.append(tok)
            line, col = el, ec
            continue
        if ch == "'" or ch == '"':
            value, end, has_escape, has_cont = _lex_string(
                text, i, n, sl, sc, ch,
            )
            raw = text[si:end]
            if len(raw) > _MAX_STRING_LENGTH:
                raise _Json5Error(
                    sl, sc, "string length limit exceeded",
                    feature="string_length_limit",
                )
            el, ec = _pos_after(sl, sc, raw)
            toks.append(_Tok(
                "string", value, raw, sl, sc, el, ec,
                quote=ch, has_escape=has_escape,
                has_continuation=has_cont,
            ))
            line, col = el, ec
            i = end
            continue
        if ch in "{}[],:":
            i += 1
            toks.append(_Tok(ch, ch, ch, sl, sc, sl, sc + 1))
            col += 1
            continue
        # ±Infinity (signed special numbers — syntactically valid
        # + projection disclosed)
        if (
            ch in "+-"
            and text[i + 1:i + 9] == "Infinity"
        ):
            raw = text[si:si + 9]
            value = -math.inf if ch == "-" else math.inf
            toks.append(_Tok(
                "number", value, raw, sl, sc, sl, sc + 9,
                is_special=True,
            ))
            col += 9
            i += 9
            continue
        if ch == "+" or ch == "." or ch == "-" or ch.isdigit():
            # lax numbers: +sign / leading dot / trailing dot /
            # leading zero / hex
            value, j, lz, is_hex, nz = _lex_number(text, i, n, sl, sc)
            raw = text[i:j]
            el, ec = _pos_after(sl, sc, raw)
            toks.append(_Tok(
                "number", value, raw, sl, sc, el, ec,
                leading_zero=lz, is_hex=is_hex, negative_zero=nz,
            ))
            line, col = el, ec
            i = j
            continue
        if _ident_start(ch):
            j = i
            while j < n and _ident_cont(text[j]):
                j += 1
            raw = text[si:j]
            el, ec = _pos_after(sl, sc, raw)
            if raw in _SPECIAL_WORDS:
                # bare-word special numbers (Infinity/NaN)
                toks.append(_Tok(
                    "number", _SPECIAL_WORDS[raw], raw, sl, sc, el, ec,
                    is_special=True,
                ))
            else:
                toks.append(_Tok("word", raw, raw, sl, sc, el, ec))
            line, col = el, ec
            i = j
            continue
        raise _Json5Error(sl, sc, f"unexpected character {ch!r}",
                          feature="unexpected_character")
    return toks, comments


class _Node:
    """Syntax node (object/array/scalar; positions and raw preserved)."""

    __slots__ = ("kind", "value", "raw", "line", "col",
                 "end_line", "end_col", "pairs", "items", "trailing",
                 "quote", "has_escape", "leading_zero", "is_hex",
                 "trailing_commas", "is_special", "negative_zero",
                 "has_continuation")

    def __init__(self, kind, value=None, raw="", line=0, col=0,
                 end_line=0, end_col=0):
        self.kind = kind
        self.value = value
        self.raw = raw
        self.line = line
        self.col = col
        self.end_line = end_line
        self.end_col = end_col
        self.pairs = []      # object: [(ktok, vnode, leading, inline)]
        self.items = []      # array: [(vnode, leading, inline)]
        self.trailing = []   # 容器闭界符前的尾注释 tokens
        self.quote = None
        self.has_escape = False
        self.leading_zero = False
        self.is_hex = False
        self.is_special = False        # ±Infinity / NaN（P1）
        self.negative_zero = False     # -0 符号保全（P1）
        self.has_continuation = False  # 反斜杠续行（P1）
        self.trailing_commas = 0


class _Json5DocParser:
    """Recursive-descent parsing (token stream → node tree + out-of-root orphan comments)."""

    def __init__(self, toks):
        self.toks = toks
        self.pos = 0
        self.nodes = 0
        self.orphans = []

    def _peek(self):
        if self.pos < len(self.toks):
            return self.toks[self.pos]
        return None

    def _next(self):
        tok = self._peek()
        if tok is not None:
            self.pos += 1
        return tok

    def _skip_comments(self):
        """Collect consecutive comment tokens from the current position (the caller owns the binding)."""
        collected = []
        while True:
            tok = self._peek()
            if tok is not None and tok.kind == "comment":
                collected.append(tok)
                self.pos += 1
            else:
                return collected
    def parse_sequence(self):
        """Root sequence splitting (≥2 roots → json5_sequence).

        Out-of-root comments (before the first root / between
        roots / after the last) are collected as orphans
        (sequence-level, no bound object — json-family convention).
        """
        roots = []
        while True:
            leading = self._skip_comments()
            if self._peek() is None:
                self.orphans.extend(leading)
                break
            # comment outside any root value → orphan (not a container binding)
            self.orphans.extend(leading)
            roots.append(self.parse_value())
        if not roots:
            raise _Json5Error(0, 0, "empty document",
                              feature="empty_document")
        return roots

    def parse_value(self, depth=0):
        """Value: object / array / string / number / keyword bare word."""
        if depth > _MAX_DEPTH:
            raise _Json5Error(0, 0, "nesting too deep",
                              feature="depth_limit")
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            raise _Json5Error(0, 0, "node limit exceeded",
                              feature="node_limit")
        tok = self._next()
        if tok is None:
            raise _Json5Error(0, 0, "unexpected end of input",
                              feature="unexpected_eoi")
        if tok.kind == "{":
            return self._parse_object(tok, depth)
        if tok.kind == "[":
            return self._parse_array(tok, depth)
        if tok.kind == "string":
            node = _Node("scalar", tok.value, tok.raw,
                         tok.line, tok.col, tok.end_line, tok.end_col)
            node.quote = tok.quote
            node.has_escape = tok.has_escape
            node.has_continuation = tok.has_continuation
            return node
        if tok.kind == "number":
            node = _Node("scalar", tok.value, tok.raw,
                         tok.line, tok.col, tok.end_line, tok.end_col)
            node.leading_zero = tok.leading_zero
            node.is_hex = tok.is_hex
            node.is_special = tok.is_special
            node.negative_zero = tok.negative_zero
            return node
        if tok.kind == "word":
            if tok.value in _KEYWORD_VALUES:
                return _Node("scalar", _KEYWORD_VALUES[tok.value], tok.raw,
                             tok.line, tok.col, tok.end_line, tok.end_col)
            if tok.value in _JS_WORDS:
                raise _Json5Error(
                    tok.line, tok.col,
                    f"JavaScript syntax {tok.value!r} is not JSON5 "
                    "(spec card 3 §1); not executed",
                    feature=_JS_WORDS[tok.value],
                )
            raise _Json5Error(
                tok.line, tok.col,
                f"invalid bare word {tok.value!r} in value position",
                feature="invalid_bare_word",
            )
        raise _Json5Error(tok.line, tok.col,
                          f"unexpected token {tok.raw!r}",
                          feature="unexpected_token")

    def _parse_object(self, open_tok, depth):
        node = _Node("object", raw=open_tok.raw,
                     line=open_tok.line, col=open_tok.col,
                     end_line=open_tok.line, end_col=open_tok.col)
        leading = []
        after_value = False
        just_had_comma = False
        while True:
            tok = self._peek()
            if tok is None:
                raise _Json5Error(
                    open_tok.line, open_tok.col, "unterminated object",
                    feature="unterminated_object",
                )
            if tok.kind == "comment":
                self.pos += 1
                if (
                    (after_value or just_had_comma)
                    and node.pairs
                    and tok.line == node.pairs[-1][1].end_line
                ):
                    # Same-line comment after the value/comma →
                    # inline on the previous pair (json/yaml family
                    # convention, zero drift)
                    kt, v, lead, inl = node.pairs[-1]
                    node.pairs[-1] = (kt, v, lead, inl + [tok])
                else:
                    leading.append(tok)
                continue
            if tok.kind == "}":
                if just_had_comma:
                    node.trailing_commas = 1
                self.pos += 1
                # Leading comments unconsumed at the closer → container trailing (never dropped)
                node.trailing = leading
                node.end_line, node.end_col = tok.end_line, tok.end_col
                return node
            if tok.kind == ",":
                if not after_value:
                    raise _Json5Error(
                        tok.line, tok.col,
                        "unexpected comma in object",
                        feature="unexpected_comma",
                    )
                self.pos += 1
                after_value = False
                just_had_comma = True
                continue
            if after_value:
                raise _Json5Error(
                    tok.line, tok.col,
                    "expected ',' or '}' in object",
                    feature="missing_comma",
                )
            # key: identifier bare word / string
            if tok.kind not in ("word", "string"):
                raise _Json5Error(
                    tok.line, tok.col,
                    f"expected key, got {tok.raw!r}",
                    feature="invalid_key",
                )
            self.pos += 1
            ktok = tok
            colon = self._next()
            if colon is None or colon.kind != ":":
                raise _Json5Error(
                    ktok.line, ktok.col,
                    "expected ':' after key",
                    feature="missing_colon",
                )
            # comment after the colon → this pair's leading (family convention: key description slot)
            while True:
                t = self._peek()
                if t is not None and t.kind == "comment":
                    leading.append(t)
                    self.pos += 1
                else:
                    break
            vnode = self.parse_value(depth + 1)
            node.pairs.append((ktok, vnode, leading, []))
            leading = []
            after_value = True
            just_had_comma = False
            if len(node.pairs) > _MAX_OBJECT_KEYS:
                raise _Json5Error(0, 0, "object key limit exceeded",
                                  feature="key_limit")

    def _parse_array(self, open_tok, depth):
        node = _Node("array", raw=open_tok.raw,
                     line=open_tok.line, col=open_tok.col,
                     end_line=open_tok.line, end_col=open_tok.col)
        leading = []
        after_value = False
        just_had_comma = False
        while True:
            tok = self._peek()
            if tok is None:
                raise _Json5Error(
                    open_tok.line, open_tok.col, "unterminated array",
                    feature="unterminated_array",
                )
            if tok.kind == "comment":
                self.pos += 1
                if (
                    (after_value or just_had_comma)
                    and node.items
                    and tok.line == node.items[-1][0].end_line
                ):
                    # Same-line comment after the value/comma → inline on the previous element (family convention)
                    v, lead, inl = node.items[-1]
                    node.items[-1] = (v, lead, inl + [tok])
                else:
                    leading.append(tok)
                continue
            if tok.kind == "]":
                if just_had_comma:
                    node.trailing_commas = 1
                self.pos += 1
                node.trailing = leading
                node.end_line, node.end_col = tok.end_line, tok.end_col
                return node
            if tok.kind == ",":
                if not after_value:
                    raise _Json5Error(
                        tok.line, tok.col,
                        "unexpected comma in array",
                        feature="unexpected_comma",
                    )
                self.pos += 1
                after_value = False
                just_had_comma = True
                continue
            if after_value:
                raise _Json5Error(
                    tok.line, tok.col,
                    "expected ',' or ']' in array",
                    feature="missing_comma",
                )
            vnode = self.parse_value(depth + 1)
            node.items.append((vnode, leading, []))
            leading = []
            after_value = True
            just_had_comma = False
            if len(node.items) > _MAX_ARRAY_LENGTH:
                raise _Json5Error(0, 0, "array length limit exceeded",
                                  feature="array_limit")


def _key_style_of(ktok):
    """Key style classification (identifier / single-quoted / double-quoted / escaped)."""
    if ktok.kind == "word":
        return "identifier"
    if ktok.has_escape:
        return "escaped_key"
    return "single_quoted" if ktok.quote == "'" else "double_quoted"


def _script_of(ch):
    """Confusion-domain script of a character (Latin/Cyrillic/Greek), else None."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for script in _CONFUSABLE_SCRIPTS:
        if name.startswith(script + " "):
            return script
    return None


def _identifier_confusion_reasons(key):
    """Key visual-confusion warnings (disclosed, not blocked).

    - identifier_script_mix: Latin/Cyrillic/Greek mixed within one
      key (the payPal vs Cyrillic-homoglyph high-risk face;
      legitimate mixes like Han + Latin are excluded)
    - zero_width_identifier: zero-width chars present (native
      zero-width in bare keys is normalized by the base sanitizer,
      but quoted keys can still smuggle them in via \u200b escapes
      — security warning)
    """
    tags = []
    scripts = {_script_of(c) for c in key}
    scripts.discard(None)
    if len(scripts) > 1:
        tags.append("identifier_script_mix")
    if any(c in _ZERO_WIDTH_CHARS for c in key):
        tags.append("zero_width_identifier")
    return tags


def _chain_value_of(node):
    """Override-chain occurrence value (containers by summary, scalars by value — json-family convention)."""
    if node.kind == "object":
        return {"kind": "dict", "child_count": len(node.pairs)}
    if node.kind == "array":
        return {"kind": "list", "child_count": len(node.items)}
    return node.value


def _to_plain(node):
    """Node tree → plain Python value for structured_data
    (duplicate keys last_wins; all occurrences already preserved
    in the contract override chains — policy disclosed, not
    silent; special numbers project to null — serialization_policy
    disclosed alongside, never silent)."""
    if node.kind == "object":
        out = {}
        for ktok, vnode, _l, _i in node.pairs:
            out[ktok.value] = _to_plain(vnode)
        return out
    if node.kind == "array":
        return [_to_plain(v) for v, _l, _i in node.items]
    if node.is_special:
        return None
    return node.value


def _count_trailing_commas(roots):
    """Whole-tree trailing-comma count (aggregated from node trailing_commas flags)."""
    count = 0
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.kind == "object":
            count += node.trailing_commas
            stack.extend(v for _k, v, _l, _i in node.pairs)
        elif node.kind == "array":
            count += node.trailing_commas
            stack.extend(v for v, _l, _i in node.items)
    return count


class Json5Parser(BaseParser):
    """JSON5 config file parser (standalone profile).

    Discipline: evidence-only (positions / raw / lexemes / comments
    / duplicate keys all preserved), zero execution (templates and
    $ref registered only), zero external IO (no environment, file
    or network reads), honest degradation at resource limits, and
    unsupported lax features error on encounter — never silent.
    """

    def __init__(self, encoding="utf-8", tolerant=True):
        """encoding tolerance parameter per family convention (tolerant is diagnostics-only)."""
        self.encoding = encoding
        self.tolerant = tolerant

    # ========================================
    # config entry contract
    # ========================================

    def _build_contract(self, text, roots, parse_error, orphan_toks):
        """Build the contract (full-occurrence duplicate-key override chains + lexeme preservation + zero-execution registration)."""
        contract = cc.build_contract(
            "json5", cc.DUP_LAST_WINS,
            line_numbers=True, comments=True,
        )
        reasons = contract["loss"]["reasons"]

        # out-of-root orphan comments (sequence-level, no bound
        # object — json-family convention)
        for c in orphan_toks or []:
            contract["comments"].append(cc.make_comment(
                "orphan", _comment_text(c.raw), c.raw,
                c.line, c.end_line,
            ))

        if parse_error is not None:
            err = parse_error
            reasons.append(f"contract_parse_failed: {err}")
            if err.feature == "depth_limit":
                # family convention (same as json): honest degradation
                # on depth overrun
                reasons.append("nesting_too_deep")
            elif err.feature:
                reasons.append(
                    f"unsupported_json5_feature:{err.feature} "
                    "(P0 subset; see specs/config_json5_family_spec_v1 "
                    "P1 scope)"
                )
            # lexical feature-diff disclosure (evidence of features
            # outside the supported set)
            feats = _detect_json5_extensions(text) - _P0_SUPPORTED_FEATURES
            if feats:
                reasons.append(
                    "json5_extension_evidence:" + ",".join(sorted(feats))
                )
            return contract

        if len(roots) >= 2:
            # multiple roots are not auto-wrapped into an array
            contract["detected_format"] = "json5_sequence"
            contract["document_count"] = len(roots)
            contract["reason_code"] = "MULTIPLE_ROOT_VALUES"
            reasons.append(
                f"multiple root values: {len(roots)} documents "
                "(json5_sequence); not merged into an array"
            )

        dup_groups = []
        try:
            for doc_index, root in enumerate(roots):
                self._walk_node(root, [], None, contract,
                                doc_index=doc_index, dup_groups=dup_groups)
            cc.resolve_references(contract)
            # JSON5 additive: flat references get the kind identity
            # key backfilled (the family flat surface has no kind —
            # zero drift from the json family; the zero-execution
            # red line requires $ref/pointer identity distinguishable
            # in the references channel)
            _kind_by = {}
            for e in contract["entries"]:
                for r in e.get("references") or []:
                    if r.get("kind"):
                        _kind_by[(e["path_str"], r["syntax"])] = r["kind"]
            for fr in contract["references"]:
                k = _kind_by.get((fr["path_str"], fr["syntax"]))
                if k is not None:
                    fr["kind"] = k
        except RecursionError:
            reasons.append("nesting_too_deep")
        except Exception as e:  # pragma: no cover - 最终兜底
            reasons.append(f"contract_build_failed: {e}")

        # duplicate-key policy disclosure (recorded, not silent)
        if dup_groups:
            contract["duplicate_key_policy"] = (
                "occurrences_preserved_last_effective"
            )
            reasons.append(
                f"duplicate_keys:{len(dup_groups)} group(s) — all "
                "occurrences preserved in override chains; last "
                "occurrence effective (policy disclosed, not silent)"
            )

        # trailing-comma tolerance disclosure (native to JSON5 —
        # nothing stripped or faked)
        tc = _count_trailing_commas(roots)
        if tc:
            reasons.append(
                f"trailing_commas:{tc} tolerated natively (JSON5)"
            )

        # special-number head reason (json_compatible:false disclosed
        # across the board — syntactic validity separated from JSON
        # outputability)
        n_special = sum(
            1 for e in contract["entries"]
            if e.get("json_compatible") is False
        )
        if n_special:
            reasons.append(
                f"special_numbers:{n_special} (json5_valid, "
                "json_compatible:false, projected null_with_diagnostic)"
            )

        # domain_hints weak declaration + schema tri-state (family
        # layer cc.apply_schema_status reuse, isomorphic to json/
        # yaml: k8s/compose by strict top-level keys; CI's image/
        # script usually live below the second level, so the whole
        # tree's keys are used. Weak declaration, empty hits omit
        # the key. Multi-root sequences take the first root as the
        # single fact (the sequence is disclosed in the header). An
        # object root's dict facts live in pairs (node.value is
        # None) — the shallow projection takes only top-level keys
        # and the $schema scalar, not a full-tree rewalk
        root_pairs = None
        if roots and roots[0].kind == "object":
            root_pairs = {}
            for kt, vnode, _l, _i in roots[0].pairs:
                root_pairs.setdefault(kt.value, vnode)
        hints = []
        if root_pairs is not None:
            top_level_keys = {
                p["path_str"] for p in (
                    contract["entries"] + contract["containers"]
                ) if "." not in p["path_str"]
            }
            all_keys = set()
            for p in contract["entries"] + contract["containers"]:
                all_keys.update(p["path_str"].split("."))
            if {"apiVersion", "kind"} <= top_level_keys:
                hints.append("kubernetes")
            if "services" in top_level_keys and "version" in top_level_keys:
                hints.append("compose")
            if (({"jobs", "stages"} & all_keys)
                    and ({"image", "script"} & all_keys)):
                hints.append("ci")
        if hints:
            contract["domain_hints"] = hints
        schema_ref = None
        declared_node = root_pairs.get("$schema") \
            if root_pairs is not None else None
        if (declared_node is not None
                and isinstance(declared_node.value, str)
                and declared_node.value):
            schema_ref = declared_node.value
        cc.apply_schema_status(
            contract, schema_ref, hints,
            contract["entries"], contract["containers"], reasons,
        )

        # JSON5 vs JSON diff disclosure (the success-path dual of
        # the failure path's json5_extension_evidence: with syntax
        # fully accepted, summarize which lexical features are not
        # RFC 8259; value-level differences stay on entries via
        # the json_compatible flag)
        diff_feats = _detect_json5_extensions(text)
        if diff_feats:
            reasons.append(
                "json5_vs_json_diff:" + ",".join(sorted(diff_feats))
                + " (lexical features not valid in RFC 8259; value-level"
                  " detail on entries via json_compatible)"
            )

        # addenda family alignment (isomorphic to json): path_display
        # / sensitive disclosure / style keys
        _finalize_p1_addenda(contract, text, roots)

        return contract

    def _walk_node(self, node, path, key, contract,
                   doc_index=0, dup_groups=None):
        """Walk the node tree (containers / entries / comment bindings / override chains / lexeme preservation)."""
        if node.kind == "object":
            occs = {}
            for ktok, vnode, leading, inline in node.pairs:
                occs.setdefault(ktok.value, []).append(
                    (ktok, vnode, leading, inline)
                )
            has_dup = any(len(v) > 1 for v in occs.values())
            desc, desc_lines = _description_from(node.trailing, [])
            contract["containers"].append(cc.make_container(
                path, "dict",
                child_count=len(occs),
                line_start=node.line, line_end=node.end_line,
                description=desc, description_lines=desc_lines,
                duplicate=has_dup,
                doc_index=doc_index,
            ))
            self._emit_comments(
                node.trailing, path, "trailing", "container",
                contract, doc_index,
            )
            reasons = contract["loss"]["reasons"]
            for k, lst in occs.items():
                ktok, vnode, leading, inline = lst[-1]
                # visual-confusion warnings (disclosed, not blocked;
                # key identity does not distinguish bare vs quoted
                # keys: quoted keys containing zero-width chars after
                # \u200b escape decoding are disclosed too)
                for tag in _identifier_confusion_reasons(k):
                    reasons.append(
                        f"{tag}:" + cc.path_str(path + [k])
                    )
                # prototype-pollution key disclosure (parse + flag,
                # never blocked or rewritten)
                if k == "__proto__":
                    reasons.append(
                        "prototype_pollution_key:" + cc.path_str(path + [k])
                    )
                elif (
                    k == "constructor"
                    and vnode.kind == "object"
                    and any(
                        pk.value == "prototype"
                        for pk, _v, _l, _i in vnode.pairs
                    )
                ):
                    reasons.append(
                        "prototype_pollution_key:" + cc.path_str(path + [k])
                    )
                dup = len(lst) > 1
                if dup and dup_groups is not None:
                    dup_groups.append(k)
                chain = None
                if dup:
                    chain = [
                        {
                            "value": _chain_value_of(v),
                            "line": kt.line,
                            "raw_key": kt.raw,
                            "key_style": _key_style_of(kt),
                        }
                        for kt, v, _l, _i in lst
                    ]
                self._walk_node_value(
                    vnode, path + [k], k, ktok, dup, chain,
                    leading, inline, contract, doc_index,
                    dup_groups=dup_groups,
                )
        elif node.kind == "array":
            contract["containers"].append(cc.make_container(
                path, "list",
                child_count=len(node.items),
                line_start=node.line, line_end=node.end_line,
                doc_index=doc_index,
            ))
            self._emit_comments(
                node.trailing, path, "trailing", "container",
                contract, doc_index,
            )
            for idx, (vnode, leading, inline) in enumerate(node.items):
                self._walk_node_value(
                    vnode, path + [str(idx)], key, None,
                    False, None, leading, inline, contract, doc_index,
                    dup_groups=dup_groups,
                )
        else:
            self._emit_scalar_entry(
                node, path, key, None, False, None, [], [],
                contract, doc_index,
            )

    def _walk_node_value(
        self, node, path, key, ktok, dup, chain,
        leading, inline, contract, doc_index,
        dup_groups=None,
    ):
        """Object member / array element value: scalar → entry; container → recurse + bind."""
        if node.kind == "scalar":
            self._emit_scalar_entry(
                node, path, key, ktok, dup, chain,
                leading, inline, contract, doc_index,
            )
            return
        # dup_groups must be passed through — duplicate keys in
        # nested objects are part of the disclosure scope.
        self._walk_node(node, path, key, contract, doc_index=doc_index,
                        dup_groups=dup_groups)
        if dup and chain:
            pstr = cc.path_str(path)
            for c in reversed(contract["containers"]):
                if c["path_str"] == pstr:
                    c["duplicate"] = True
                    c["override_chain"] = chain
                    break
        self._emit_comments(
            leading, path, "leading", "container", contract, doc_index,
        )
        self._emit_comments(
            inline, path, "inline", "container", contract, doc_index,
        )

    def _emit_scalar_entry(
        self, node, path, key, ktok, dup, chain,
        leading, inline, contract, doc_index=0,
    ):
        """Scalar entry (raw lexeme + positions + precision + additive keys)."""
        desc, desc_lines = _description_from(leading, inline)
        value = node.value
        state = (
            cc.VALUE_STATE_EXPLICIT_NULL if value is None
            else cc.VALUE_STATE_SCALAR
        )
        vt = _value_type_of(value)
        if node.is_hex:
            vt = "hex_integer"  # spec 第一张 §八：词法基别披露
        elif node.negative_zero:
            vt = "negative_zero"  # 第二批 §5：符号信息保留
        prec = None
        if not node.is_special and isinstance(value, int) \
                and not isinstance(value, bool):
            prec = "exact"  # Python 任意精度整数（十六进制同 exact）
        elif not node.is_special and isinstance(value, float) and not (
            math.isnan(value) or math.isinf(value)
        ):
            prec = "double"
        entry = cc.make_entry(
            path, key, node.raw, None if node.is_special else value,
            value_state=state,
            line_start=(ktok.line if ktok is not None else node.line),
            line_end=node.end_line,
            description=desc, description_lines=desc_lines,
            duplicate=dup, override_chain=chain,
            doc_index=doc_index,
            value_type=vt, type_source="lexical" if vt else None,
            numeric_precision=prec,
        )
        # special numbers: three explicit keys — syntactic validity /
        # JSON outputability / serialization policy — never silently
        # coerced to null
        if node.is_special:
            entry["json5_valid"] = True
            entry["json_compatible"] = False
            entry["serialization_policy"] = "null_with_diagnostic"
        # additive keys: key style / quote style / leading zero —
        # original-text facts never lost
        if ktok is not None:
            entry["raw_key"] = ktok.raw
            entry["key_style"] = _key_style_of(ktok)
        if node.quote is not None:
            entry["quote_style"] = (
                "single" if node.quote == "'" else "double"
            )
        if node.has_continuation:
            # line continuation: flag disclosing that a physical
            # newline was stripped
            entry["line_continuation"] = True
        if node.negative_zero:
            # sign information preserved (not normalized away)
            entry["sign_preserved"] = True
        if node.leading_zero:
            entry["leading_zero"] = True
            contract["loss"]["reasons"].append(
                "leading_zero_lexeme:" + cc.path_str(path)
                + " (accepted per JSON5; lexeme preserved, not normalized)"
            )
        contract["entries"].append(entry)
        # $ref/pointer bypass detection (family single source of
        # truth; registered, never resolved)
        ref = _detect_json_pointer_ref(key, value)
        if (
            ref is None
            and key == "$ref"
            and isinstance(value, str)
            and value
        ):
            # $ref key with a non-pointer value (e.g. an external
            # file path): the family helper only covers the three
            # pointer shapes (pointer/URI/relative #fragment); still
            # registered, never expanded, no file reads (zero
            # execution) — resolution belongs to the
            # resolve_references five-state classification
            # (unresolved/external).
            ref = {"syntax": value, "target": value, "kind": "$ref"}
        if ref is not None:
            entry["references"].append(ref)
        # template registration (register yes / expand no —
        # zero-execution red line)
        if isinstance(value, str) and _TEMPLATE_RE.search(value):
            contract["loss"]["reasons"].append(
                "template_value:" + cc.path_str(path)
                + " (registered only; not expanded — zero execution)"
            )
        self._emit_comments(
            leading, path, "leading", "key", contract, doc_index,
        )
        self._emit_comments(
            inline, path, "inline", "key", contract, doc_index,
        )

    @staticmethod
    def _emit_comments(toks, path, kind, bound_kind, contract, doc_index):
        """Comment tokens → contract['comments'] records (with bound paths)."""
        for c in toks or []:
            contract["comments"].append(cc.make_comment(
                kind, _comment_text(c.raw), c.raw,
                c.line, c.end_line,
                bound_path=path, bound_kind=bound_kind,
                doc_index=doc_index,
            ))

    # ========================================
    # parse entry
    # ========================================

    def parse(self, file_path):
        """Parse a JSON5 file into a Document (with the config contract metadata)."""
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "json5", self.encoding, self.tolerant,
            )
            text = ctx.text
            metadata = ctx.metadata

            # format identity (.json5 → the json5 standalone profile)
            metadata["syntax_profile"] = "json5"

            roots = None
            parse_error = None
            orphan_toks = []
            try:
                toks, _comments = _lex_json5(text)
                doc_parser = _Json5DocParser(toks)
                roots = doc_parser.parse_sequence()
                orphan_toks = doc_parser.orphans
            except _Json5Error as e:
                parse_error = e
            except RecursionError:
                parse_error = _Json5Error(
                    0, 0, "nesting too deep", feature="depth_limit",
                )

            if roots is not None:
                metadata["parse_success"] = True
                metadata["detected_format"] = "json5"
                structured_data = _to_plain(roots[0])
            else:
                metadata["parse_success"] = False
                metadata["detected_format"] = "json5"
                metadata["reason_code"] = "INVALID_JSON5"
                metadata["warnings"].append("json5 parse failed")
                structured_data = {"_error": str(parse_error)}

            # metadata enrichment
            if isinstance(structured_data, dict) \
                    and "_error" not in structured_data:
                metadata["top_level_keys"] = list(structured_data.keys())
            elif isinstance(structured_data, list):
                metadata["top_level_type"] = "list"
                metadata["list_length"] = len(structured_data)

            # ConfigEntry contract (fact layer; failure never blocks
            # the parse)
            metadata["config_entries"] = self._build_contract(
                text, roots, parse_error, orphan_toks,
            )

            # multi-root values disclosed at the metadata layer (not
            # auto-wrapped)
            if roots is not None and len(roots) >= 2:
                metadata["detected_format"] = "json5_sequence"
                metadata["document_count"] = len(roots)
                metadata["reason_code"] = "MULTIPLE_ROOT_VALUES"
                metadata["warnings"].append(
                    f"multiple root values: {len(roots)} documents "
                    "(json5_sequence); not merged into an array"
                )
                structured_data = {"_error": (
                    f"multiple root values: {len(roots)} documents "
                    "(json5_sequence); structured_data not merged"
                )}
                metadata.pop("top_level_keys", None)

            # object-level unresolved registry (family channel reuse;
            # guarded silent degradation)
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)

            return Document(
                text=text,
                metadata={**metadata, "structured_data": structured_data},
                source_text=ctx.source_text,
            )

        except Exception as e:
            metadata = {"parse_failed": True, "parse_error": str(e)}
            return Document(text="", metadata=metadata)

    # ========================================
    # Multi-file overlay merge (isomorphic to the json-family
    # parse_many_merged; core converges in cc.merge_contracts)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Multi-file overlay merge: later files shadow earlier
        ones (de-facto market semantics).

        Each file goes through the existing single-file contract
        channel (with the full disclosure semantics); the core
        overlay merge converges in the shared helper
        ``cc.merge_contracts``. The tail steps stay in this parser:
        cross-file schema/hints propagation (the later declaration
        wins; structural expectations re-run on the merged
        entries/containers) + key re-runs (path_display re-attached,
        sensitive recount; style omitted — across files there is no
        single original text to be faithful to) + structured_data
        dict deep-merge (lists replaced whole, later wins).

        Returns the merged contract dict (not a Document — across
        files there is no single original text; line numbers are
        attributed via source_file). Base parse semantics frozen.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            c = meta.get("config_entries") or cc.build_contract(
                "json5", cc.DUP_LAST_WINS,
            )
            contracts.append(c)
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })

        # core overlay merge converges in the shared helper (family
        # single source of truth)
        merged = cc.merge_contracts("json5", contracts, sources)
        merged_entries = merged["entries"]
        merged_containers = merged["containers"]

        # merge-layer schema disclosure is factually recomputed by
        # apply_schema_status below (single owner) — per-file
        # pass-through unverifiable reasons are stripped first
        # (otherwise union + re-run append would double them)
        merged["loss"]["reasons"] = [
            r for r in merged["loss"]["reasons"]
            if not r.startswith("schema_declared_unverifiable:")
        ]

        # cross-file schema/hints propagation (later declaration
        # wins; consistent with last_file_wins — structural
        # expectations re-run on the merged entries/containers)
        merged_hints = []
        seen_hints = set()
        for c in contracts:
            for h in c.get("domain_hints") or []:
                if h not in seen_hints:
                    seen_hints.add(h)
                    merged_hints.append(h)
        if merged_hints:
            merged["domain_hints"] = merged_hints
        merged_schema_ref = None
        for c in contracts:
            r = c.get("schema_ref")
            if r:
                merged_schema_ref = r
        cc.apply_schema_status(
            merged, merged_schema_ref, merged_hints,
            merged_entries, merged_containers,
            merged["loss"]["reasons"],
        )

        # key re-runs (style omitted — no single original text
        # across files)
        _attach_path_display(merged)
        _attach_sensitive_disclosure(merged)

        # structured_data deep-merge (dicts recursive, lists replaced whole)
        def _deep_merge(a, b):
            if isinstance(a, dict) and isinstance(b, dict):
                out = dict(a)
                for k, v in b.items():
                    out[k] = _deep_merge(out[k], v) if k in out else v
                return out
            return b

        structured = None
        for doc in docs:
            sd = doc.metadata.get("structured_data")
            if not isinstance(sd, dict):
                continue
            structured = sd if structured is None else _deep_merge(structured, sd)
        if structured is not None:
            merged["structured_data"] = structured
        return merged
