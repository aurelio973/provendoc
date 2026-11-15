# ============================================================
# Structured Config Domain
# JSON Semantic Structure Parser
# ============================================================
import json
import logging
import math
import re
import time
from collections import Counter

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import sensitive_signals
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)

_log = logging.getLogger(__name__)

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# JSON5 feature detection (string-aware lexing, disclosure only — no parsing decisions)
_JSON5_KEYWORDS = {"true", "false", "null"}
_JSON5_SPECIALS = {"NaN", "Infinity"}

# ------------------------------------------------------------
# JSON Pointer / $ref bypass detection: whole-value shape matching
# (not substring scanning) — internal `#/frag`, relative external
# `./file#/frag`, URI external `scheme://...#/frag`; strict RFC 6901
# escape restoration (~1→/ before ~0→~); array segments stringified
# for dotted addressing. Reuses the family resolve_references
# five-state channel (does not go through INTERPOLATION_PATTERNS —
# zero pollution of the family pattern table).
# ------------------------------------------------------------
_JSON_POINTER_INTERNAL_RE = re.compile(r"^#/.+$")
_JSON_POINTER_RELATIVE_RE = re.compile(r"^\./[\w./-]*#/.+$")
_JSON_POINTER_URI_RE = re.compile(r"^[a-zA-Z][\w+.-]*://[^\s`\"'<>]*#/\S+$")


def _json_pointer_segments(fragment: str):
    """RFC 6901 fragment (no leading `#/`) → path segment list
    (segments stringified).

    Escape restoration follows RFC 6901 §3 strictly: `~1`→`/` before
    `~0`→`~` (avoids double restoration); numeric segments stay
    strings (consistent with the family path_str dotted convention,
    so array paths like `services.0.name` are addressable).
    """
    return [
        seg.replace("~1", "/").replace("~0", "~")
        for seg in fragment.split("/")
    ]


def _detect_json_pointer_ref(key, value):
    """Whole-value pointer shape detection (strict); returns a ref
    dict or None.

    kind: `$ref` (the $ref key form) / `pointer` (value form);
    internal forms get the target restored to a dotted path
    (the family resolve_references in-doc addressing convention),
    external forms keep the target verbatim (family classification
    external). Detection requires the whole value to be pointer-
    shaped — substring shapes like `see #/a/b` are not detected.
    """
    if not isinstance(value, str) or not value:
        return None
    kind = "$ref" if key == "$ref" else "pointer"
    if _JSON_POINTER_INTERNAL_RE.match(value):
        target = ".".join(_json_pointer_segments(value[2:]))
        return {"syntax": value, "target": target, "kind": kind}
    if _JSON_POINTER_URI_RE.match(value) or _JSON_POINTER_RELATIVE_RE.match(
        value
    ):
        return {"syntax": value, "target": value, "kind": kind}
    return None


# ------------------------------------------------------------
# Additive finalization helpers (path_display / sensitive-entry
# disclosure / the three style keys). Single mount point
# _finalize_p1_addenda — called on both the main path and the
# stdlib fallback / failure paths; the family-common
# path_str/references/sensitive-stamp layers are untouched.
# ------------------------------------------------------------
# Masked shape: follows the established masking precedent (****) —
# never store the value
_SENSITIVE_MASK = "****"

# Indent sampling: JSONC comment lines (// and /* * continuation
# lines) are excluded from the sample
_JSONC_COMMENT_LINE_RE = re.compile(r"^[ \t]*(?://|/\*|\*)")


def _display_path(path, doc_index, list_prefixes):
    """Path segments → `services[0].name` display shape; pure
    object paths → None.

    Assembly: string segments dot-joined, array index segments in
    brackets; index segments are decided by list_prefixes (a set of
    (doc_index, prefix path tuple) — a segment whose prefix is a
    list container is an index segment; multi-root documents are
    isolated by doc_index to prevent cross-document misjudgment of
    same-named paths). The root container (path=[]) has no segments
    and produces nothing.
    """
    if not path:
        return None
    parts = []
    has_index = False
    for i, seg in enumerate(path):
        if (doc_index, tuple(path[:i])) in list_prefixes:
            parts.append("[" + str(seg) + "]")
            has_index = True
        else:
            parts.append(str(seg))
    if not has_index:
        return None
    out = ""
    for part in parts:
        if part.startswith("["):
            out += part
        elif out:
            out += "." + part
        else:
            out = part
    return out


def _attach_path_display(contract):
    """Additive `path_display` key for entries/containers (only
    paths containing array segments get the key; pure object paths
    omit it — no empty shells)."""
    list_prefixes = {
        (c.get("doc_index", 0), tuple(c["path"]))
        for c in contract["containers"]
        if c.get("container_type") == "list"
    }
    for record in contract["entries"] + contract["containers"]:
        display = _display_path(
            record["path"], record.get("doc_index", 0), list_prefixes,
        )
        if display is not None:
            record["path_display"] = display


def _attach_sensitive_disclosure(contract):
    """Raw-value scan hits → additive `sensitive_candidates` key
    ([{type, masked}...], types deduplicated, never the value) plus
    the contract-head reason `sensitive_entry_values:N`; non-hits
    get no keys (read-only reuse of the pattern library; the
    central stamping layer is untouched)."""
    count = 0
    for entry in contract["entries"]:
        raw = entry.get("raw_value")
        if not isinstance(raw, str) or not raw:
            continue
        types = sensitive_signals.scan_sensitive_types(raw)
        if types:
            entry["sensitive_candidates"] = [
                {"type": t, "masked": _SENSITIVE_MASK} for t in types
            ]
            count += 1
    if count:
        contract["loss"]["reasons"].append(
            f"sensitive_entry_values:{count}"
        )
    return count


def _style_indent_width(text):
    """Mode of leading whitespace per line (blank/comment lines
    excluded; unindented top-level lines excluded — missing
    indentation is not width 0, so 0 and k coexisting would
    misreport pure-tab/pure-space files as mixed; content lines
    with no indentation at all → 0, i.e. minified; mixed tab/space
    or tied modes → "mixed"; no content lines → None; a single
    sample is reported as-is, not extrapolated)."""
    widths = []
    saw_tab = saw_space = False
    has_content = False
    for line in text.splitlines():
        if not line.strip() or _JSONC_COMMENT_LINE_RE.match(line):
            continue
        has_content = True
        ws = line[: len(line) - len(line.lstrip(" \t"))]
        if not ws:
            continue
        widths.append(len(ws))
        if "\t" in ws:
            saw_tab = True
        if " " in ws:
            saw_space = True
    if not has_content:
        return None
    if not widths:
        return 0
    if saw_tab and saw_space:
        return "mixed"
    counts = Counter(widths)
    top = max(counts.values())
    modes = [w for w, c in counts.items() if c == top]
    return modes[0] if len(modes) == 1 else "mixed"


def _collect_quote_chars(node, out):
    """String raw lexemes of the node tree → quote character set
    (key tokens scanned too).

    raw is the source lexeme (with surrounding quotes); numbers /
    true / false / null have no quotes and never enter the set."""
    kind = getattr(node, "kind", None)
    if kind == "object":
        for pair in getattr(node, "pairs", None) or []:
            kraw = getattr(pair[0], "raw", None)
            if isinstance(kraw, str) and kraw[:1] in ('"', "'"):
                out.add(kraw[0])
            _collect_quote_chars(pair[1], out)
    elif kind == "array":
        for item in getattr(node, "items", None) or []:
            _collect_quote_chars(item[0], out)
    else:
        raw = getattr(node, "raw", None)
        if isinstance(raw, str) and raw[:1] in ('"', "'"):
            out.add(raw[0])


