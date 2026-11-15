# ============================================================
# Structured Config Domain
# HCL Native Parser (.tf / .tfvars)
# ============================================================
"""HCL Native parser (.tf / .tfvars / .tftest.hcl).

Handwritten recursive-descent HCL2 native parser with zero
external dependencies.

Syntax facts:

- Block: ``<block_type> <label>... { body }``; labels are
  quoted strings or bare identifiers; a body is nested blocks
  and attributes interleaved;
- Attribute: ``<name> = <expression>``;
- Expressions (structure recovery only, never evaluated):
  - literals string / number / bool / null;
  - object ``{ k = v, ... }`` / list ``[e, ...]`` (recursive);
  - bare traversal references (``var.ami`` /
  ``aws_instance.app.id`` / ``local.config["name"]``) —
  emitted as contract references with value unknown;
  - template strings (``"${...}"`` interpolation — shared
  detect_references);
  - heredocs (``<<`` / ``<<-`` / ``<<'EOF'`` literal forms);
  - any other complex expression (operators / conditionals /
    function calls / for / splat / dynamic) is preserved
    verbatim as raw with value unknown and
    ``expression_kind="expression"`` — evaluation is never
    guessed;
- Comments: ``#`` / ``//`` line comments + ``/* */`` block
  comments; leading comments bind to the following
  attribute/block, inline comments bind to the current
  attribute, EOF orphan comments get bound=None;
- Duplicate attribute (same body) → ``DUPLICATE_ATTRIBUTE``
  (Terraform semantics reject it), duplicate block (same type
  same labels) → ``DUPLICATE_BLOCK`` registered +
  ``duplicate=True``; parsing is never aborted;
- Recovery strategy: block-level local recovery — a broken
  single attribute expression skips to the expression
  boundary and continues, unclosed block/string/heredoc get
  recorded as codes and closed as best as possible; one bad
  block never abandons the other blocks in the same file.

Security boundary: zero execution, zero extra IO — no
functions / providers / modules / backends / provisioners /
commands are executed, no external files are read
(``file("x")`` stays raw), no network access, no process
environment reads (TF_VAR_*).

Value-state separation: ``value=None + value_type="null"`` =
known_null (literal null); ``value=None + value_type=``
"unknown" = statically unknowable (references / complex
expressions).
"""

import bisect
import logging
import re
import time
from pathlib import Path

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)
from provendoc.core.parsers.config.terraform_profile import (
    build_terraform_profile,
)

_log = logging.getLogger(__name__)

# HCL identifier: letter/underscore start, may contain
# digits/hyphens/underscores
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# Numbers: integer / decimal / scientific notation (a leading minus
# is consumed only at expression start)
_NUMBER_RE = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")

# Operator first characters (a traversal followed by one of these
# → complex expression preserved verbatim)
_OPERATOR_START = frozenset("+-*/%<>=!&|?:")

# Depth guardrail (DEPTH_LIMIT_EXCEEDED; defense against
# pathological nesting)
_MAX_DEPTH = 64

# Escape map (HCL double-quoted strings; \uNNNN / \UNNNNNNNN are
# handled separately)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


