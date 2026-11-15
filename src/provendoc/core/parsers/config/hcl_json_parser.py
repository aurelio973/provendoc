# ============================================================
# Structured Config Domain
# HCL JSON Parser (.tf.json / .tfvars.json / .tftest.json)
# ============================================================
"""HCL JSON variant parser (``.tf.json`` / ``.tfvars.json`` /
``.tftest.json``).

After JSON decoding, values are mapped to a tree isomorphic to the
native parser's (block/labels/attribute/expr node shapes match),
then the whole chain reuses ``HclParser``'s contract layer
(``_build_contract``) and semantic layers
(``build_terraform_profile`` + ``build_expression_semantics``);
the semantic passes apply to the JSON variant directly, with only
``source_variant="json"`` disclosing the source difference.

Mapping rules (schema-less static basis, zero IO, zero execution):

- Known Terraform block types (``_BLOCK_LABEL_ARITY``) → block
  nodes; each nesting level of object keys = one label (arity
  levels), the leaf object = body; multiple keys at the same
  level, or an array value whose elements are all objects →
  multiple blocks of the same type (duplicate blocks are coded
  DUPLICATE_BLOCK by the contract layer per family convention).
- Objects / object-array values under unknown keys → **attribute
  values** (object/list expr, raw preserved) — Terraform JSON's
  block-vs-attribute ambiguity is inherently schema-dependent;
  without a schema the structure is preserved as an attribute
  (the decision is disclosed via ``hcl_profile.mapping_decisions``;
  a schema is never guessed).
- ``.tfvars.json`` top level is always attributes (top-level keys
  are variable assignments).
- A string value containing ``${}`` → template; a whole-string
  single interpolation (``"${expr}"``) →
  ``expression_kind="expression"`` (Terraform JSON semantics: a
  single-interpolation string is the expression itself), value
  unknown, raw preserved verbatim (quotes included), zero
  evaluation.
- Numbers / booleans / null → literals; line numbers come from
  the position-aware JSON lexer (zero-drift raw spans, also used
  for verbatim raw preservation).

Coding: JSON syntax errors = INVALID_HCL_JSON on this parser's
contract channel (the JSON counterpart of native INVALID_HCL);
mapping-layer structural anomalies = INVALID_TF_JSON_ROOT (root
not an object) / HCL_JSON_BLOCK_SHAPE_MISMATCH (non-object at a
label level) land in profile ``loss_codes``; both are registered
in vocab_registry.

Security boundary: zero execution, zero extra IO — functions /
providers / modules / provisioners / apply inside tests are
never executed, external files are never read, no network, no
process environment.
"""

from __future__ import annotations

import bisect
import logging
import re
import time
from pathlib import Path
from typing import Any, NoReturn

from provendoc.base.models.document import Document
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)
from provendoc.core.parsers.config.hcl_parser import HclParser, _Diag
from provendoc.core.parsers.config.terraform_profile import (
    build_terraform_profile,
)

_log = logging.getLogger(__name__)

# ---- block type → label arity (Terraform language structure
# knowledge, not schema acquisition) ----
# Aligned with the native `_KNOWN_BLOCK_TYPES` + nested blocks +
# tftest. Label levels = the nested object levels following the
# block type (resource: type.name = two levels).
_BLOCK_LABEL_ARITY = {
    # top-level blocks (known block types)
    "terraform": 0, "locals": 0, "moved": 0, "import": 0,
    "resource": 2, "data": 2,
    "module": 1, "variable": 1, "output": 1, "provider": 1,
    "check": 1,
    # nested blocks (block form in native HCL)
    "backend": 1, "required_providers": 0,
    "provisioner": 1, "dynamic": 1, "connection": 0,
    "lifecycle": 0, "precondition": 0, "postcondition": 0,
    "validation": 0,
    # tftest (.tftest.json: run/assert/variables/expect_failures)
    "run": 1, "assert": 0, "variables": 0, "expect_failures": 0,
}

_INT_RE = re.compile(r"-?\d+")