def _derive_style(text, roots):
    """Additive `style` keys on the contract head (filled only when
    unambiguous; genuine mixtures reported as mixed). quote_style
    is derived only when the full node tree is available (the
    stdlib fallback has no raw lexemes → the key is omitted, never
    fabricated)."""
    style = {}
    indent = _style_indent_width(text)
    if indent is not None:
        style["indent_width"] = indent
    if roots is not None:
        quotes = set()
        for root in roots:
            _collect_quote_chars(root, quotes)
        if len(quotes) == 1:
            style["quote_style"] = "double" if '"' in quotes else "single"
        elif len(quotes) > 1:
            style["quote_style"] = "mixed"
    style["trailing_newline"] = text.endswith("\n")
    return style


def _finalize_p1_addenda(contract, text, roots=None):
    """Additive finalization mount point.

    Also adds the pointer/$ref unresolved head reason (family
    template references without a kind key are not counted here —
    zero drift on the family surface); see each helper for the
    rest.
    """
    n_unresolved = sum(
        1
        for entry in contract["entries"]
        for ref in entry.get("references") or []
        if ref.get("kind") in ("pointer", "$ref")
        and ref.get("resolution_status") == cc.REF_UNRESOLVED
    )
    if n_unresolved:
        contract["loss"]["reasons"].append(
            f"unresolved_pointer_refs:{n_unresolved}"
        )
    _attach_path_display(contract)
    _attach_sensitive_disclosure(contract)
    contract["style"] = _derive_style(text, roots)


def _detect_json5_extensions(text):
    """String-aware scan for JSON5 extension lexical features
    (evidence-only).

    Feature set: single_quotes / bare_keys / unquoted_values /
    hex_numbers / special_numbers / extra_commas. Strings (double/
    single quoted) and comment content are not judged; returns the
    feature set (empty = no features). Disclosure only — not used
    for parsing decisions.
    """
    features = set()
    stack = []  # container context: "{" / "["
    last_sig = ""  # last significant character/word ('w' = word)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        # String (double quotes): a legal JSON string, skip as a whole
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                i += 1
            i += 1
            last_sig = 'w'
            continue
        # Single-quoted string: a JSON5 feature; skip as a string to avoid content false positives
        if ch == "'":
            features.add("single_quotes")
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                i += 1
            i += 1
            last_sig = 'w'
            continue
        # Comment (JSONC): skipped, not judged
        if ch == "/" and i + 1 < n and text[i + 1] in "/*":
            if text[i + 1] == "/":
                while i < n and text[i] not in "\r\n":
                    i += 1
            else:
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
            continue
        if ch in " \t\r\n":
            i += 1
            continue
        if ch in "{[":
            stack.append(ch)
            last_sig = ch
            i += 1
            continue
        if ch in "}]":
            if stack:
                stack.pop()
            last_sig = ch
            i += 1
            continue
        if ch == ",":
            if last_sig in ("{", "[", ","):
                features.add("extra_commas")
            else:
                # Trailing comma lookahead: the next non-space char is a closer
                k = i + 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] in "}]":
                    features.add("extra_commas")
            last_sig = ch
            i += 1
            continue
        # Bare word (starts with letter/_/$)
        if ch.isalpha() or ch in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            if word in _JSON5_SPECIALS:
                features.add("special_numbers")
            else:
                in_obj = bool(stack) and stack[-1] == "{"
                if in_obj and last_sig in ("{", ","):
                    features.add("bare_keys")
                elif word not in _JSON5_KEYWORDS:
                    features.add("unquoted_values")
            i = j
            last_sig = "w"
            continue
        # Number (with hex lookahead)
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            if ch == "0" and i + 1 < n and text[i + 1] in "xX":
                features.add("hex_numbers")
                i += 2
                while i < n and text[i] in "0123456789abcdefABCDEF":
                    i += 1
            else:
                i += 1 if ch != "-" else 2
                while i < n and (text[i].isalnum() or text[i] in ".+-eE"):
                    i += 1
            last_sig = "w"
            continue
        last_sig = ch
        i += 1
    return features


def _try_load_multi_root(text, **loads_kwargs):
    """Slice roots with raw_decode; returns the document list when
    all roots are legal and there are ≥2.

    Any illegal root (including trailing garbage) → None — no
    partial success dressed up as json_sequence.
    """
    dec = json.JSONDecoder(**loads_kwargs)
    docs = []
    idx, n = 0, len(text)
    while True:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            val, end = dec.raw_decode(text, idx)
        except Exception:
            return None
        docs.append(val)
        idx = end
    return docs if len(docs) >= 2 else None


def _load_json_sequence(text, **loads_kwargs):
    """Multi-root json_sequence detection (with a JSONC-stripped
    retry).

    Returns: (docs, stats) — stats is None or the jsonc strip
    counts (comments between roots); no multi-root → (None, None).
    """
    docs = _try_load_multi_root(text, **loads_kwargs)
    if docs is not None:
        return docs, None
    sanitized, stats = _strip_jsonc(text)
    if stats["comments_stripped"] or stats["trailing_commas_removed"]:
        docs = _try_load_multi_root(sanitized, **loads_kwargs)
        if docs is not None:
            return docs, stats
    return None, None


def _strip_jsonc(text):
    """String-aware stripping of JSONC comments, then trailing
    commas.

    Character-by-character scan: everything inside strings
    (including escapes) is preserved verbatim — `"http://a"` is
    never treated as a comment; `//` line comments and `/* */`
    block comments outside strings are removed. Trailing commas
    are handled after comment stripping (comments may sit between
    a `,` and `}`).

    Returns:
        (cleaned_text, stats): stats records the comment and
        trailing-comma counts so callers can write them honestly
        into metadata / contract loss.
    """
    out = []
    i, n = 0, len(text)
    in_str = False
    comments = 0
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                comments += 1
                i += 2
                while i < n and text[i] not in "\r\n":
                    i += 1
                continue
            if nxt == "*":
                comments += 1
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
                continue
        out.append(ch)
        i += 1
    cleaned, trailing = _TRAILING_COMMA_RE.subn(r"\1", "".join(out))
    return cleaned, {
        "comments_stripped": comments,
        "trailing_commas_removed": trailing,
    }


def _loads_with_dialect(text, **loads_kwargs):
    """Strict first; on failure, strip the JSONC dialect
    (comments / trailing commas) and retry.

    Returns:
        (ok, data, dialect, stats, error): dialect ∈ {"strict",
        "jsonc"}; stats and error are None depending on the path
        taken. Real-world "JSON" like tsconfig.json and
        .vscode/*.json goes the jsonc route; the caller must write
        the dialect and strip counts into metadata (honest record),
        never silently treating it as standard JSON.
    """
    try:
        return True, json.loads(text, **loads_kwargs), "strict", None, None
    except Exception as e:
        strict_error = e
    sanitized, stats = _strip_jsonc(text)
    if not stats["comments_stripped"] and not stats["trailing_commas_removed"]:
        return False, None, "strict", None, strict_error
    try:
        return True, json.loads(sanitized, **loads_kwargs), "jsonc", stats, None
    except Exception:
        return False, None, "strict", None, strict_error