class _Diag:
    """Honest-failure counters (reason code → count; zero hits,
    zero keys)."""

    def __init__(self):
        self.counts: dict = {}
        self.extra: list = []  # non-counting reasons (insertion order kept)

    def hit(self, code: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1

    def reasons(self) -> list:
        out = []
        for code, n in self.counts.items():
            out.append(f"{code}:{n}")
        out.extend(self.extra)
        return out


class _Scanner:
    """Character-level scanner (pos → 0-based line via
    line_starts bisection)."""

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.line_starts = [0]
        for idx, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(idx + 1)

    def line_of(self, pos: int) -> int:
        return bisect.bisect_right(self.line_starts, pos) - 1

    def peek(self, offset: int = 0) -> str:
        p = self.pos + offset
        return self.text[p] if p < self.n else ""

    def skip_inline_ws(self) -> None:
        while self.pos < self.n and self.text[self.pos] in " \t":
            self.pos += 1

    def skip_ws(self) -> None:
        """Skip all whitespace (including newlines)."""
        while self.pos < self.n and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def at_comment(self) -> bool:
        ch = self.peek()
        if ch in ("#",):
            return True
        if ch == "/" and self.peek(1) in ("/", "*"):
            return True
        return False

    def consume_comment(self):
        """Consume one comment; returns (line_start, line_end, text,
        raw, style).

        style: "line" (# or //) | "block" (/* */, may span lines).
        An unclosed block comment is consumed to EOF (kept
        verbatim — no UNCLOSED code is registered here).
        """
        start = self.pos
        ls = self.line_of(start)
        if self.peek() == "#" or self.peek(1) == "/":
            while self.pos < self.n and self.text[self.pos] != "\n":
                self.pos += 1
            raw = self.text[start:self.pos]
            style = "line"
            text = raw.lstrip("#/").strip()
        else:  # /* */
            self.pos += 2
            while self.pos < self.n:
                if self.text[self.pos] == "*" and self.peek(1) == "/":
                    self.pos += 2
                    break
                self.pos += 1
            raw = self.text[start:self.pos]
            style = "block"
            text = raw[2:-2].strip() if raw.endswith("*/") else raw[2:].strip()
        return ls, self.line_of(max(self.pos - 1, start)), text, raw, style


class HclParser(BaseParser):
    """HCL Native ``.tf`` / ``.tfvars`` parser (config domain).

    Both suffixes share this parser; the file identity is
    disclosed via ``metadata["hcl_profile"]`` (terraform
    configuration vs tfvars variable definitions; a top-level
    block in ``.tfvars`` records ``INVALID_TFVARS_BLOCK`` but
    the structure is kept as-is).
    """

    _FORMAT = "hcl"
    file_type = "hcl"

    def __init__(self, encoding: str = "utf-8-sig"):
        """Initialize the HCL parser.

        Args:
            encoding: source file encoding, defaults to
                ``utf-8-sig`` (tolerates a BOM prefix).
        """
        self.encoding = encoding

    # ==================================
    # entry point
    # ==================================

    def parse(self, file_path) -> Document:
        """Parse an HCL file into a Document (see the class
        docstring)."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path, self._FORMAT, self.encoding, True,
            )
            text = ctx.text
            metadata = ctx.metadata

            file_name = Path(file_path).name
            is_tfvars = file_name.lower().endswith(".tfvars")
            # .tftest.hcl is HCL native syntax, so it reuses
            # this parser; its identity is disclosed via the
            # profile file_role=test_configuration.
            is_tftest = file_name.lower().endswith(".tftest.hcl")

            tree, diag = self._parse_document(text, is_tfvars)

            metadata["parse_success"] = not any(
                k.startswith(("INVALID", "UNCLOSED", "TRUNCATED", "DEPTH"))
                for k in diag.counts
            )
            metadata["top_level_keys"] = [
                item["name"] for item in tree["items"]
            ]
            metadata["structured_data"] = self._structured_data(tree)

            metadata["config_entries"] = self._build_contract(
                tree, diag, file_name=file_name, is_tfvars=is_tfvars,
            )

            # Object-level unresolved registry (family convention;
            # guarded silent degradation)
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            # File identity disclosure; the tftest identity takes
            # priority over configuration.
            if is_tftest:
                _domain_profile = "terraform_tftest"
                _file_kind = "test_configuration"
            elif is_tfvars:
                _domain_profile = "terraform_tfvars"
                _file_kind = "variable_definitions"
            else:
                _domain_profile = "terraform"
                _file_kind = "configuration"
            metadata["hcl_profile"] = {
                "format_family": "hcl",
                "syntax": "native",
                "hcl_version": 2,
                "domain_profile": _domain_profile,
                "file_kind": _file_kind,
                "profile_source": "extension_and_project_context",
            }

            # Terraform profile semantics pass (separate module,
            # pure disclosure, zero evaluation zero IO; guarded
            # hookup — a failure degrades silently and leaves the
            # contract chain untouched). Its codes land in
            # profile["loss_codes"], so contract loss.reasons is
            # not drifted.
            try:
                metadata["terraform_profile"] = build_terraform_profile(
                    tree, metadata["config_entries"],
                    file_name, is_tfvars,
                    source_variant="native", is_tftest=is_tftest,
                )
                file_role = (
                    metadata["terraform_profile"].get("file_role")
                )
                if file_role:
                    metadata["hcl_profile"]["file_role"] = file_role
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("terraform profile pass skipped: %s", exc)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text, metadata=metadata, source_text=ctx.source_text,
            )
        except Exception as e:
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                    "structured_data": {"_error": str(e)},
                },
            )

    # ==================================
    # tree parsing (recursive descent)
    # ==================================

    def _parse_document(self, text: str, is_tfvars: bool):
        s = _Scanner(text)
        diag = _Diag()
        tree = self._parse_body(s, depth=0, diag=diag, top=True,
                                is_tfvars=is_tfvars)
        if s.pos < s.n and s.peek() != "":
            # the top-level body stopping before EOF means leftover
            # garbage (INVALID_HCL already recorded)
            pass
        return tree, diag

    def _parse_body(self, s: _Scanner, depth: int, diag: _Diag,
                    top: bool = False, is_tfvars: bool = False):
        """Parse a body (blocks and attributes interleaved) up to
        '}' / EOF.

        Returns {"items": [...], "leading_comments": [...]};
        items are attr nodes or block nodes (see _parse_attribute
        / _parse_block).
        """
        if depth > _MAX_DEPTH:
            diag.hit("DEPTH_LIMIT_EXCEEDED")
            # swallow up to the matching '}' or EOF (conservative
            # recovery)
            self._skip_to_brace(s)
            return {"items": [], "leading_comments": []}

        items = []
        pending_comments = []  # leading comments (bind to the next item)
        while True:
            s.skip_inline_ws()
            if s.pos >= s.n:
                break
            ch = s.peek()
            if ch == "\n" or ch == "\r":
                s.pos += 1
                continue
            if s.at_comment():
                pending_comments.append(s.consume_comment())
                continue
            if ch == "}":
                # consumed by the caller (block/object); at top
                # level it is a garbage close brace
                if top:
                    diag.hit("INVALID_HCL")
                    s.pos += 1
                    continue
                break
            # identifier / string / number start
            m = _IDENT_RE.match(s.text, s.pos)
            if m:
                items.append(self._parse_after_ident(
                    s, m, depth, diag, pending_comments, top, is_tfvars,
                ))
                pending_comments = []
                continue
            if ch == '"':
                # a top-level bare string has no semantics (not a
                # label context) → skip-line recovery
                diag.hit("INVALID_HCL")
                self._skip_line(s)
                continue
            if ch.isdigit() or (ch == "." and s.peek(1).isdigit()):
                # a top-level bare number (a value without '=')
                # → MISSING_ASSIGNMENT
                diag.hit("MISSING_ASSIGNMENT")
                self._skip_line(s)
                continue
            diag.hit("INVALID_HCL")
            s.pos += 1  # skip a single garbage char (minimal recovery step)
        # remaining pending = orphan comments (before EOF / '}'),
        # bound=None at the contract layer
        return {"items": items, "leading_comments": pending_comments}

    def _parse_after_ident(self, s, m, depth, diag, pending_comments,
                           top, is_tfvars):
        """Dispatch after an identifier: attribute ('=') or block
        (label(s) + '{')."""
        name = m.group(0)
        name_ls = s.line_of(s.pos)
        s.pos = m.end()
        s.skip_ws()
        ch = s.peek()
        if ch == "=":
            s.pos += 1
            return self._parse_attribute(
                s, name, name_ls, depth, diag, pending_comments,
            )
        if ch in ('"',) or ch == "{" or _IDENT_RE.match(s.text, s.pos):
            return self._parse_block(
                s, name, name_ls, depth, diag, pending_comments,
                top=top, is_tfvars=is_tfvars,
            )
        # a name with no '=' and no body: missing assignment
        diag.hit("MISSING_ASSIGNMENT")
        s.skip_inline_ws()
        if s.peek() == "\n" or s.pos >= s.n:
            return {"kind": "invalid", "name": name,
                    "line_start": name_ls, "line_end": name_ls,
                    "leading_comments": pending_comments}
        return {"kind": "invalid", "name": name,
                "line_start": name_ls, "line_end": name_ls,
                "leading_comments": pending_comments}

    def _parse_attribute(self, s, name, name_ls, depth, diag,
                         pending_comments):
        """``name = expression`` → attr node."""
        s.skip_ws()
        expr = self._parse_expression(s, depth, diag)
        line_end = s.line_of(max(s.pos - 1, s.pos))
        # inline comment (# / // at the end of the expression
        # line; /* */ is not consumed here — a block comment may
        # legally follow the expression, so it goes to the
        # leading channel and is consumed by the next round)
        inline_comment = None
        save = s.pos
        s.skip_inline_ws()
        if s.peek() == "#" or (s.peek() == "/" and s.peek(1) == "/"):
            cm = s.consume_comment()
            inline_comment = cm[2]
        else:
            s.pos = save
        return {
            "kind": "attr",
            "name": name,
            "line_start": name_ls,
            "line_end": max(line_end, name_ls),
            "expr": expr,
            "leading_comments": pending_comments,
            "inline_comment": inline_comment,
        }

    def _parse_block(self, s, block_type, type_ls, depth, diag,
                     pending_comments, top=False, is_tfvars=False):
        """``block_type label... { body }`` → block node."""
        labels = []
        while True:
            s.skip_ws()
            ch = s.peek()
            if ch == '"':
                label, ok = self._scan_string(s, diag)
                labels.append(label if ok else label)
                continue
            lm = _IDENT_RE.match(s.text, s.pos)
            if lm:
                labels.append(lm.group(0))
                s.pos = lm.end()
                continue
            break
        s.skip_ws()
        if s.peek() != "{":
            # no body after the block header (truncated /
            # broken syntax)
            diag.hit("UNCLOSED_BLOCK")
            return {
                "kind": "block", "name": block_type,
                "block_type": block_type,
                "labels": labels, "line_start": type_ls,
                "line_end": s.line_of(max(s.pos - 1, type_ls)),
                "body": {"items": [], "leading_comments": []},
                "leading_comments": pending_comments,
                "unclosed": True,
            }
        s.pos += 1  # consume '{'
        body = self._parse_body(s, depth + 1, diag)
        if s.peek() == "}":
            s.pos += 1
        else:
            # unclosed at EOF (_parse_body returns naturally at
            # EOF)
            diag.hit("UNCLOSED_BLOCK")
        line_end = s.line_of(max(s.pos - 1, type_ls))
        node = {
            "kind": "block", "name": block_type,
            "block_type": block_type, "labels": labels,
            "line_start": type_ls, "line_end": line_end, "body": body,
            "leading_comments": pending_comments,
        }
        if top and is_tfvars:
            # a top-level block in .tfvars = INVALID_TFVARS_BLOCK
            # (structure still kept, no abort)
            node["invalid_tfvars_block"] = True
        return node

    # ==================================
    # expressions (structure only, never evaluate)
    # ==================================

    def _parse_expression(self, s, depth, diag):
        """One expression → {raw, value, value_type,
        expression_kind, refs, heredoc, line_start, line_end}.

        Complex expressions (operators/functions/for/splat) are
        preserved verbatim as raw with value unknown.
        """
        s.skip_ws()  # whitespace before the value (including newlines — HCL expressions may span lines)
        start = s.pos
        ls = s.line_of(start)
        ch = s.peek()

        # heredoc (legal only as an attribute value; recognized
        # here at the expression level)
        if ch == "<" and s.peek(1) == "<":
            return self._parse_heredoc(s, ls, diag)

        if ch == '"':
            raw, value, closed, invalid_esc = self._scan_string_full(s)
            if not closed:
                diag.hit("UNCLOSED_STRING")
            if invalid_esc:
                diag.hit("INVALID_ESCAPE")
            kind = "template" if "${" in value else "literal"
            return {
                "raw": raw, "value": value,
                "value_type": cc.VALUE_TYPE_STRING,
                "expression_kind": kind, "refs": [],
                "line_start": ls, "line_end": s.line_of(max(s.pos - 1, ls)),
            }

        if ch == "{":
            return self._parse_object(s, ls, depth, diag)

        if ch == "[":
            return self._parse_list(s, ls, depth, diag)

        if ch == "-" and (s.peek(1).isdigit()
                          or (s.peek(1) == "." and s.peek(2).isdigit())):
            s.pos += 1  # fold the minus sign into the number

        if ch.isdigit() or ch == "." or (ch == "-" and s.pos != start) \
                or (ch == "-" and s.peek(1).isdigit()):
            nm = _NUMBER_RE.match(s.text, s.pos)
            if nm:
                s.pos = nm.end()
                # operator continuation (e.g. 1 + 2) → complex
                # expression preserved verbatim
                save = s.pos
                s.skip_inline_ws()
                if s.peek() in _OPERATOR_START:
                    s.pos = save
                    return self._consume_complex_expression(
                        s, start, ls, diag)
                s.pos = save
                raw = s.text[start:s.pos]
                if "." in raw or "e" in raw or "E" in raw:
                    return {
                        "raw": raw, "value": float(raw),
                        "value_type": cc.VALUE_TYPE_FLOAT,
                        "expression_kind": "literal", "refs": [],
                        "line_start": ls, "line_end": s.line_of(s.pos - 1),
                    }
                return {
                    "raw": raw, "value": int(raw),
                    "value_type": cc.VALUE_TYPE_INT,
                    "expression_kind": "literal", "refs": [],
                    "line_start": ls, "line_end": s.line_of(s.pos - 1),
                }

        # true / false / null
        m = _IDENT_RE.match(s.text, s.pos)
        if m:
            word = m.group(0)
            if word in ("true", "false"):
                s.pos = m.end()
                return {
                    "raw": word, "value": word == "true",
                    "value_type": cc.VALUE_TYPE_BOOL,
                    "expression_kind": "literal", "refs": [],
                    "line_start": ls, "line_end": s.line_of(s.pos - 1),
                }
            if word == "null":
                s.pos = m.end()
                return {
                    "raw": word, "value": None,
                    "value_type": cc.VALUE_TYPE_NULL,
                    "expression_kind": "literal", "refs": [],
                    "line_start": ls, "line_end": s.line_of(s.pos - 1),
                }
            # function-call detection: an identifier followed
            # (whitespace tolerated) directly by '(' → preserved
            # verbatim as raw, zero evaluation zero IO
            probe = m.end()
            while probe < s.n and s.text[probe] in " \t":
                probe += 1
            if probe < s.n and s.text[probe] == "(":
                return self._consume_complex_expression(s, start, ls, diag)
            # bare traversal: ident (.ident | ["key"])*
            traversal = self._parse_traversal(s, m, diag)
            if traversal is not None:
                refs = [{"syntax": traversal["raw"],
                         "target": traversal["target"],
                         "resolution_status": cc.REF_NOT_APPLICABLE}]
                # traversal followed by an operator → complex
                # expression (preserved verbatim)
                s.skip_inline_ws()
                if s.peek() in _OPERATOR_START:
                    return self._consume_complex_expression(
                        s, start, ls, diag)
                return {
                    "raw": traversal["raw"], "value": None,
                    "value_type": cc.VALUE_TYPE_UNKNOWN,
                    "expression_kind": "reference", "refs": refs,
                    "line_start": ls,
                    "line_end": s.line_of(max(s.pos - 1, ls)),
                }
            # traversal None = splat / complex indexing →
            # complex-expression channel
            return self._consume_complex_expression(s, start, ls, diag)

        # unrecognized expression start: scan to the boundary,
        # keep raw
        return self._consume_complex_expression(s, start, ls, diag)

    def _parse_traversal(self, s, m, diag):
        """Bare reference ``root.attr["key"].attr2``; returns None
        when the identifier is followed by '('."""
        start = m.start()
        s.pos = m.end()
        parts = [m.group(0)]
        while True:
            ch = s.peek()
            if ch == ".":
                nm = _IDENT_RE.match(s.text, s.pos + 1)
                if nm:
                    parts.append(nm.group(0))
                    s.pos = nm.end()
                    continue
                if s.peek(1) == "*":
                    # splat: hand the whole thing to the
                    # complex-expression channel
                    s.pos = start
                    return None
                break
            if ch == "[":
                # index traversal: ["key"] / [0]
                s.pos += 1
                s.skip_ws()
                if s.peek() == '"':
                    key, _ok = self._scan_string(s, diag)
                    s.skip_ws()
                    if s.peek() == "]":
                        s.pos += 1
                        parts.append(key)
                        continue
                else:
                    nm = re.match(r"\d+", s.text[s.pos:])
                    if nm:
                        s.skip_ws()
                        if s.peek() == "]":
                            parts.append(nm.group(0))
                            s.pos += 1
                            continue
                # complex index expression (e.g. [for ...]) →
                # fall back to the complex channel
                s.pos = start
                return None
            break
        return {"raw": s.text[start:s.pos], "target": ".".join(parts)}

    def _consume_complex_expression(self, s, start, ls, diag):
        """Preserve a complex expression verbatim: scan to the
        expression boundary (depth-0 newline / ',' / '}' / ']' /
        EOF), bracket/quote aware. Value unknown, never
        evaluated."""
        depth = 0
        while s.pos < s.n:
            ch = s.text[s.pos]
            if ch == '"':
                self._scan_string(s, diag)
                continue
            if ch == "#" and depth == 0:
                break  # an inline comment is not part of the expression
            if ch == "/" and s.peek(1) == "/" and depth == 0:
                break  # an inline comment is not part of the expression
            if ch in "([{":
                depth += 1
                s.pos += 1
                continue
            if ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
                s.pos += 1
                continue
            if ch == "\n" and depth == 0:
                break
            if ch == "," and depth == 0:
                break
            if ch == "<" and s.peek(1) == "<":
                # mid-expression heredoc (rare but legal Terraform)
                self._parse_heredoc(s, s.line_of(s.pos), diag)
                continue
            s.pos += 1
        raw = s.text[start:s.pos].strip()
        return {
            "raw": raw, "value": None,
            "value_type": cc.VALUE_TYPE_UNKNOWN,
            "expression_kind": "expression", "refs": [],
            "line_start": ls, "line_end": s.line_of(max(s.pos - 1, ls)),
        }

    def _parse_object(self, s, ls, depth, diag):
        """Object literal ``{ k = v, ... }`` → nested attr nodes +
        container value."""
        if depth + 1 > _MAX_DEPTH:
            diag.hit("DEPTH_LIMIT_EXCEEDED")
            self._skip_to_brace(s)
            return {
                "raw": "", "value": {}, "value_type": None,
                "expression_kind": "object", "refs": [],
                "line_start": ls, "line_end": ls, "children": [],
            }
        s.pos += 1  # consume '{'
        obj_start = s.pos - 1
        items = []
        while True:
            s.skip_ws()
            if s.pos >= s.n:
                diag.hit("UNCLOSED_BLOCK")
                break
            if s.peek() == "}":
                s.pos += 1
                break
            if s.at_comment():
                s.consume_comment()
                continue
            if s.peek() == ",":
                s.pos += 1
                continue
            # key: bare identifier / quoted string
            if s.peek() == '"':
                key, _ok = self._scan_string(s, diag)
            else:
                km = _IDENT_RE.match(s.text, s.pos)
                if km:
                    key = km.group(0)
                    if key == "for" and km.end() < s.n:
                        # for-object expression: preserved verbatim
                        # as raw
                        s.pos = km.end()
                        expr = self._consume_complex_expression(
                            s, obj_start, ls, diag)
                        if s.peek() == "}":
                            s.pos += 1
                            expr["raw"] = s.text[obj_start:s.pos].strip()
                            expr["line_end"] = s.line_of(max(
                                s.pos - 1, obj_start))
                        return expr
                    s.pos = km.end()
                else:
                    diag.hit("INVALID_HCL")
                    s.pos += 1
                    continue
            s.skip_ws()
            if s.peek() == "=":
                s.pos += 1
                key_ls = s.line_of(s.pos)
                expr = self._parse_expression(s, depth + 1, diag)
                items.append({
                    "kind": "attr", "name": key,
                    "line_start": key_ls,
                    "line_end": s.line_of(max(s.pos - 1, key_ls)),
                    "expr": expr, "leading_comments": [],
                    "inline_comment": None,
                })
                s.skip_inline_ws()
                if s.peek() == ",":
                    s.pos += 1
                continue
            # bare block inside an object (HCL allows nested
            # blocks in object-constructor context)
            if _IDENT_RE.match(s.text, s.pos) or s.peek() == "{":
                items.append(self._parse_nested_in_object(
                    s, key, diag, depth + 1))
                continue
            diag.hit("MISSING_ASSIGNMENT")
            self._skip_line(s)
        raw = s.text[obj_start:s.pos] if s.pos > obj_start else "{}"
        # container value (scalar subitems are expanded as
        # derived entries by the contract layer)
        value = self._object_value(items)
        return {
            "raw": raw, "value": value, "value_type": None,
            "expression_kind": "object", "refs": [],
            "line_start": ls, "line_end": s.line_of(max(s.pos - 1, ls)),
            "children": items,
        }

    def _parse_nested_in_object(self, s, key, diag, depth):
        """Nested block inside an object (rare: keep structure,
        don't guess semantics)."""
        return self._parse_block(
            s, key, s.line_of(s.pos), depth, diag, [],
        )

    def _object_value(self, items):
        """Object literal → dict value (reference/complex subitems
        are None placeholders, never evaluated — subitem facts
        are carried by the derived entries)."""
        out = {}
        for item in items:
            if item["kind"] != "attr":
                continue
            expr = item.get("expr") or {}
            if expr.get("expression_kind") == "literal":
                out[item["name"]] = expr.get("value")
            else:
                out[item["name"]] = None  # unknown placeholder
        return out

    def _parse_list(self, s, ls, depth, diag):
        """List/tuple literal ``[e, ...]`` (a for expression goes
        to the complex channel)."""
        list_start = s.pos
        s.pos += 1  # consume '['
        elements = []
        # for-expression detection (preserved verbatim)
        s.skip_ws()
        fm = _IDENT_RE.match(s.text, s.pos)
        if fm and fm.group(0) == "for":
            s.pos = fm.end()
            expr = self._consume_complex_expression(
                s, list_start, ls, diag)
            if s.peek() == "]":
                s.pos += 1
                expr["raw"] = s.text[list_start:s.pos].strip()
                expr["line_end"] = s.line_of(max(s.pos - 1, list_start))
            return expr
        while True:
            s.skip_ws()
            if s.pos >= s.n:
                diag.hit("INVALID_HCL")
                break
            if s.peek() == "]":
                s.pos += 1
                break
            if s.at_comment():
                s.consume_comment()
                continue
            if s.peek() == ",":
                s.pos += 1
                continue
            expr = self._parse_expression(s, depth + 1, diag)
            elements.append(expr)
        value = [
            e.get("value") if e.get("expression_kind") == "literal"
            else None
            for e in elements
        ]
        raw = s.text[list_start:s.pos]
        return {
            "raw": raw, "value": value, "value_type": None,
            "expression_kind": "list", "refs": [],
            "line_start": ls, "line_end": s.line_of(max(s.pos - 1, ls)),
            "children": [
                {
                    "kind": "attr", "name": str(i),
                    "line_start": e["line_start"], "line_end": e["line_end"],
                    "expr": e, "leading_comments": [],
                    "inline_comment": None,
                }
                for i, e in enumerate(elements)
            ],
        }

    def _parse_heredoc(self, s, ls, diag):
        """Heredoc ``<<MARK`` / ``<<-MARK`` / ``<<'MARK'``."""
        s.pos += 2  # consume '<<'
        indented = False
        if s.peek() == "-":
            indented = True
            s.pos += 1
        literal = False
        if s.peek() == "'":
            literal = True
            s.pos += 1
        mm = _IDENT_RE.match(s.text, s.pos)
        if not mm:
            # no end marker: swallow to EOF (truncated)
            diag.hit("UNCLOSED_HEREDOC")
            body = s.text[s.pos:]
            s.pos = s.n
            return {
                "raw": "", "value": body,
                "value_type": cc.VALUE_TYPE_STRING,
                "expression_kind": "heredoc", "refs": [],
                "line_start": ls, "line_end": s.line_of(max(s.n - 1, ls)),
                "heredoc": {
                    "delimiter": None, "indented": indented,
                    "interpolation": not literal,
                    "raw_body": body, "closed": False,
                },
            }
        delim = mm.group(0)
        s.pos = mm.end()
        # skip to end of line (the rest of the marker line is
        # discarded — the marker owns the line)
        while s.pos < s.n and s.text[s.pos] != "\n":
            s.pos += 1
        if s.pos < s.n:
            s.pos += 1  # skip the newline
        body_start = s.pos
        closed = False
        while s.pos < s.n:
            line_end = s.text.find("\n", s.pos)
            if line_end == -1:
                line_end = s.n
            line = s.text[s.pos:line_end]
            if line.strip() == delim:
                # <<- allows an indented terminator; << strictly
                # requires column 0 (leniently: a stripped match
                # closes it, indentation differences are kept
                # in raw_body)
                s.pos = line_end + 1 if line_end < s.n else s.n
                closed = True
                break
            s.pos = line_end + 1 if line_end < s.n else s.n
        raw_body = s.text[body_start:s.pos]
        if closed:
            # drop the terminator line
            lines = raw_body.splitlines()
            if lines and lines[-1].strip() == delim:
                lines = lines[:-1]
            raw_body = "\n".join(lines)
        else:
            diag.hit("UNCLOSED_HEREDOC")
        decoded = raw_body
        if indented:
            # <<- minimal-indent stripping (Terraform semantics:
            # drop the common leading whitespace)
            lines = decoded.splitlines()
            indents = [
                len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()
            ]
            cut = min(indents) if indents else 0
            decoded = "\n".join(ln[cut:] if ln.strip() else ln for ln in lines)
        return {
            "raw": f"{'<<-' if indented else '<<'}{delim}",
            "value": decoded,
            "value_type": cc.VALUE_TYPE_STRING,
            "expression_kind": "heredoc", "refs": [],
            "line_start": ls, "line_end": s.line_of(max(s.pos - 1, ls)),
            "heredoc": {
                "delimiter": delim, "indented": indented,
                "interpolation": not literal,
                "raw_body": raw_body, "closed": closed,
            },
        }

    # ==================================
    # string scanning (escape-aware)
    # ==================================

    def _scan_string(self, s, diag):
        """Fast string scan (for labels / object keys); returns
        (text, closed)."""
        raw, value, closed, _esc = self._scan_string_full(s)
        return value, closed

    def _scan_string_full(self, s, diag=None):
        """Full double-quoted string scan.

        Returns (raw, decoded, closed, invalid_escapes);
        escape-aware (\\x escapes are consumed as a pair),
        \\uNNNN / \\UNNNNNNNN are decoded; an unclosed string is
        swallowed to EOF (closed=False, the caller records
        the code); template interpolation ``${...}`` is kept
        in decoded (never evaluated).
        """
        start = s.pos
        s.pos += 1
        out = []
        invalid = 0
        while s.pos < s.n:
            ch = s.text[s.pos]
            if ch == '"':
                s.pos += 1
                raw = s.text[start:s.pos]
                return raw, "".join(out), True, invalid
            if ch == "\\":
                nxt = s.peek(1)
                if nxt == "u" or nxt == "U":
                    width = 4 if nxt == "u" else 8
                    hexs = s.text[s.pos + 2:s.pos + 2 + width]
                    if len(hexs) == width and all(
                            c in "0123456789abcdefABCDEF" for c in hexs):
                        try:
                            out.append(chr(int(hexs, 16)))
                            s.pos += 2 + width
                            continue
                        except ValueError:
                            pass
                    invalid += 1
                    out.append(ch)
                    s.pos += 1
                    continue
                if nxt in _ESCAPES:
                    out.append(_ESCAPES[nxt])
                    s.pos += 2
                    continue
                invalid += 1
                out.append(ch)
                s.pos += 1
                continue
            if ch == "\n":
                # HCL double-quoted strings do not span a bare
                # newline (template directives are the exception
                # — those complex forms appear inline via %{ },
                # treated as unclosed here)
                break
            out.append(ch)
            s.pos += 1
        raw = s.text[start:s.pos]
        return raw, "".join(out), False, invalid

    # ==================================
    # recovery helpers
    # ==================================

    @staticmethod
    def _skip_line(s):
        while s.pos < s.n and s.text[s.pos] != "\n":
            s.pos += 1
        if s.pos < s.n:
            s.pos += 1

    @staticmethod
    def _skip_to_brace(s):
        """Swallow up to the matching '}' / EOF (conservative
        recovery when the depth guardrail trips)."""
        depth = 0
        while s.pos < s.n:
            ch = s.text[s.pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    s.pos += 1
                    return
                depth -= 1
            s.pos += 1

    # ==================================
    # structured_data (retrieval face)
    # ==================================

    def _structured_data(self, tree):
        """Flattened top-level retrieval face (env-shaped; blocks
        recurse one level into sections)."""
        titles = [item["name"] for item in tree["items"]]
        contents = [{"section": "", "items": {}, "tables": []}]
        for item in tree["items"]:
            if item["kind"] == "attr":
                expr = item.get("expr") or {}
                contents[0]["items"][item["name"]] = (
                    expr.get("value") if expr.get("value_type") != cc.VALUE_TYPE_UNKNOWN
                    else (expr.get("raw") or None)
                )
            elif item["kind"] == "block":
                section_items = {}
                for child in item["body"]["items"]:
                    if child["kind"] == "attr":
                        expr = child.get("expr") or {}
                        section_items[child["name"]] = (
                            expr.get("value")
                            if expr.get("value_type") != cc.VALUE_TYPE_UNKNOWN
                            else (expr.get("raw") or None)
                        )
                contents.append({
                    "section": ".".join(
                        [item["block_type"]] + item["labels"]),
                    "items": section_items,
                    "tables": [],
                })
        return {
            "titles": titles,
            "contents": contents,
            "tables": [],
            "structure": contents[0]["items"],
        }

    # ==================================
    # config entry contract
    # ==================================

    def _build_contract(self, tree, diag, file_name=None, is_tfvars=False):
        """Tree → config_entry_v1.1 contract (fully aligned with
        family conventions)."""
        contract = cc.build_contract(
            self._FORMAT, cc.DUP_REJECTED,
            line_numbers=True, comments=True,
        )
        reasons = contract["loss"]["reasons"]
        entries = contract["entries"]
        containers = contract["containers"]
        comments = contract["comments"]

        block_seen: dict = {}  # (path_str) → container (duplicate block detection)

        def flush_leading(pending, bound_path, bound_kind):
            """Write leading comments bound to their target into
            the comments channel."""
            for cm in pending or []:
                ls, le, text, raw, style = cm
                comments.append(cc.make_comment(
                    "block" if style == "block" else "block",
                    text, raw, ls, le,
                    bound_path=bound_path, bound_kind=bound_kind,
                ))

        def emit_attr(item, path_prefix, entry_map,
                      inherited_duplicate=False):
            name = item["name"]
            expr = item.get("expr") or {}
            full_path = list(path_prefix) + [name]
            path_s = cc.path_str(full_path)
            # leading-comment binding + description
            desc = None
            desc_lines = None
            if item.get("leading_comments"):
                texts = [c[2] for c in item["leading_comments"] if c[2]]
                if texts:
                    desc = "\n".join(texts)
                    desc_lines = [
                        item["leading_comments"][0][0],
                        item["leading_comments"][-1][1],
                    ]
                flush_leading(item["leading_comments"], full_path, "key")
            # inline comment
            if item.get("inline_comment"):
                comments.append(cc.make_comment(
                    "inline", item["inline_comment"],
                    " # " + item["inline_comment"],
                    item["line_start"], item["line_end"],
                    bound_path=full_path, bound_kind="key",
                ))
            # references: bare traversals + ${} templates in
            # strings (shared detection)
            refs = list(expr.get("refs") or [])
            raw_val = expr.get("raw")
            if isinstance(raw_val, str):
                for r in cc.detect_references(raw_val):
                    if r["syntax"] not in {x["syntax"] for x in refs}:
                        refs.append(r)
            if path_s in entry_map:
                # a duplicate attribute in the same body is a
                # Terraform parse error
                existing = entry_map[path_s]
                existing["duplicate"] = True
                if existing.get("override_chain") is None:
                    existing["override_chain"] = []
                existing["override_chain"].append({
                    "value": raw_val, "line": item["line_start"],
                })
                diag.hit("DUPLICATE_ATTRIBUTE")
                return
            value_state = cc.VALUE_STATE_SCALAR
            if expr.get("expression_kind") == "object":
                value_state = cc.VALUE_STATE_CONTAINER
            elif expr.get("expression_kind") == "list":
                value_state = cc.VALUE_STATE_CONTAINER
            entry = cc.make_entry(
                full_path, name, raw_val, expr.get("value"),
                value_state=value_state,
                line_start=item["line_start"],
                line_end=item["line_end"],
                description=desc, description_lines=desc_lines,
                value_type=expr.get("value_type"),
                duplicate=inherited_duplicate,
            )
            entry["expression_kind"] = expr.get("expression_kind")
            if expr.get("heredoc"):
                entry["heredoc"] = dict(expr["heredoc"])
            if refs:
                entry["references"] = refs
            entries.append(entry)
            entry_map[path_s] = entry
            # inline-container derived expansion (line_only
            # projection)
            self._emit_derived(expr, full_path, entry, entries, containers)
            return entry

        def emit_block(item, path_prefix, entry_map, depth=0,
                       inherited_duplicate=False):
            if depth > _MAX_DEPTH:
                return
            labels = item.get("labels") or []
            full_path = list(path_prefix) + [item["block_type"]] + labels
            path_s = cc.path_str(full_path)
            flush_leading(item.get("leading_comments"), full_path, "section")
            body = item.get("body") or {}
            is_duplicate = inherited_duplicate
            if not inherited_duplicate:
                if path_s in block_seen:
                    existing = block_seen[path_s]
                    existing["duplicate"] = True
                    diag.hit("DUPLICATE_BLOCK")
                    is_duplicate = True
                else:
                    container = cc.make_container(
                        full_path, "dict",
                        line_start=item["line_start"],
                        line_end=item["line_end"],
                    )
                    if item.get("unclosed"):
                        container["unclosed"] = True
                    if item.get("invalid_tfvars_block"):
                        container["invalid_tfvars_block"] = True
                        diag.hit("INVALID_TFVARS_BLOCK")
                    containers.append(container)
                    block_seen[path_s] = container
            # duplicate block subtree: DUPLICATE_BLOCK is not
            # recorded again (the root cause is in the ancestor);
            # child entries keep their structure +
            # duplicate=True
            child_map = {}
            for child in body.get("items", []):
                if child["kind"] == "attr":
                    emit_attr(child, full_path, child_map,
                              inherited_duplicate=is_duplicate)
                elif child["kind"] == "block":
                    emit_block(child, full_path, child_map, depth + 1,
                               inherited_duplicate=is_duplicate)
                # invalid nodes are skipped (diag already
                # recorded)
            for child in body.get("leading_comments", []):
                flush_leading([child], None, None)

        entry_map = {}
        for item in tree["items"]:
            if item["kind"] == "attr":
                emit_attr(item, [], entry_map)
            elif item["kind"] == "block":
                emit_block(item, [], entry_map)
        # top-level orphan comments (EOF pending)
        flush_leading(tree.get("leading_comments"), None, None)

        cc.fill_child_counts(entries, containers)
        reasons.extend(diag.reasons())
        # family convention: sensitive disclosure (key-name +
        # value-pattern channels, no keys when nothing hits)
        sensitive_count = cc.scan_sensitive_entries(entries)
        if sensitive_count:
            reasons.append(f"sensitive_entry_values:{sensitive_count}")
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")
        # dialect capability declaration (additive top-level
        # keys)
        contract["dialect"] = {
            "syntax": "native",
            "hcl_version": 2,
            "scope": "p0_core",
            "expressions": "structure_only_never_evaluated",
            "comments": "#-//-and-block",
            "heredoc": "template-and-literal",
            "duplicate_policy": "attributes_rejected_blocks_flagged",
        }
        return contract

    def _emit_derived(self, expr, parent_path, parent_entry,
                      entries, containers):
        """Derived-entry expansion of inline object/list
        (line_only)."""
        kind = expr.get("expression_kind")
        if kind == "object":
            children = expr.get("children") or []
            child_paths = []
            for child in children:
                if child["kind"] != "attr":
                    continue
                cexpr = child.get("expr") or {}
                cpath = list(parent_path) + [child["name"]]
                centry = cc.make_entry(
                    cpath, child["name"], cexpr.get("raw"),
                    cexpr.get("value"),
                    value_state=(
                        cc.VALUE_STATE_CONTAINER
                        if cexpr.get("expression_kind") in ("object", "list")
                        else cc.VALUE_STATE_SCALAR
                    ),
                    line_start=parent_entry["line_start"],
                    line_end=parent_entry["line_end"],
                    source_precision=cc.SOURCE_PRECISION_LINE_ONLY,
                    value_type=cexpr.get("value_type"),
                )
                centry["expression_kind"] = cexpr.get("expression_kind")
                if cexpr.get("refs"):
                    centry["references"] = list(cexpr["refs"])
                entries.append(centry)
                child_paths.append(cpath)
                self._emit_derived(cexpr, cpath, centry, entries, containers)
            containers.append(cc.make_container(
                parent_path, "dict",
                line_start=parent_entry["line_start"],
                line_end=parent_entry["line_end"],
                implicit=True,
            ))
        elif kind == "list":
            children = expr.get("children") or []
            for child in children:
                cexpr = child.get("expr") or {}
                cpath = list(parent_path) + [child["name"]]
                centry = cc.make_entry(
                    cpath, child["name"], cexpr.get("raw"),
                    cexpr.get("value"),
                    value_state=(
                        cc.VALUE_STATE_CONTAINER
                        if cexpr.get("expression_kind") in ("object", "list")
                        else cc.VALUE_STATE_SCALAR
                    ),
                    line_start=parent_entry["line_start"],
                    line_end=parent_entry["line_end"],
                    source_precision=cc.SOURCE_PRECISION_LINE_ONLY,
                    value_type=cexpr.get("value_type"),
                )
                centry["expression_kind"] = cexpr.get("expression_kind")
                if cexpr.get("refs"):
                    centry["references"] = list(cexpr["refs"])
                entries.append(centry)
                self._emit_derived(cexpr, cpath, centry, entries, containers)
            containers.append(cc.make_container(
                parent_path, "list",
                line_start=parent_entry["line_start"],
                line_end=parent_entry["line_end"],
                implicit=True,
            ))