# Whole-string single interpolation (Terraform JSON: `${expr}` is
# the expression itself)
def _is_single_interpolation(s: str) -> bool:
    return (
        isinstance(s, str)
        and s.startswith("${")
        and s.endswith("}")
        and "${" not in s[2:]
    )


# ==================================
# Position-aware JSON lexer/parser (zero dependencies, verbatim
# span preservation)
# ==================================

class _JsonSyntaxError(Exception):
    """JSON syntax error (carries a 0-based line number; maps to
    INVALID_HCL_JSON)."""

    def __init__(self, msg: str, line: int):
        super().__init__(f"{msg} (line {line})")
        self.line = line


class _JNode:
    """JSON value node: jtype + decoded value + raw span + line
    number.

    - object: value = [(key, key_node, value_node), ...] (duplicate
      keys preserved, matching the native duplicate
      attribute/block coding);
    - array: value = [node, ...];
    - string: value = decoded string; raw = source text (quotes
      included);
    - number: value = int/float; bool/null: value = True/False/None.
    """

    __slots__ = ("jtype", "value", "raw", "line")

    def __init__(self, jtype, value, raw, line):
        self.jtype = jtype
        self.value = value
        self.raw = raw
        self.line = line


class _JParser:
    """Recursive-descent JSON (position-aware; strict JSON, errors
    raise _JsonSyntaxError)."""

    _ESCAPES = {
        '"': '"', "\\": "\\", "/": "/",
        "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
    }
    _NUM_RE = re.compile(
        r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    )

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.line_starts = [0] + [
            i + 1 for i, ch in enumerate(text) if ch == "\n"
        ]

    def line_of(self, pos: int) -> int:
        return bisect.bisect_right(self.line_starts, pos) - 1

    def _error(self, msg: str) -> NoReturn:
        raise _JsonSyntaxError(msg, self.line_of(self.pos))

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < self.n else ""

    def _ws(self):
        while self.pos < self.n and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def parse(self) -> _JNode:
        self._ws()
        node = self._value()
        self._ws()
        if self.pos < self.n:
            self._error("trailing content after JSON value")
        return node

    def _value(self) -> _JNode:
        if self.pos >= self.n:
            self._error("unexpected end of input")
        ch = self._peek()
        if ch == "{":
            return self._object()
        if ch == "[":
            return self._array()
        if ch == '"':
            return self._string_node()
        if ch == "t" and self.text.startswith("true", self.pos):
            return self._word_node("true", True)
        if ch == "f" and self.text.startswith("false", self.pos):
            return self._word_node("false", False)
        if ch == "n" and self.text.startswith("null", self.pos):
            return self._word_node("null", None)
        if ch == "-" or ch.isdigit():
            return self._number_node()
        self._error(f"invalid token {ch!r}")

    def _word_node(self, word: str, value) -> _JNode:
        ls = self.line_of(self.pos)
        self.pos += len(word)
        return _JNode(
            "bool" if value is not None else "null",
            value, word, ls,
        )

    def _number_node(self) -> _JNode:
        ls = self.line_of(self.pos)
        m = self._NUM_RE.match(self.text, self.pos)
        if not m:
            self._error("invalid number")
        raw = m.group(0)
        self.pos = m.end()
        value: int | float
        if _INT_RE.fullmatch(raw):
            value = int(raw)
        else:
            value = float(raw)
        return _JNode("number", value, raw, ls)

    def _string_node(self) -> _JNode:
        start = self.pos
        ls = self.line_of(start)
        self.pos += 1  # consume '"'
        out: list[str] = []
        while self.pos < self.n:
            ch = self.text[self.pos]
            if ch == '"':
                self.pos += 1
                return _JNode(
                    "string", "".join(out), self.text[start:self.pos], ls,
                )
            if ch == "\\":
                nxt = self.text[self.pos + 1] if self.pos + 1 < self.n else ""
                if nxt in self._ESCAPES:
                    out.append(self._ESCAPES[nxt])
                    self.pos += 2
                    continue
                if nxt == "u":
                    hexs = self.text[self.pos + 2:self.pos + 6]
                    if len(hexs) == 4 and all(
                        c in "0123456789abcdefABCDEF" for c in hexs
                    ):
                        try:
                            out.append(chr(int(hexs, 16)))
                            self.pos += 6
                            continue
                        except ValueError:
                            pass
                    self._error("invalid \\u escape")
                self._error(f"invalid escape \\{nxt}")
            if ch == "\n":
                self._error("unescaped newline in string")
            out.append(ch)
            self.pos += 1
        self._error("unterminated string")

    def _object(self) -> _JNode:
        start = self.pos
        ls = self.line_of(start)
        self.pos += 1  # consume '{'
        members: list[tuple[Any, _JNode, _JNode]] = []
        self._ws()
        if self._peek() == "}":
            self.pos += 1
            return _JNode(
                "object", members, self.text[start:self.pos], ls,
            )
        while True:
            self._ws()
            if self._peek() != '"':
                self._error("expected object key string")
            key_node = self._string_node()
            self._ws()
            if self._peek() != ":":
                self._error("expected ':' after object key")
            self.pos += 1
            self._ws()
            val = self._value()
            members.append((key_node.value, key_node, val))
            self._ws()
            ch = self._peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "}":
                self.pos += 1
                break
            self._error("expected ',' or '}' in object")
        return _JNode("object", members, self.text[start:self.pos], ls)

    def _array(self) -> _JNode:
        start = self.pos
        ls = self.line_of(start)
        self.pos += 1  # consume '['
        elements: list[_JNode] = []
        self._ws()
        if self._peek() == "]":
            self.pos += 1
            return _JNode(
                "array", elements, self.text[start:self.pos], ls,
            )
        while True:
            self._ws()
            elements.append(self._value())
            self._ws()
            ch = self._peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "]":
                self.pos += 1
                break
            self._error("expected ',' or ']' in array")
        return _JNode("array", elements, self.text[start:self.pos], ls)