class _DupTrackingDict(dict):
    """Dict recording same-level duplicate-key occurrence order
    (used as the json object_pairs_hook).

    json.loads semantics: duplicate keys are last_wins (the later
    one overwrites the earlier).
    duplicates[key] keeps all overwritten historical values (in
    occurrence order).
    """
    def __init__(self):
        """Initialize an empty mapping and the duplicate-key history table."""
        super().__init__()
        self.duplicates = {}


def _object_pairs_hook(pairs):
    obj = _DupTrackingDict()
    for key, value in pairs:
        if key in obj:
            obj.duplicates.setdefault(key, []).append(obj[key])
        obj[key] = value
    return obj


# ============================================================
# In-house recursive-descent parser
# Positions (0-based line/col, yaml-family convention) / raw source
# lexemes / JSONC comment capture / precision disclosure in one;
# value domain aligned with stdlib json.loads (stdlib is the
# double-run reconciliation anchor — divergences fall back to
# stdlib and leave a trace). NaN/Infinity accepted as the stdlib
# parse_constant superset.
# ============================================================

_JSON_ESCAPES = {
    '"': '"', '\\': '\\', '/': '/',
    'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t',
}
_JSON_HEX = '0123456789abcdefABCDEF'
_JSON_WORDS = {
    'true': ('literal', True),
    'false': ('literal', False),
    'null': ('literal', None),
    'NaN': ('number', float('nan')),
    'Infinity': ('number', float('inf')),
}

# Safety limits (exceeded → reason + stdlib fallback; production
# values are generous, tests trigger them by monkeypatching down)
_MAX_NODES = 500_000
_MAX_STRING_LENGTH = 1_000_000
_MAX_ARRAY_LENGTH = 1_000_000
_MAX_OBJECT_KEYS = 100_000


class _JsonParseError(Exception):
    """In-house parser syntax / resource-limit error (0-based line/col + offset)."""

    def __init__(self, line, col, message, code='syntax', off=-1):
        super().__init__(f'line {line}, col {col}: {message}')
        self.line = line
        self.col = col
        self.code = code
        self.off = off


class _Tok:
    """Lexical token: kind + value + raw source lexeme + start/end line/col/offset."""

    __slots__ = (
        'kind', 'value', 'raw', 'line', 'col',
        'end_line', 'end_col', 'off', 'end_off',
    )

    def __init__(self, kind, value, raw, line, col, end_line, end_col, off, end_off):
        self.kind = kind
        self.value = value
        self.raw = raw
        self.line = line
        self.col = col
        self.end_line = end_line
        self.end_col = end_col
        self.off = off
        self.end_off = end_off


class _Node:
    """Syntax tree node: kind + value + raw + start/end positions + comment bindings."""

    __slots__ = (
        'kind', 'value', 'raw', 'line', 'col',
        'end_line', 'end_col', 'pairs', 'items', 'trailing',
    )

    def __init__(self, kind, value, raw, line, col, end_line, end_col):
        self.kind = kind  # 'object' | 'array' | 'scalar'
        self.value = value
        self.raw = raw
        self.line = line
        self.col = col
        self.end_line = end_line
        self.end_col = end_col
        self.pairs = None  # object: [(key_tok, value_node, leading, inline)]
        self.items = None  # array: [(value_node, leading, inline)]
        self.trailing = []  # unbound comments before the container's closer


def _pos_after(sl, sc, seg):
    """Position after a token (0-based; seg is the token's source slice)."""
    nl = seg.count('\n')
    if nl:
        return sl + nl, len(seg) - (seg.rfind('\n') + 1)
    return sl, sc + len(seg)


def _comment_text(raw):
    """Comment body (delimiters and surrounding whitespace stripped)."""
    if raw.startswith('//'):
        return raw[2:].strip()
    if raw.startswith('/*') and raw.endswith('*/'):
        return raw[2:-2].strip()
    return raw.strip()


def _lex_string(text, i, n, sl, sc, si):
    """String token value domain (stdlib json aligned: escapes / surrogate pairs / control chars)."""
    i += 1  # skip the opening quote
    buf = []
    while i < n:
        c = text[i]
        if c == '"':
            return ''.join(buf), i + 1
        if c == '\\' and i + 1 < n:
            e = text[i + 1]
            i += 2
            if e in _JSON_ESCAPES:
                buf.append(_JSON_ESCAPES[e])
            elif e == 'u':
                if i + 4 > n or any(
                    text[i + k] not in _JSON_HEX for k in range(min(4, n - i))
                ):
                    raise _JsonParseError(sl, sc, 'invalid \\u escape', off=si)
                cp = int(text[i:i + 4], 16)
                i += 4
                if (
                    0xD800 <= cp <= 0xDBFF
                    and text[i:i + 2] == '\\u'
                    and i + 6 <= n
                    and all(text[i + 2 + k] in _JSON_HEX for k in range(4))
                ):
                    lo = int(text[i + 2:i + 6], 16)
                    if 0xDC00 <= lo <= 0xDFFF:
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                        i += 6
                buf.append(chr(cp))
            else:
                raise _JsonParseError(sl, sc, f'invalid escape \\{e}', off=si)
        elif ord(c) < 0x20:
            raise _JsonParseError(sl, sc, 'control character in string', off=si)
        else:
            buf.append(c)
            i += 1
    raise _JsonParseError(sl, sc, 'unterminated string', off=si)


def _lex_number(text, i, n, sl, sc, si):
    """Number token value domain (RFC 8259 grammar; returns (value, is_float, end))."""
    j = i + 1 if text[i] == '-' else i
    if j >= n or not text[j].isdigit():
        raise _JsonParseError(sl, sc, 'invalid number', off=si)
    if text[j] == '0':
        j += 1
        if j < n and text[j].isdigit():
            raise _JsonParseError(sl, sc, 'leading zeros not allowed', off=si)
    else:
        while j < n and text[j].isdigit():
            j += 1
    is_float = False
    if j < n and text[j] == '.':
        is_float = True
        j += 1
        if j >= n or not text[j].isdigit():
            raise _JsonParseError(sl, sc, 'invalid number (fraction)', off=si)
        while j < n and text[j].isdigit():
            j += 1
    if j < n and text[j] in 'eE':
        is_float = True
        j += 1
        if j < n and text[j] in '+-':
            j += 1
        if j >= n or not text[j].isdigit():
            raise _JsonParseError(sl, sc, 'invalid number (exponent)', off=si)
        while j < n and text[j].isdigit():
            j += 1
    raw = text[i:j]
    return (float(raw) if is_float else int(raw)), is_float, j


def _lex_json(text, partial=False):
    """JSON/JSONC lexer (comments become tokens, never dropped;
    includes the stdlib-superset number words).

    Returns:
        (toks, comments, err): raised when err is not None and
        partial=False (partial=True serves truncated-input local
        recovery).
    """
    toks = []
    comments = []
    err = None
    i, n = 0, len(text)
    line, col = 0, 0
    try:
        while i < n:
            ch = text[i]
            si, sl, sc = i, line, col
            if ch in ' \t\r\n':
                if ch == '\n':
                    line += 1
                    col = 0
                else:
                    col += 1
                i += 1
                continue
            if ch == '"':
                value, end = _lex_string(text, i, n, sl, sc, si)
                raw = text[si:end]
                if len(raw) > _MAX_STRING_LENGTH:
                    raise _JsonParseError(
                        sl, sc, 'string length limit exceeded',
                        code='string_length_limit', off=si,
                    )
                el, ec = _pos_after(sl, sc, raw)
                toks.append(_Tok('string', value, raw, sl, sc, el, ec, si, end))
                line, col = el, ec
                i = end
                continue
            if ch == '/' and text[i + 1:i + 2] == '/':
                i += 2
                while i < n and text[i] not in '\r\n':
                    i += 1
                raw = text[si:i]
                el, ec = _pos_after(sl, sc, raw)
                tok = _Tok('comment', _comment_text(raw), raw, sl, sc, el, ec, si, i)
                toks.append(tok)
                comments.append(tok)
                line, col = el, ec
                continue
            if ch == '/' and text[i + 1:i + 2] == '*':
                i += 2
                while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                    i += 1
                if i + 1 >= n:
                    raise _JsonParseError(sl, sc, 'unterminated block comment', off=si)
                i += 2
                raw = text[si:i]
                el, ec = _pos_after(sl, sc, raw)
                tok = _Tok('comment', _comment_text(raw), raw, sl, sc, el, ec, si, i)
                toks.append(tok)
                comments.append(tok)
                line, col = el, ec
                continue
            if ch in '{}[],:':
                i += 1
                toks.append(_Tok(ch, ch, ch, sl, sc, sl, sc + 1, si, i))
                col += 1
                continue
            if ch == '-' and text[i + 1:i + 9] == 'Infinity':
                raw = text[si:i + 9]
                el, ec = _pos_after(sl, sc, raw)
                toks.append(_Tok(
                    'number', float('-inf'), raw, sl, sc, el, ec, si, i + 9,
                ))
                line, col = el, ec
                i += 9
                continue
            if ch.isalpha():
                j = i
                while j < n and text[j].isalpha():
                    j += 1
                word = text[i:j]
                if word not in _JSON_WORDS:
                    raise _JsonParseError(sl, sc, f'invalid token {word!r}', off=si)
                kind, value = _JSON_WORDS[word]
                raw = text[si:j]
                el, ec = _pos_after(sl, sc, raw)
                toks.append(_Tok(kind, value, raw, sl, sc, el, ec, si, j))
                line, col = el, ec
                i = j
                continue
            if ch == '-' or ch.isdigit():
                value, _is_float, j = _lex_number(text, i, n, sl, sc, si)
                raw = text[si:j]
                el, ec = _pos_after(sl, sc, raw)
                toks.append(_Tok('number', value, raw, sl, sc, el, ec, si, j))
                line, col = el, ec
                i = j
                continue
            raise _JsonParseError(sl, sc, f'unexpected character {ch!r}', off=si)
    except _JsonParseError as e:
        err = e
    if err is not None and not partial:
        raise err
    return toks, comments, err


class _JsonDocParser:
    """Recursive-descent parser (token stream → positioned node tree + four-way comment classification)."""

    def __init__(self, toks, start=0, end=None):
        self.toks = toks
        self.pos = start
        self.end = len(toks) if end is None else end
        self.stats = {'comments': 0, 'trailing_commas': 0}
        self.orphans = []
        self.nodes = 0

    def _peek(self):
        return self.toks[self.pos] if self.pos < self.end else None

    def _next(self):
        if self.pos >= self.end:
            return None
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def parse_sequence(self):
        """Parse to the end of the token stream (multiple roots → multiple root nodes)."""
        roots = []
        pending = []
        while True:
            tok = self._peek()
            if tok is None:
                break
            if tok.kind == 'comment':
                self._next()
                self.stats['comments'] += 1
                pending.append(tok)
                continue
            roots.append(self.parse_value())
            self.orphans.extend(pending)
            pending = []
        self.orphans.extend(pending)
        if not roots:
            raise _JsonParseError(0, 0, 'empty document', off=0)
        return roots

    def parse_value(self):
        tok = self._peek()
        if tok is None:
            raise _JsonParseError(0, 0, 'unexpected end of input', off=0)
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            raise _JsonParseError(
                tok.line, tok.col, 'node limit exceeded',
                code='node_limit', off=tok.off,
            )
        if tok.kind == '{':
            return self._parse_object()
        if tok.kind == '[':
            return self._parse_array()
        if tok.kind in ('string', 'number', 'literal'):
            self._next()
            return _Node(
                'scalar', tok.value, tok.raw,
                tok.line, tok.col, tok.end_line, tok.end_col,
            )
        raise _JsonParseError(
            tok.line, tok.col, f'unexpected token {tok.raw!r}', off=tok.off,
        )

    def _parse_object(self):
        open_tok = self._next()
        node = _Node(
            'object', None, open_tok.raw,
            open_tok.line, open_tok.col, open_tok.line, open_tok.col,
        )
        node.pairs = []
        leading = []
        after = []
        state = 'key_or_close'  # key_or_close | after_comma | after_value
        while True:
            tok = self._peek()
            if tok is None:
                raise _JsonParseError(
                    open_tok.line, open_tok.col, 'unterminated object',
                    off=open_tok.off,
                )
            if tok.kind == 'comment':
                self._next()
                self.stats['comments'] += 1
                if state == 'after_value':
                    after.append(tok)
                elif (
                    state == 'after_comma' and node.pairs
                    and tok.line == node.pairs[-1][1].end_line
                ):
                    # Same-line comment after the comma → inline on the previous pair (yaml-family convention)
                    kt, vnode, lead, inl = node.pairs[-1]
                    node.pairs[-1] = (kt, vnode, lead, inl + [tok])
                else:
                    leading.append(tok)
                continue
            if tok.kind == '}':
                if state == 'after_comma':
                    self.stats['trailing_commas'] += 1
                self._next()
                node.end_line = tok.end_line
                node.end_col = tok.end_col
                rest = []
                if after:
                    if node.pairs:
                        kt, vnode, lead, inl = node.pairs[-1]
                        same = [c for c in after if c.line == vnode.end_line]
                        rest = [c for c in after if c.line != vnode.end_line]
                        node.pairs[-1] = (kt, vnode, lead, inl + same)
                    else:
                        rest = after
                # Leading comments unconsumed at the closer (e.g.
                # cross-line comments after the last key's comma)
                # → container trailing (never dropped)
                node.trailing = rest + leading
                leading = []
                break
            if state in ('key_or_close', 'after_comma'):
                if tok.kind != 'string':
                    raise _JsonParseError(
                        tok.line, tok.col,
                        f"expected string key, got {tok.raw!r}",
                        off=tok.off,
                    )
                key_tok = self._next()
                colon_seen = False
                while True:
                    tok2 = self._peek()
                    if tok2 is None:
                        raise _JsonParseError(
                            key_tok.line, key_tok.col,
                            "unterminated object (missing ':')",
                            off=key_tok.off,
                        )
                    if tok2.kind == 'comment':
                        self._next()
                        self.stats['comments'] += 1
                        leading.append(tok2)
                        continue
                    colon_seen = tok2.kind == ':'
                    self._next()
                    break
                if not colon_seen:
                    raise _JsonParseError(
                        key_tok.line, key_tok.col, "expected ':' after key",
                        off=key_tok.off,
                    )
                value = self.parse_value()
                if len(node.pairs) + 1 > _MAX_OBJECT_KEYS:
                    raise _JsonParseError(
                        key_tok.line, key_tok.col, 'object key limit exceeded',
                        code='object_key_limit', off=key_tok.off,
                    )
                inline = [c for c in after if c.line == value.end_line]
                rest = [c for c in after if c.line != value.end_line]
                node.pairs.append((key_tok, value, leading, inline))
                leading = rest
                after = []
                state = 'after_value'
                continue
            if tok.kind != ',':
                raise _JsonParseError(
                    tok.line, tok.col,
                    f"expected ',' or '}}', got {tok.raw!r}",
                    off=tok.off,
                )
            # ',' arrives: after-comments are placed by line
            # (same line → inline on the previous pair; across
            # lines → leading on the next key)
            if after and node.pairs:
                kt, vnode, lead, inl = node.pairs[-1]
                same = [c for c in after if c.line == vnode.end_line]
                rest = [c for c in after if c.line != vnode.end_line]
                node.pairs[-1] = (kt, vnode, lead, inl + same)
                leading.extend(rest)
            after = []
            self._next()
            state = 'after_comma'
        node.value = {}
        for key_tok, vnode, _l, _i in node.pairs:
            node.value[key_tok.value] = vnode.value
        return node

    def _parse_array(self):
        open_tok = self._next()
        node = _Node(
            'array', None, open_tok.raw,
            open_tok.line, open_tok.col, open_tok.line, open_tok.col,
        )
        node.items = []
        leading = []
        after = []
        state = 'value_or_close'  # value_or_close | after_comma | after_value
        while True:
            tok = self._peek()
            if tok is None:
                raise _JsonParseError(
                    open_tok.line, open_tok.col, 'unterminated array',
                    off=open_tok.off,
                )
            if tok.kind == 'comment':
                self._next()
                self.stats['comments'] += 1
                if state == 'after_value':
                    after.append(tok)
                elif (
                    state == 'after_comma' and node.items
                    and tok.line == node.items[-1][0].end_line
                ):
                    # Same-line comment after the comma → inline on the previous element
                    vnode, lead, inl = node.items[-1]
                    node.items[-1] = (vnode, lead, inl + [tok])
                else:
                    leading.append(tok)
                continue
            if tok.kind == ']':
                if state == 'after_comma':
                    self.stats['trailing_commas'] += 1
                self._next()
                node.end_line = tok.end_line
                node.end_col = tok.end_col
                rest = []
                if after:
                    if node.items:
                        vnode, lead, inl = node.items[-1]
                        same = [c for c in after if c.line == vnode.end_line]
                        rest = [c for c in after if c.line != vnode.end_line]
                        node.items[-1] = (vnode, lead, inl + same)
                    else:
                        rest = after
                # Leading comments unconsumed at the closer → container trailing (never dropped)
                node.trailing = rest + leading
                leading = []
                break
            if state in ('value_or_close', 'after_comma'):
                if len(node.items) + 1 > _MAX_ARRAY_LENGTH:
                    raise _JsonParseError(
                        tok.line, tok.col, 'array length limit exceeded',
                        code='array_length_limit', off=tok.off,
                    )
                value = self.parse_value()
                inline = [c for c in after if c.line == value.end_line]
                rest = [c for c in after if c.line != value.end_line]
                node.items.append((value, leading, inline))
                leading = rest
                after = []
                state = 'after_value'
                continue
            if tok.kind != ',':
                raise _JsonParseError(
                    tok.line, tok.col,
                    f"expected ',' or ']', got {tok.raw!r}",
                    off=tok.off,
                )
            if after and node.items:
                vnode, lead, inl = node.items[-1]
                same = [c for c in after if c.line == vnode.end_line]
                rest = [c for c in after if c.line != vnode.end_line]
                node.items[-1] = (vnode, lead, inl + same)
                leading.extend(rest)
            after = []
            self._next()
            state = 'after_comma'
        node.value = [vnode.value for vnode, _l, _i in node.items]
        return node