# ==================================
# Parser (inherits HclParser: contract layer / structure
# retrieval fully reused)
# ==================================

class HclJsonParser(HclParser):
    """HCL JSON ``.tf.json`` / ``.tfvars.json`` / ``.tftest.json``
    parser.

    After mapping JSON to an isomorphic HCL tree, the whole chain
    reuses ``HclParser``'s contract layer and the semantic passes
    (disclosed via ``source_variant="json"``); file identity is
    disclosed via ``metadata["hcl_profile"]`` (terraform_json
    variant / tfvars_json variant / tftest test_configuration).
    """

    _FORMAT = "hcl_json"
    file_type = "hcl_json"

    # ==================================
    # entry point
    # ==================================

    def parse(self, file_path) -> Document:
        """Parse an HCL JSON variant file: JSON lexing → isomorphic
        tree → contract/semantic layer reuse."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path, self._FORMAT, self.encoding, True,
            )
            text = ctx.text
            metadata = ctx.metadata

            file_name = Path(file_path).name
            low = file_name.lower()
            is_tfvars = low.endswith(".tfvars.json") or low.endswith(
                ".tfvars")
            is_tftest = low.endswith(".tftest.json") or low.endswith(
                ".tftest.hcl")

            diag = _Diag()
            mapping_codes: list = []
            tree: dict[str, Any] = {
                "items": [], "leading_comments": [],
            }
            root = None
            try:
                root = _JParser(text).parse()
            except _JsonSyntaxError as exc:
                # syntax channel (contract loss.reasons; the JSON
                # counterpart of INVALID_HCL)
                diag.hit("INVALID_HCL_JSON")
                _log.debug("hcl json syntax error: %s", exc)
            if root is not None:
                if root.jtype != "object":
                    # semantic-level root anomaly → profile loss_codes
                    mapping_codes.append("INVALID_TF_JSON_ROOT")
                else:
                    tree = {
                        "items": self._map_members(
                            root, mapping_codes,
                            tfvars_top=is_tfvars and not is_tftest,
                        ),
                        "leading_comments": [],
                    }

            metadata["parse_success"] = not any(
                k.startswith((
                    "INVALID", "UNCLOSED", "TRUNCATED", "DEPTH",
                ))
                for k in diag.counts
            )
            metadata["top_level_keys"] = [
                item["name"] for item in tree["items"]
            ]
            metadata["structured_data"] = self._structured_data(tree)

            metadata["config_entries"] = self._build_contract(
                tree, diag, file_name=file_name, is_tfvars=is_tfvars,
            )
            # dialect disclosure (this parser's own contract product,
            # not part of the native zero-drift face)
            dialect = (metadata["config_entries"] or {}).get("dialect")
            if isinstance(dialect, dict):
                dialect["syntax"] = "json"
                dialect["scope"] = "p2b_json_variant"
                dialect["hcl_json_mapping"] = (
                    "structure_preserving_known_block_types_only"
                )

            # object-level unresolved registry (family convention;
            # guarded silent degradation)
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            # file identity disclosure (generic_json vs
            # terraform_json profile distinction; JSON semantics
            # never conflated with native)
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
                "syntax": "json",
                "hcl_version": 2,
                "domain_profile": _domain_profile,
                "file_kind": _file_kind,
                "profile_source": "extension_and_project_context",
                "mapping_decisions": {
                    "known_block_types_map_to_blocks": True,
                    "unknown_object_values_map_to": (
                        "attribute_value_schemaless_ambiguity"
                    ),
                    "expressions_preserved_as_raw_strings": True,
                    "line_numbers_from_positional_lexing": True,
                },
            }

            # semantic passes apply directly (source_variant="json";
            # they operate on the isomorphic tree as-is);
            # mapping-layer codes are appended to profile loss_codes
            # (never into the contract channel — keeping the
            # contract coding surface pure).
            try:
                profile = build_terraform_profile(
                    tree, metadata["config_entries"],
                    file_name, is_tfvars,
                    source_variant="json", is_tftest=is_tftest,
                )
                if mapping_codes:
                    counts: dict = {}
                    for code in mapping_codes:
                        counts[code] = counts.get(code, 0) + 1
                    loss_codes = profile.setdefault("loss_codes", [])
                    for code, n in sorted(counts.items()):
                        loss_codes.append(f"{code}:{n}")
                metadata["terraform_profile"] = profile
                file_role = profile.get("file_role")
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
    # JSON → HCL isomorphic tree mapping
    # ==================================

    def _map_members(self, node: _JNode, mapping_codes: list,
                     tfvars_top: bool = False) -> list:
        """JSON object members → HCL body items (blocks and attrs
        mixed)."""
        items = []
        for key, key_node, val in node.value:
            arity = None if tfvars_top else _BLOCK_LABEL_ARITY.get(key)
            if arity is not None:
                instances = self._block_instances_for_type(val, arity)
                if instances is not None:
                    for labels, body_node, mismatch in instances:
                        if mismatch:
                            mapping_codes.append(
                                "HCL_JSON_BLOCK_SHAPE_MISMATCH")
                        body_items = (
                            self._map_members(body_node, mapping_codes)
                            if body_node.jtype == "object" else []
                        )
                        items.append({
                            "kind": "block",
                            "name": key,
                            "block_type": key,
                            "labels": labels,
                            "line_start": key_node.line,
                            "line_end": max(
                                body_node.line, key_node.line),
                            "body": {
                                "items": body_items,
                                "leading_comments": [],
                            },
                            "leading_comments": [],
                        })
                    continue
                # non-object value under a known block type (an
                # anomalous shape) → preserved as an attribute
            items.append(self._attr_item(key, key_node, val, mapping_codes))
        return items

    def _block_instances_for_type(self, val: _JNode, arity: int):
        """A known block type's value → [(labels, body_node,
        mismatch)]; None = not a block shape.

        An object descends the label levels; an array (all-object
        elements) yields multiple blocks of the same type (each
        element descends the label levels independently).
        """
        if val.jtype == "object":
            return self._descend_labels(val, arity, [])
        if (
            val.jtype == "array"
            and val.value
            and all(v.jtype == "object" for v in val.value)
        ):
            out = []
            for element in val.value:
                out.extend(self._descend_labels(element, arity, []))
            return out
        return None

    def _descend_labels(self, node: _JNode, arity: int, labels: list):
        """Descend the label levels: each object key = one label,
        after arity levels the node is the body.

        A non-object at a label level → mismatch (structure-
        preserving degradation: labels truncated +
        HCL_JSON_BLOCK_SHAPE_MISMATCH coded, never aborting).
        """
        if node.jtype != "object":
            return [(labels, node, True)]
        if arity == 0:
            return [(labels, node, False)]
        out = []
        for key, _key_node, child in node.value:
            out.extend(self._descend_labels(child, arity - 1, labels + [key]))
        return out

    def _attr_item(self, key: str, key_node: _JNode, val: _JNode,
                   mapping_codes: list) -> dict:
        expr = self._expr_from_node(val, mapping_codes)
        return {
            "kind": "attr",
            "name": key,
            "line_start": key_node.line,
            "line_end": max(val.line, key_node.line),
            "expr": expr,
            "leading_comments": [],
            "inline_comment": None,
        }

    def _expr_from_node(self, node: _JNode, mapping_codes: list) -> dict:
        """A JSON value node → a native-isomorphic expr dict (raw =
        verbatim span)."""
        ls = node.line
        t = node.jtype
        if t == "string":
            s = node.value
            if _is_single_interpolation(s):
                # Terraform JSON: a whole-string single interpolation
                # is the expression itself (value unknown)
                return {
                    "raw": node.raw, "value": None,
                    "value_type": cc.VALUE_TYPE_UNKNOWN,
                    "expression_kind": "expression", "refs": [],
                    "line_start": ls, "line_end": ls,
                }
            kind = "template" if "${" in s else "literal"
            return {
                "raw": node.raw, "value": s,
                "value_type": cc.VALUE_TYPE_STRING,
                "expression_kind": kind, "refs": [],
                "line_start": ls, "line_end": ls,
            }
        if t == "number":
            return {
                "raw": node.raw, "value": node.value,
                "value_type": (
                    cc.VALUE_TYPE_INT if isinstance(node.value, int)
                    else cc.VALUE_TYPE_FLOAT
                ),
                "expression_kind": "literal", "refs": [],
                "line_start": ls, "line_end": ls,
            }
        if t == "bool":
            return {
                "raw": node.raw, "value": node.value,
                "value_type": cc.VALUE_TYPE_BOOL,
                "expression_kind": "literal", "refs": [],
                "line_start": ls, "line_end": ls,
            }
        if t == "null":
            return {
                "raw": node.raw, "value": None,
                "value_type": cc.VALUE_TYPE_NULL,
                "expression_kind": "literal", "refs": [],
                "line_start": ls, "line_end": ls,
            }
        if t == "object":
            children = [
                self._attr_item(k, kn, v, mapping_codes)
                for (k, kn, v) in node.value
            ]
            value: dict[str, Any] = {}
            for child in children:
                cexpr = child.get("expr") or {}
                if cexpr.get("expression_kind") == "literal":
                    value[child["name"]] = cexpr.get("value")
                else:
                    value[child["name"]] = None  # unknown placeholder
            return {
                "raw": node.raw, "value": value, "value_type": None,
                "expression_kind": "object", "refs": [],
                "line_start": ls,
                "line_end": self._object_line_end(node, children),
                "children": children,
            }
        # array
        children = []
        values: list[Any] = []
        for i, element in enumerate(node.value):
            child = self._attr_item(str(i), element, element, mapping_codes)
            children.append(child)
            eexpr = child.get("expr") or {}
            if eexpr.get("expression_kind") == "literal":
                values.append(eexpr.get("value"))
            else:
                values.append(None)
        return {
            "raw": node.raw, "value": values, "value_type": None,
            "expression_kind": "list", "refs": [],
            "line_start": ls,
            "line_end": self._object_line_end(node, children),
            "children": children,
        }

    @staticmethod
    def _object_line_end(node: _JNode, children: list) -> int:
        line_end = node.line
        for child in children:
            cexpr = child.get("expr") or {}
            if cexpr.get("line_end") is not None:
                line_end = max(line_end, cexpr["line_end"])
        return line_end