def _parse_json_documented(text):
    """Full parse (multi-root / JSONC / comment collection; value
    domain aligned with stdlib).

    Returns:
        (roots, stats): roots is the root node list (≥2 means
        json_sequence); stats holds the comments/trailing_commas
        counts and the orphan_comments token list.
    Raises:
        _JsonParseError / RecursionError
    """
    toks, _comments, _err = _lex_json(text)
    parser = _JsonDocParser(toks)
    roots = parser.parse_sequence()
    stats = dict(parser.stats)
    stats['orphan_comments'] = parser.orphans
    return roots, stats


def _chain_value_node(node):
    """override_chain payload (same convention as the stdlib walk)."""
    if node.kind == 'object':
        return {'kind': 'dict', 'child_count': len(node.value)}
    if node.kind == 'array':
        return {'kind': 'list', 'child_count': len(node.value)}
    return node.value


def _value_type_of(value):
    """Scalar value type (the config contract VALUE_TYPE constant family)."""
    if value is None:
        return cc.VALUE_TYPE_NULL
    if isinstance(value, bool):
        return cc.VALUE_TYPE_BOOL
    if isinstance(value, str):
        return cc.VALUE_TYPE_STRING
    if isinstance(value, int):
        return cc.VALUE_TYPE_INT
    if isinstance(value, float):
        if math.isnan(value):
            return cc.VALUE_TYPE_FLOAT_NAN
        if value == math.inf:
            return cc.VALUE_TYPE_FLOAT_INFINITY
        if value == -math.inf:
            return cc.VALUE_TYPE_FLOAT_NEG_INFINITY
        return cc.VALUE_TYPE_FLOAT
    return None


def _description_from(leading, inline):
    """Description text (leading first, yaml-family block-first convention)."""
    if leading:
        text = '\n'.join(_comment_text(c.raw) for c in leading)
        return text, [leading[0].line, leading[-1].end_line]
    if inline:
        return _comment_text(inline[0].raw), [inline[0].line, inline[0].end_line]
    return None, None


def _recover_partial(text):
    """Local recovery (degraded-chain tier).

    Truncated / corrupted documents: complete subtrees first
    (bracket matching + re-parse verification) → degraded key-value
    scan. Half tokens adjacent to the lexical damage point are not
    trusted (nothing fabricated).

    Returns:
        (spans, pairs, strategy): spans = [(path_prefix, node)]
        complete subtrees (with their own comment bindings);
        pairs = [(entry_path, key, node)] degraded key-value pairs;
        strategy ∈ complete_subtree | key_value_scan | mixed | none.
    """
    toks, _comments, err = _lex_json(text, partial=True)
    err_off = err.off if err is not None else -1

    raw_spans = []  # (start_idx, end_idx, path_prefix)
    pair_cands = []  # (base_path, key_tok, value_tok, value_idx)
    frames = []  # {'kind', 'path', 'pk', 'ktok', 'elem', 'start'}
    last_kind = None

    for i, tok in enumerate(toks):
        k = tok.kind
        if k == 'comment':
            continue
        if k in ('{', '['):
            if frames:
                top = frames[-1]
                if top['kind'] == '{':
                    name = top['pk']
                else:
                    name = str(top['elem'])
                    top['elem'] += 1
                prefix = top['path'] + [name]
            else:
                prefix = []
            frames.append({
                'kind': k, 'path': prefix, 'pk': None,
                'ktok': None, 'elem': 0, 'start': i,
            })
            last_kind = k
            continue
        if k in ('}', ']'):
            matching = '{' if k == '}' else '['
            if frames and frames[-1]['kind'] == matching:
                top = frames.pop()
                raw_spans.append((top['start'], i, top['path']))
            elif frames:
                frames.pop()  # mismatched closer: drop the broken frame (nothing fabricated)
            last_kind = k
            continue
        if k == ':':
            if frames and frames[-1]['kind'] == '{' and last_kind != 'key':
                # Non-key before the colon (damaged region): poison the pending key to prevent a mispaired value
                frames[-1]['ktok'] = None
                frames[-1]['pk'] = None
            last_kind = ':'
            continue
        if (
            k == 'string'
            and frames
            and frames[-1]['kind'] == '{'
            and last_kind in ('{', ',')
        ):
            frames[-1]['pk'] = tok.value
            frames[-1]['ktok'] = tok
            last_kind = 'key'
            continue
        # Value-position determination (scalar token)
        is_value = False
        key_tok = None
        base_path = []
        if frames:
            top = frames[-1]
            if top['kind'] == '{' and last_kind == ':':
                if top['ktok'] is not None:
                    is_value = True
                    key_tok = top['ktok']
                    base_path = top['path']
            elif top['kind'] == '[' and last_kind in ('[', ',', ']'):
                is_value = True
                base_path = top['path'] + [str(top['elem'])]
                top['elem'] += 1
        elif last_kind is None:
            is_value = True  # top-level scalar root
        if is_value and k in ('string', 'number', 'literal'):
            if err_off < 0 or tok.end_off != err_off:
                pair_cands.append((base_path, key_tok, tok, i))
        last_kind = k

    # --- Span validation (re-parse consumes exactly → accepted; outer spans first, no overlap) ---
    accepted = []  # (start, end, node, prefix)
    covered = []  # (start, end) token index ranges (inclusive)
    for start, end, prefix in sorted(raw_spans, key=lambda s: (s[0], s[1])):
        if any(s <= start and end <= e for s, e in covered):
            continue  # covered by an accepted larger outer span
        try:
            sub = _JsonDocParser(toks, start, end + 1)
            node = sub.parse_value()
            if sub.pos != end + 1:
                raise _JsonParseError(0, 0, 'span not fully consumed')
        except Exception:
            continue  # RecursionError/_JsonParseError are both untrusted
        accepted.append((start, end, node, prefix))
        covered.append((start, end))

    def _covered(idx):
        return any(s <= idx <= e for s, e in covered)

    pairs = []
    for base_path, key_tok, tok, idx in pair_cands:
        if _covered(idx):
            continue  # a formal entry already exists inside a complete subtree (avoid duplicate disclosure)
        node = _Node(
            'scalar', tok.value, tok.raw,
            tok.line, tok.col, tok.end_line, tok.end_col,
        )
        entry_path = list(base_path) + ([key_tok.value] if key_tok else [])
        pairs.append((entry_path, key_tok.value if key_tok else None, node))

    spans = [(prefix, node) for _s, _e, node, prefix in accepted]
    if spans and pairs:
        strategy = 'mixed'
    elif spans:
        strategy = 'complete_subtree'
    elif pairs:
        strategy = 'key_value_scan'
    else:
        strategy = 'none'
    return spans, pairs, strategy


class JsonParser(BaseParser):
    """JSON config file parser (duplicate-key override chain +
    empty-container tri-state contract).

    Dialect strategy: strict JSON first; on failure, string-awarely
    strip JSONC comments and trailing commas and retry (tsconfig /
    VS Code scenarios), recording the strip counts honestly in
    metadata["json_dialect"]="jsonc" and metadata["json_relaxed"]
    — never silently treating it as standard JSON.
    """

    def __init__(
        self,
        encoding="utf-8",
        tolerant=True,
    ):
        """Initialize with encoding and tolerance switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    # ========================================
    # config entry contract
    # ========================================

    def _build_contract(self, text, jsonc_expected=False):
        """Build the contract: duplicate-key override chain +
        empty-container tri-state + positions / raw / comments /
        precision.

        The in-house recursive-descent parser supplies token
        positions (0-based line/col, yaml-family convention), raw
        source lexemes, four-way JSONC comment bindings, and the
        numeric precision keys; the value domain is aligned with
        stdlib json.loads (double-run reconciliation anchors the
        tests; divergences fall back to stdlib with a trace).
        structured_data values still come from the stdlib-
        equivalent path (the parse layer is unchanged).
        """
        contract = cc.build_contract(
            "json", cc.DUP_LAST_WINS,
            line_numbers=True, comments=True,
        )
        reasons = contract["loss"]["reasons"]

        try:
            roots, stats = _parse_json_documented(text)
        except _JsonParseError as e:
            return self._contract_parse_failed(contract, reasons, text, e)
        except RecursionError:
            reasons.append("nesting_too_deep")
            # Finalization: text facts (indent/trailing) are still available, no node tree
            _finalize_p1_addenda(contract, text)
            return contract

        if len(roots) >= 2:
            # Multi-root json_sequence is disclosed per document
            # (no merging into an array; nothing partial)
            contract["detected_format"] = "json_sequence"
            contract["document_count"] = len(roots)
            contract["reason_code"] = "MULTIPLE_ROOT_VALUES"

        try:
            for doc_index, root in enumerate(roots):
                self._walk_node(root, [], None, contract, doc_index=doc_index)
            cc.resolve_references(contract)
        except RecursionError:
            reasons.append("nesting_too_deep")
        except Exception as e:  # pragma: no cover - final safety net
            reasons.append(f"contract_build_failed: {e}")

        # JSONC dialect tolerance disclosure + formalized mismatch
        # (JSONC inside .json → format_mismatch; the .jsonc suffix
        # is legal, no mismatch)
        if stats["comments"] or stats["trailing_commas"]:
            reasons.append(
                "dialect_jsonc_relaxed: "
                f"{stats['comments']} comment(s), "
                f"{stats['trailing_commas']} trailing comma(s) "
                "tolerated natively; comments bound as descriptions "
                "where applicable"
            )
            if not jsonc_expected:
                contract["format_mismatch"] = True
                if "reason_code" not in contract:
                    contract["reason_code"] = "JSONC_DIALECT_IN_JSON"
                reasons.append(
                    "jsonc_dialect_in_json: comments/trailing commas "
                    "are not strict RFC 8259"
                )

        # NaN/Infinity damage-level disclosure (value domain unchanged, disclosure only)
        if "special_numbers" in _detect_json5_extensions(text):
            if "detected_format" not in contract:
                contract["detected_format"] = "json5_candidate"
            contract["format_mismatch"] = True
            if "reason_code" not in contract:
                contract["reason_code"] = "NON_JSONC_EXTENSION_DETECTED"
            reasons.append(
                "NON_JSONC_EXTENSION_DETECTED:special_numbers "
                "(NaN/Infinity accepted by python json superset, "
                "not RFC 8259)"
            )

        # domain_hints weak declaration + schema tri-state (family
        # layer cc.apply_schema_status, replacing the earlier
        # $schema-only absent registration). Hint inference matches
        # yaml: k8s/compose by strict top-level keys; CI's image/
        # script usually live below the second level, so the whole
        # tree's keys are used (weak declaration, empty hits omit
        # the key)
        root_value = roots[0].value
        hints = []
        if isinstance(root_value, dict):
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
        if isinstance(root_value, dict):
            declared = root_value.get("$schema")
            if isinstance(declared, str) and declared:
                schema_ref = declared
        cc.apply_schema_status(
            contract, schema_ref, hints,
            contract["entries"], contract["containers"], reasons,
        )

        # JSON Patch candidate recognition (detect only, never
        # execute; merge-patch shapes carry no strong signal and
        # are not marked — the false-positive cost is unacceptable)
        if (
            isinstance(root_value, list) and root_value
            and all(
                isinstance(x, dict) and "op" in x and "path" in x
                for x in root_value
            )
        ):
            contract["document_kind"] = "json_patch_candidate"
            contract["execution"] = "not_attempted"

        # Top-level orphan comments (sequence-level, no bound object)
        if stats["orphan_comments"]:
            for c in stats["orphan_comments"]:
                contract["comments"].append(cc.make_comment(
                    "orphan", _comment_text(c.raw), c.raw, c.line, c.end_line,
                ))
            reasons.append(
                f"orphan_comments:{len(stats['orphan_comments'])}"
            )

        # Additive finalization: unresolved head reason +
        # path_display + sensitive-entry disclosure + style
        # (roots available → quote_style)
        _finalize_p1_addenda(contract, text, roots)
        return contract

    def _contract_parse_failed(self, contract, reasons, text, err):
        """Failure path: stdlib cross-check guard → local recovery → JSON5 flagging."""
        # Defensive guard: stdlib succeeds while the in-house parser
        # fails (resource limit or real divergence) → stdlib
        # fallback, honestly admitting the capability gap (the only
        # fallback path)
        ok, data, dialect, _stats, _error = _loads_with_dialect(
            text, object_pairs_hook=_object_pairs_hook
        )
        if ok:
            if err.code != "syntax":
                reasons.append(f"resource_limit:{err.code}")
            else:
                reasons.append(
                    f"parser_stdlib_divergence_fallback: {err}"
                )
            reasons.extend([
                "line_numbers_unavailable: stdlib fallback path",
                "raw_value_is_canonical_re_render: stdlib fallback path",
                "comments_unavailable: stdlib fallback path",
            ])
            if dialect == "jsonc":
                reasons.append(
                    "dialect_jsonc_relaxed (stdlib fallback path)"
                )
            try:
                self._walk(
                    data, [], None,
                    contract["entries"], contract["containers"],
                )
                cc.resolve_references(contract)
            except RecursionError:
                reasons.append("nesting_too_deep")
            except Exception as e:  # pragma: no cover - final safety net
                reasons.append(f"contract_build_failed: {e}")
            reasons.append(f"contract_parse_failed: {err}")
            # Finalization: stdlib fallback path (no raw lexemes,
            # quote_style omitted, never fabricated)
            _finalize_p1_addenda(contract, text)
            return contract

        # Local recovery: complete subtrees first → degraded
        # key-value pairs (nothing fabricated — every recovered item
        # is flagged recovered, never asserted as a complete fact;
        # degraded pairs have no key token position → the entry
        # takes the value's line)
        try:
            rec_spans, rec_pairs, strategy = _recover_partial(text)
        except Exception:  # pragma: no cover - defensive guard
            rec_spans, rec_pairs, strategy = [], [], "none"
        if rec_spans or rec_pairs:
            n_ent = len(contract["entries"])
            n_cont = len(contract["containers"])
            try:
                for prefix, node in rec_spans:
                    self._walk_node(node, list(prefix), None, contract)
                for entry_path, key, vnode in rec_pairs:
                    # rec_pairs' entry_path already includes the key (assembled by the recovery layer)
                    self._emit_scalar_entry(
                        vnode, entry_path, key, None,
                        False, None, [], [], contract,
                    )
                cc.resolve_references(contract)
            except RecursionError:
                reasons.append("nesting_too_deep")
            except Exception as e:  # pragma: no cover - final safety net
                reasons.append(f"contract_build_failed: {e}")
            for ent in contract["entries"][n_ent:]:
                ent["recovered"] = True
            for cont in contract["containers"][n_cont:]:
                cont["recovered"] = True
            n_rec = len(contract["entries"]) - n_ent
            contract["parse_recovery"] = {
                "strategy": strategy,
                "recovered_entries": n_rec,
            }
            reasons.append(
                f"partial_recovery:{n_rec} "
                f"entr{'y' if n_rec == 1 else 'ies'} recovered "
                f"({strategy}); document incomplete — recovered items "
                "flagged, not asserted as complete facts"
            )

        # JSON5 feature flagging (both strict and jsonc failed)
        feats = _detect_json5_extensions(text)
        if feats:
            contract["detected_format"] = "json5_candidate"
            contract["reason_code"] = "NON_JSONC_EXTENSION_DETECTED"
            reasons.append(
                "NON_JSONC_EXTENSION_DETECTED:"
                + ",".join(sorted(feats))
            )
        reasons.append(f"contract_parse_failed: {err}")
        # Finalization: local-recovery path (the recovered node set
        # is incomplete, quote_style omitted, never fabricated;
        # indent/trailing are whole-text facts and still available)
        _finalize_p1_addenda(contract, text)
        return contract

    def _walk(self, node, path, key, entries, containers, doc_index=0):
        if isinstance(node, dict):
            duplicates = getattr(node, "duplicates", {})
            containers.append(cc.make_container(
                path, "dict",
                child_count=len(node),
                duplicate=bool(duplicates),
                doc_index=doc_index,
            ))
            for k, v in node.items():
                dup = k in duplicates
                chain = None
                if dup:
                    # duplicates[k] = the first n-1 overwritten
                    # occurrences (in order); the dict's current
                    # value = the n-th (the last_wins winner)
                    chain = [
                        {"value": self._chain_value(occ), "line": None}
                        for occ in duplicates[k]
                    ] + [{"value": self._chain_value(v), "line": None}]
                self._walk_value(
                    v, path + [str(k)], str(k), dup, chain,
                    entries, containers, doc_index,
                )
        elif isinstance(node, list):
            containers.append(cc.make_container(
                path, "list",
                child_count=len(node),
                doc_index=doc_index,
            ))
            for idx, item in enumerate(node):
                self._walk_value(
                    item, path + [str(idx)], key, False, None,
                    entries, containers, doc_index,
                )

    def _walk_value(self, node, path, key, dup, chain, entries, containers, doc_index):
        if isinstance(node, (dict, list)):
            # Container-value override chain: the chain was built by
            # the parent layer (if duplicated)
            self._walk(node, path, key, entries, containers, doc_index)
            # A duplicated container gets the chain attached to the
            # just-recorded container
            if dup and chain:
                for c in reversed(containers):
                    if c["path"] == path:
                        c["duplicate"] = True
                        c["override_chain"] = chain
                        break
        else:
            state = cc.VALUE_STATE_EXPLICIT_NULL if node is None else cc.VALUE_STATE_SCALAR
            raw = json.dumps(node, ensure_ascii=False) if node is not None else "null"
            entries.append(cc.make_entry(
                path, key, raw, node,
                value_state=state,
                duplicate=dup, override_chain=chain,
                doc_index=doc_index,
            ))

    @staticmethod
    def _chain_value(value):
        if isinstance(value, dict):
            return {"kind": "dict", "child_count": len(value)}
        if isinstance(value, list):
            return {"kind": "list", "child_count": len(value)}
        return value

    # ========================================
    # In-house parser node-tree walk (positions / raw / comment bindings / precision)
    # ========================================

    def _walk_node(self, node, path, key, contract, doc_index=0):
        """Walk the positioned node tree (object / array / scalar roots)."""
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
                # Prototype-pollution key disclosure (evidence-only: disclosed, not blocked or rewritten)
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
                chain = None
                if dup:
                    chain = [
                        {"value": _chain_value_node(v), "line": kt.line}
                        for kt, v, _l, _i in lst
                    ]
                self._walk_node_value(
                    vnode, path + [k], k, ktok, dup, chain,
                    leading, inline, contract, doc_index,
                )
        elif node.kind == "array":
            desc, desc_lines = _description_from(node.trailing, [])
            contract["containers"].append(cc.make_container(
                path, "list",
                child_count=len(node.items),
                line_start=node.line, line_end=node.end_line,
                description=desc, description_lines=desc_lines,
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
                )
        else:
            self._emit_scalar_entry(
                node, path, key, None, False, None, [], [],
                contract, doc_index,
            )

    def _walk_node_value(
        self, node, path, key, ktok, dup, chain,
        leading, inline, contract, doc_index=0,
    ):
        """Object member / array element value: scalar → entry; container → recurse + bind."""
        if node.kind == "scalar":
            self._emit_scalar_entry(
                node, path, key, ktok, dup, chain,
                leading, inline, contract, doc_index,
            )
            return
        self._walk_node(node, path, key, contract, doc_index)
        pstr = cc.path_str(path)
        if dup and chain:
            for c in reversed(contract["containers"]):
                if c["path_str"] == pstr:
                    c["duplicate"] = True
                    c["override_chain"] = chain
                    break
        desc, desc_lines = _description_from(leading, inline)
        if desc:
            for c in reversed(contract["containers"]):
                if c["path_str"] == pstr:
                    if c["description"] is None:
                        c["description"] = desc
                        c["description_lines"] = desc_lines
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
        """Scalar entry (raw = source lexeme; position = key line first; precision keys)."""
        desc, desc_lines = _description_from(leading, inline)
        value = node.value
        state = (
            cc.VALUE_STATE_EXPLICIT_NULL if value is None
            else cc.VALUE_STATE_SCALAR
        )
        vt = _value_type_of(value)
        prec = None
        if isinstance(value, int) and not isinstance(value, bool):
            prec = "exact"  # Python arbitrary-precision int
        elif isinstance(value, float) and not (
            math.isnan(value) or math.isinf(value)
        ):
            prec = "double"  # IEEE 754 double
        entry = cc.make_entry(
            path, key, node.raw, value,
            value_state=state,
            line_start=(ktok.line if ktok is not None else node.line),
            line_end=node.end_line,
            description=desc, description_lines=desc_lines,
            duplicate=dup, override_chain=chain,
            doc_index=doc_index,
            value_type=vt, type_source="lexical" if vt else None,
            numeric_precision=prec,
        )
        contract["entries"].append(entry)
        # pointer/$ref bypass detection (whole-value shapes; the
        # family resolve_references channel yields the five states,
        # zero pollution of the pattern table)
        ref = _detect_json_pointer_ref(key, value)
        if ref is not None:
            entry["references"].append(ref)
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

    def parse(self, file_path):
        """Parse a JSON file into a Document (with the config contract metadata)."""
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "json", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            # json parse (strict first, JSONC dialect tolerance recorded honestly)
            ok, structured_data, dialect, relax_stats, error = _loads_with_dialect(text)
            metadata["json_dialect"] = dialect
            if ok:
                metadata["parse_success"] = True
                if relax_stats is not None:
                    metadata["json_relaxed"] = relax_stats
                    metadata["warnings"].append(
                        "jsonc dialect relaxed: stripped "
                        f"{relax_stats['comments_stripped']} comment(s), "
                        f"{relax_stats['trailing_commas_removed']} trailing comma(s)"
                    )
                # NaN/Infinity silent-acceptance disclosure (metadata layer)
                if "special_numbers" in _detect_json5_extensions(text):
                    metadata["detected_format"] = "json5_candidate"
                    metadata["format_mismatch"] = True
                    metadata["reason_code"] = "NON_JSONC_EXTENSION_DETECTED"
                    metadata["warnings"].append(
                        "non-standard number literal (NaN/Infinity) "
                        "accepted by python json superset; not RFC 8259"
                    )
            else:
                # Multi-root json_sequence (metadata layer;
                # structured_data keeps the json.loads-equivalent
                # _error, no merging)
                seq_docs, _seq_stats = _load_json_sequence(text)
                if seq_docs is not None:
                    metadata["detected_format"] = "json_sequence"
                    metadata["document_count"] = len(seq_docs)
                    metadata["reason_code"] = "MULTIPLE_ROOT_VALUES"
                    metadata["warnings"].append(
                        f"multiple root values: {len(seq_docs)} documents "
                        "(json_sequence); not merged into an array"
                    )
                else:
                    feats = _detect_json5_extensions(text)
                    if feats:
                        metadata["detected_format"] = "json5_candidate"
                        metadata["reason_code"] = "NON_JSONC_EXTENSION_DETECTED"
                structured_data = {"_error": str(error)}
                metadata["parse_success"] = False
                metadata["warnings"].append("json parse failed")

            # metadata enrichment
            if isinstance(structured_data, dict):
                metadata["top_level_keys"] = list(structured_data.keys())
            elif isinstance(structured_data, list):
                metadata["top_level_type"] = "list"
                metadata["list_length"] = len(structured_data)

            # ConfigEntry contract (fact layer; failure never blocks
            # the parse; positions / raw / comments / precision —
            # jsonc_expected is decided by the file suffix)
            metadata["config_entries"] = self._build_contract(
                text,
                jsonc_expected=str(file_path).lower().endswith(".jsonc"),
            )

            # package.json embedded tool config recognition
            # (eslintConfig/babel top-level keys →
            # embedded_dotfile_configs; independent sources are not
            # merged; plain JSON gets no keys, zero drift. Guarded
            # lazy import, failure never blocks the main parse.)
            try:
                from provendoc.core.parsers.config.dotfile_json_yaml_parser import (
                    attach_package_json_embedded_configs,
                )
                attach_package_json_embedded_configs(
                    metadata, structured_data, file_path,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug(
                    "package.json embedded config scan skipped: %s", exc,
                )

            # Object-level unresolved registry: derived from the
            # contract's reference tri-states / last_wins shadowed
            # occurrences (existing fact sources); original keys are
            # never rewritten, only non-empty writes; guarded silent
            # degradation, never blocking the main parse.
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
    # Multi-file overlay merge (isomorphic to the yaml
    # parse_many_merged)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Multi-file overlay merge: later files shadow earlier
        ones (de-facto market semantics).

        Each file goes through the existing single-file contract
        channel (with the full disclosure semantics); the core
        overlay merge converges in the shared helper
        ``cc.merge_contracts``. The json-family tail steps stay in
        this parser: cross-file schema/hints propagation (the later
        declaration wins; structural expectations re-run on the
        merged entries/containers) + key re-runs (path_display
        re-attached, sensitive recount; style omitted — across
        files there is no single original text to be faithful to)
        + structured_data dict deep-merge (lists replaced whole,
        later wins).

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
                "json", cc.DUP_LAST_WINS)
            contracts.append(c)
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })

        # Core overlay merge converges in the shared helper
        # (behavioral equivalence guarded by existing panels +
        # full regression)
        merged = cc.merge_contracts("json", contracts, sources)
        merged_entries = merged["entries"]
        merged_containers = merged["containers"]

        # Merge-layer schema disclosure is factually recomputed by
        # apply_schema_status below (single owner) — per-file
        # pass-through unverifiable reasons are stripped first,
        # otherwise union + re-run append would double them, and
        # losing declarations would leak in, contradicting the
        # header.
        merged["loss"]["reasons"] = [
            r for r in merged["loss"]["reasons"]
            if not r.startswith("schema_declared_unverifiable:")
        ]

        # Cross-file schema/hints propagation (the later declaration
        # wins; structural expectations re-run on the merged
        # entries/containers — consistent with last_file_wins)
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

        # Key re-runs (style omitted — no single original text across files)
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
