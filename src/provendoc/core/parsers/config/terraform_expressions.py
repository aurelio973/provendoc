# -*- coding: utf-8 -*-
"""Terraform expression semantics layer.

Position: a sub-pass of ``terraform_profile``, producing the pure
disclosure structure ``terraform_profile["expression_semantics"]``
plus supplementary dependency edges (function arguments, for
collections, splat sources, dynamic for_each, state transfers,
check conditions — recovering the honesty-boundary targets the
profile layer leaves out).

Iron rules:
- Zero execution, zero IO: no function / provider / provisioner
  execution; partial evaluation means pure-literal arithmetic
  folding (tokenizer + recursive descent, a closed operator
  set) — any identifier reference, function call, or
  interpolation is always blocked (PARTIAL_EVALUATION_BLOCKED,
  into profile loss_codes) or unknown (not coded; the syntax
  layer owns that);
- Additive: the contract is untouched (fold results are profile
  side notes only, entry value / value state unchanged);
  existing terraform_profile keys untouched;
- Raw preservation: every structure field carries the raw source
  (quotes included);
- The static AST is never treated as the final configuration:
  instance_expansion only marks static_known / unknown and never
  generates instance keys like ``res[0]``;
- Out of scope: JSON variants, directory-level merging, schema
  acquisition.

Dependency direction (cycle prevention): this module is imported
one-way by the terraform_profile top level; it only imports
contracts (shared contract primitives); the top-level splitter
_split_top_level is defined here for terraform_profile reuse.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from provendoc.core.parsers.config import contracts as cc

# ---- shared splitting utilities (moved here from
# terraform_profile, one-way dependency) ----


def _split_top_level(text: str, seps: Sequence[str] = (",",)) -> List[str]:
    """Top-level split (bracket/quote aware). Splits the raw text
    only, never evaluates."""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == '"':
                in_str = False
            buf.append(ch)
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif depth == 0 and ch in seps:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _mask_string_contents(text: str) -> str:
    """Replace string-literal contents with \x00 (length
    preserved, ``${}`` interpolation spans preserved).

    Used by the bare-reference scan: plain text inside quotes is
    excluded from reference detection, while ``${...}``
    interpolation spans stay verbatim (detect_references handles
    those).
    """
    out = list(text)
    interp_spans = [m.span() for m in re.finditer(r"\$\{[^{}]*\}", text)]
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                for k in range(i, min(i + 2, len(text))):
                    out[k] = "\x00"
                i += 2
                continue
            if ch == '"':
                in_str = False
                out[i] = '"'
                i += 1
                continue
            if not any(s <= i < e for s, e in interp_spans):
                out[i] = "\x00"
        elif ch == '"':
            in_str = True
        i += 1
    return "".join(out)


# ---- reference extraction (bare traversals + ${}
# interpolations, shared detection) ----

_BARE_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)+)"
)


def _expr_refs(text: str,
               ignore_first: Optional[set] = None) -> List[str]:
    """Reference targets inside an expression text (deduplicated,
    order preserved; the leading segment of for variables can be
    ignored)."""
    if not text:
        return []
    seen: List[str] = []
    masked = _mask_string_contents(text)
    for m in _BARE_REF_RE.finditer(masked):
        t = m.group(1)
        if ignore_first and t.split(".")[0] in ignore_first:
            continue
        if t not in seen:
            seen.append(t)
    for r in cc.detect_references(text):
        t = (r.get("target") or "").strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _find_toplevel(text: str, needle: str) -> Optional[int]:
    """Index of the first top-level (depth 0, not inside a
    string) occurrence of needle."""
    masked = _mask_string_contents(text)
    depth = 0
    n = len(needle)
    i = 0
    while i <= len(masked) - n:
        ch = masked[i]
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            i += 1
            continue
        if depth == 0 and masked[i:i + n] == needle:
            return i
        i += 1
    return None


# ---- pure-literal arithmetic folding (tokenizer + recursive
# descent, zero identifiers) ----


class _FoldBlocked(Exception):
    """The expression contains references / functions /
    interpolations — partial evaluation is blocked."""


class _FoldError(Exception):
    """Tokenizing / parsing failed (non-reference causes) —
    unknown, not coded."""


_TOK_RE = re.compile(
    r"""\s*(?:
        (?P<num>\d+(?:\.\d+)?)
      | (?P<str>"(?:[^"\\]|\\.)*")
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op><=|>=|==|!=|&&|\|\||[+\-*/%()<>!?:])
    )""",
    re.X,
)


def _tok_fold_str(tok: str) -> str:
    body = tok[1:-1]
    out: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({
                "n": "\n", "t": "\t", "r": "\r", '"': '"',
                "\\": "\\", "'": "'",
            }.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _tokenize_fold(raw: str) -> List[Tuple[str, Any]]:
    toks: List[Tuple[str, Any]] = []
    i = 0
    while i < len(raw):
        m = _TOK_RE.match(raw, i)
        if not m or m.end() == m.start():
            raise _FoldError(f"tokenize fail @ {i}")
        if m.group("num") is not None:
            v: Any = m.group("num")
            toks.append(("num", float(v) if "." in v else int(v)))
        elif m.group("str") is not None:
            if "${" in m.group("str"):
                raise _FoldBlocked("interpolation")
            toks.append(("str", _tok_fold_str(m.group("str"))))
        elif m.group("ident") is not None:
            name = m.group("ident")
            if name == "true":
                toks.append(("bool", True))
            elif name == "false":
                toks.append(("bool", False))
            elif name == "null":
                toks.append(("null", None))
            else:
                raise _FoldBlocked(f"identifier {name}")
        else:
            toks.append(("op", m.group("op")))
        i = m.end()
    return toks


def _require_num(x: Any) -> None:
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        raise _FoldError("numeric operand expected")


class _FoldParser:
    """Recursive descent: ternary → or → and → cmp → add → mul →
    unary → primary."""

    def __init__(self, toks: List[Tuple[str, Any]]):
        self.toks = toks
        self.i = 0

    def _peek_op(self) -> Optional[str]:
        if self.i < len(self.toks) and self.toks[self.i][0] == "op":
            return self.toks[self.i][1]
        return None

    def _eat(self, op: str) -> bool:
        if self._peek_op() == op:
            self.i += 1
            return True
        return False

    def parse(self) -> Any:
        v = self._ternary()
        if self.i != len(self.toks):
            raise _FoldError("trailing tokens")
        return v

    def _ternary(self) -> Any:
        cond = self._or()
        if self._eat("?"):
            a = self._ternary()
            if not self._eat(":"):
                raise _FoldError("ternary missing :")
            b = self._ternary()
            if not isinstance(cond, bool):
                raise _FoldError("ternary condition not bool")
            return a if cond else b
        return cond

    def _or(self) -> Any:
        v = self._and()
        while self._eat("||"):
            r = self._and()
            if not (isinstance(v, bool) and isinstance(r, bool)):
                raise _FoldError("|| operand not bool")
            v = v or r
        return v

    def _and(self) -> Any:
        v = self._cmp()
        while self._eat("&&"):
            r = self._cmp()
            if not (isinstance(v, bool) and isinstance(r, bool)):
                raise _FoldError("&& operand not bool")
            v = v and r
        return v

    def _cmp(self) -> Any:
        v = self._add()
        while True:
            op = self._peek_op()
            if op in ("==", "!=", "<", ">", "<=", ">="):
                self.i += 1
                r = self._add()
                if op == "==":
                    v = v == r
                elif op == "!=":
                    v = v != r
                else:
                    _require_num(v)
                    _require_num(r)
                    v = {"<": v < r, ">": v > r,
                         "<=": v <= r, ">=": v >= r}[op]
            else:
                return v

    def _add(self) -> Any:
        v = self._mul()
        while True:
            op = self._peek_op()
            if op in ("+", "-"):
                self.i += 1
                r = self._mul()
                if op == "+":
                    if isinstance(v, str) and isinstance(r, str):
                        v = v + r
                    else:
                        _require_num(v)
                        _require_num(r)
                        v = v + r
                else:
                    _require_num(v)
                    _require_num(r)
                    v = v - r
            else:
                return v

    def _mul(self) -> Any:
        v = self._unary()
        while True:
            op = self._peek_op()
            if op in ("*", "/", "%"):
                self.i += 1
                r = self._unary()
                _require_num(v)
                _require_num(r)
                if op == "*":
                    v = v * r
                else:
                    if r == 0:
                        raise _FoldError("division by zero")
                    v = v / r if op == "/" else v % r
            else:
                return v

    def _unary(self) -> Any:
        if self._eat("!"):
            v = self._unary()
            if not isinstance(v, bool):
                raise _FoldError("! operand not bool")
            return not v
        if self._eat("-"):
            v = self._unary()
            _require_num(v)
            return -v
        return self._primary()

    def _primary(self) -> Any:
        if self.i >= len(self.toks):
            raise _FoldError("unexpected end")
        kind, val = self.toks[self.i]
        if kind == "op":
            if val == "(":
                self.i += 1
                v = self._ternary()
                if not self._eat(")"):
                    raise _FoldError("unclosed paren")
                return v
            raise _FoldError(f"unexpected op {val}")
        if kind == "null":
            self.i += 1
            return None
        self.i += 1
        return val


def _serialize_fold(v: Any) -> Any:
    """Serialize a folded value (str keeps its escaped form,
    whole-number floats become ints)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v


def _try_fold(raw: str) -> Tuple[str, Any]:
    """Return (value_status, evaluated); the caller codes the
    blocked side."""
    try:
        v = _FoldParser(_tokenize_fold(raw)).parse()
        return ("known", _serialize_fold(v))
    except _FoldBlocked:
        return ("blocked", None)
    except _FoldError:
        return ("unknown", None)


# ---- for / splat shape recognition ----

_SPLAT_FULL_RE = re.compile(
    r"^(?P<source>[A-Za-z_][A-Za-z0-9_.\-]*)\[\*\]"
    r"(?:\.(?P<rest>[A-Za-z0-9_.\-]*))?$"
)
_SPLAT_LEGACY_RE = re.compile(
    r"^(?P<source>[A-Za-z_][A-Za-z0-9_.\-]*)\.\*\.(?P<rest>[A-Za-z0-9_.\-]+)$"
)


def _parse_for(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if text.startswith("[for "):
        result_kind = "tuple"
        body = text[1:-1].strip() if text.endswith("]") else text[1:].strip()
    elif text.startswith("{for ") or text.startswith("{ for "):
        result_kind = "map"
        body = text[1:-1].strip() if text.endswith("}") else text[1:].strip()
    else:
        return None
    if body.startswith("for"):
        body = body[3:]
    in_idx = _find_toplevel(body, " in ")
    if in_idx is None:
        return None
    vars_part = body[:in_idx].strip()
    rest = body[in_idx + 4:]
    colon_idx = _find_toplevel(rest, ": ")
    if colon_idx is None:
        return None
    collection = rest[:colon_idx].strip()
    tail = rest[colon_idx + 1:].strip()
    if not collection:
        return None
    rec: Dict[str, Any] = {
        "result_kind": result_kind,
        "for_variables": [v.strip() for v in vars_part.split(",")
                          if v.strip()],
        "collection_expression": collection,
    }
    if result_kind == "tuple":
        rec["value_expression"] = tail
    else:
        arrow = _find_toplevel(tail, " => ")
        if arrow is None:
            return None
        rec["key_expression"] = tail[:arrow].strip()
        rest2 = tail[arrow + 4:]
        ifidx = _find_toplevel(rest2, " if ")
        if ifidx is None:
            rec["value_expression"] = rest2.strip()
        else:
            rec["value_expression"] = rest2[:ifidx].strip()
            rec["condition"] = rest2[ifidx + 4:].strip()
    return rec


# ---- embedded-language candidate sniffing (zero execution) ----


def _sniff_language(text: str) -> Tuple[Optional[str], Optional[str]]:
    t = (text or "").lstrip("\r\n")
    if not t.strip():
        return None, None
    first = t.split("\n", 1)[0].strip()
    if first.startswith("#!"):
        if "python" in first:
            return "python", first
        return "shell", first
    if first.startswith("#cloud-config"):
        return "cloud-init", first
    stripped = t.strip()
    if stripped[:1] in "{[":
        try:
            import json
            json.loads(stripped)
            return "json", "structural JSON"
        # JSON format probe: a parse failure simply means not
        # JSON — fall through to the next probes.
        except (ValueError, RecursionError):
            pass
    m = re.match(r"^(SELECT|INSERT|UPDATE|DELETE|WITH)\b",
                 stripped, re.I)
    if m:
        return "sql", m.group(0)
    lines = [ln for ln in stripped.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) >= 2:
        kv = sum(1 for ln in lines if re.match(r"^[^\s:]{1,64}:", ln))
        if kv / len(lines) >= 0.6:
            return "yaml", f"{kv}/{len(lines)} key-value lines"
    if re.search(r"^\s*(def |import |from\s+\w+\s+import)",
                 stripped, re.M):
        return "python", "def/import statement"
    return None, None


# ---- main pass ----

EdgeSpec = Tuple[List[str], str, str, str]
# (source_path, target, relation, attribute)


def build_expression_semantics(
    tree: Optional[Dict[str, Any]],
    entries: List[Dict[str, Any]],
    hit: Callable[[str], None],
) -> Tuple[Dict[str, Any], List[EdgeSpec]]:
    """Build the expression_semantics structure plus the
    supplementary dependency-edge spec list.

    Edge specs are converted to _make_edge by terraform_profile
    uniformly (source_node derivation / namespace classification
    reuse the terraform_profile rules).
    """
    for_expressions: List[Dict[str, Any]] = []
    splat_expressions: List[Dict[str, Any]] = []
    dynamic_blocks: List[Dict[str, Any]] = []
    instance_expansions: List[Dict[str, Any]] = []
    partial_evaluations: List[Dict[str, Any]] = []
    state_transfers: List[Dict[str, Any]] = []
    embedded_languages: List[Dict[str, Any]] = []
    provider_blocks: List[Dict[str, Any]] = []
    edges: List[EdgeSpec] = []

    def entry_source(path: List[str]) -> List[str]:
        return list(path[:-1])

    # ---- moved / import / check static registry (independent of
    # expression_kind — from/to/id/condition/error_message may be
    # bare references or literals, all registered as raw;
    # identified: yes, registered: yes, executed: no) ----
    for e in entries:
        path = list(e.get("path") or [])
        if not path:
            continue
        attr = path[-1]
        src = entry_source(path)
        raw = e.get("raw_value")
        if len(path) == 2 and path[0] in ("moved", "import"):
            # multi-block grouping: the contract layer blocks
            # duplicate attributes in the same body at
            # DUPLICATE_ATTRIBUTE (they never enter entries), and
            # entries are produced per block, contiguous in
            # document order — the same attr reappearing in the
            # entry stream marks the start of a new block (a
            # deterministic criterion, not a heuristic). The
            # earlier keying by block_type would have collapsed
            # multiple moved/import blocks into one last-wins
            # record.
            current = None
            for transfer in reversed(state_transfers):
                if transfer["block_type"] == path[0]:
                    current = transfer
                    break
            if current is None or attr in current:
                current = {"block_type": path[0], "path": [path[0]]}
                state_transfers.append(current)
            current[attr] = raw
            if attr in ("from", "to") and isinstance(raw, str):
                t = raw.strip()
                if t:
                    edges.append((src, t, "state_transfer", attr))
        elif (len(path) >= 4 and path[0] == "check"
                and path[-2] == "assert"):
            # check blocks group by path[:2] (the block name) —
            # multiple named check blocks are no longer merged
            # into one; within a block, each assert still gets
            # one record per condition.
            block_key = list(path[:2])
            current = None
            for transfer in reversed(state_transfers):
                if (transfer["block_type"] == "check"
                        and transfer.get("path") == block_key):
                    current = transfer
                    break
            if current is None:
                current = {
                    "block_type": "check", "path": block_key,
                    "asserts": [],
                }
                state_transfers.append(current)
            if attr == "condition":
                current["asserts"].append({
                    "condition_raw": raw,
                    "error_message_raw": None,
                })
                if isinstance(raw, str):
                    for t in _expr_refs(raw):
                        edges.append((src, t, "check_condition", attr))
            elif attr == "error_message" and current["asserts"]:
                current["asserts"][-1]["error_message_raw"] = raw

    # ---- entry-level expression scan ----
    for e in entries:
        path = list(e.get("path") or [])
        if not path:
            continue
        attr = path[-1]
        src = entry_source(path)
        raw = e.get("raw_value")
        kind = e.get("expression_kind")

        # count / for_each instantiation semantics (in a
        # resource/data/module context; a dynamic block's for_each
        # is modeled separately below, not registered twice)
        if (attr in ("count", "for_each") and len(path) >= 2
                and path[0] in ("resource", "data", "module")
                and "dynamic" not in path):
            status, folded = _try_fold(raw if isinstance(raw, str) else "")
            rec = {
                "path": path[:-1],
                "meta_kind": attr,
                "raw": raw,
                "expansion_status": (
                    "static_known" if status == "known" else "unknown"),
            }
            if status == "known":
                rec["static_value"] = folded
            instance_expansions.append(rec)
            continue

        if kind != "expression" or not isinstance(raw, str):
            # embedded language: heredoc entry (expression_kind ==
            # "heredoc")
            if kind == "heredoc":
                body = e.get("value")
                cand, ev = _sniff_language(
                    body if isinstance(body, str) else "")
                if cand:
                    embedded_languages.append({
                        "node_type": "embedded_script",
                        "parent_path": src,
                        "language_candidate": cand,
                        "language_evidence": ev,
                        "raw_preserved": True,
                        "execution": "blocked",
                    })
            continue

        text = raw.strip()

        # for expression
        if text.startswith("[for ") or text.startswith("{for ") \
                or text.startswith("{ for "):
            parsed = _parse_for(raw)
            if parsed:
                parsed["path"] = path
                parsed["raw"] = raw
                ignore = {v for v in parsed["for_variables"]}
                for t in _expr_refs(parsed["collection_expression"]):
                    edges.append((src, t, "for_collection", attr))
                for seg in ("key_expression", "value_expression", "condition"):
                    for t in _expr_refs(parsed.get(seg) or "", ignore):
                        edges.append((src, t, "for_body", attr))
                for_expressions.append(parsed)
                continue

        # splat expression
        m = _SPLAT_FULL_RE.match(text)
        if m:
            splat_expressions.append({
                "path": path, "raw": raw,
                "splat_kind": "full",
                "source": m.group("source"),
                "attribute": m.group("rest"),
                "references": [m.group("source")],
            })
            edges.append((src, m.group("source"), "splat_source", attr))
            continue
        m = _SPLAT_LEGACY_RE.match(text)
        if m:
            splat_expressions.append({
                "path": path, "raw": raw,
                "splat_kind": "legacy",
                "source": m.group("source"),
                "attribute": m.group("rest"),
                "references": [m.group("source")],
            })
            edges.append((src, m.group("source"), "splat_source", attr))
            continue

        # pure-literal arithmetic folding (function calls /
        # references → blocked)
        status, folded = _try_fold(raw)
        if status == "known":
            partial_evaluations.append({
                "path": path, "raw": raw,
                "value_status": "known",
                "evaluated": folded,
            })
        elif status == "blocked":
            partial_evaluations.append({
                "path": path, "raw": raw,
                "value_status": "unknown",
                "reason_code": "PARTIAL_EVALUATION_BLOCKED",
            })
            hit("PARTIAL_EVALUATION_BLOCKED")
            # recover references inside function arguments
            # (honesty-boundary recovery)
            for fc in e.get("function_calls") or []:
                for arg in fc.get("arguments") or []:
                    for t in _expr_refs(arg):
                        edges.append((src, t, "function_argument", attr))

    # ---- dynamic-block modeling + provider layering + embedded
    # languages (provisioner) ----

    def walk(items, prefix):
        for item in items or []:
            if item.get("kind") != "block":
                continue
            bt = item.get("block_type")
            labels = item.get("labels") or []
            path = list(prefix) + [bt] + labels
            body = (item.get("body") or {}).get("items", []) or []
            if bt == "dynamic":
                rec: Dict[str, Any] = {
                    "path": path,
                    "name": labels[0] if labels else None,
                    "for_each": None,
                    "iterator": labels[0] if labels else None,
                    "iterator_source": "default_label",
                    "content_attributes": [],
                }
                for child in body:
                    if child.get("kind") != "attr":
                        continue
                    nm = child.get("name")
                    raw = (child.get("expr") or {}).get("raw")
                    if nm == "for_each":
                        rec["for_each"] = raw
                        if isinstance(raw, str):
                            for t in _expr_refs(raw):
                                edges.append((
                                    list(path), t, "dynamic_for_each",
                                    "for_each"))
                    elif nm == "iterator":
                        rec["iterator"] = raw
                        rec["iterator_source"] = "explicit"
                content = [c for c in body
                           if c.get("kind") == "block"
                           and c.get("block_type") == "content"]
                if content:
                    rec["content_attributes"] = [
                        c.get("name") for c in
                        (content[0].get("body") or {}).get("items", [])
                        or [] if c.get("kind") == "attr"
                    ]
                rec["template_only_not_expanded"] = True
                dynamic_blocks.append(rec)
            elif bt in ("resource", "data", "module"):
                provider_blocks.append({
                    "path": path,
                    "terraform_status": "candidate",
                    "provider_schema_status": "unavailable",
                })
            elif bt == "provisioner":
                for child in body:
                    if (child.get("kind") == "attr"
                            and child.get("name") == "command"):
                        expr = child.get("expr") or {}
                        val = expr.get("value")
                        text = val if isinstance(val, str) \
                            else expr.get("raw")
                        cand, ev = _sniff_language(
                            text if isinstance(text, str) else "")
                        if cand:
                            embedded_languages.append({
                                "node_type": "embedded_script",
                                "parent_path": list(path),
                                "language_candidate": cand,
                                "language_evidence": ev,
                                "raw_preserved": True,
                                "execution": "blocked",
                            })
            walk(body, path)

    walk((tree or {}).get("items", []), [])

    semantics = {
        "for_expressions": for_expressions,
        "splat_expressions": splat_expressions,
        "dynamic_blocks": dynamic_blocks,
        "instance_expansions": instance_expansions,
        "partial_evaluations": partial_evaluations,
        "state_transfers": state_transfers,
        "provider_layers": provider_blocks,
        "embedded_languages": embedded_languages,
    }
    return semantics, edges


# ---- Markdown summary sections (called by
# terraform_profile.render) ----


def render_expression_sections(sem: Optional[Dict[str, Any]]) -> List[str]:
    """Expressions / State Transfers section lines (counts +
    representative samples)."""
    lines: List[str] = []
    sem = sem or {}
    counts = (
        ("for expressions", sem.get("for_expressions")),
        ("splat expressions", sem.get("splat_expressions")),
        ("dynamic blocks", sem.get("dynamic_blocks")),
    )
    if any(lst for _, lst in counts):
        lines.append("## Expressions")
        lines.append("")
        for label, lst in counts:
            lines.append(f"- {label}: {len(lst or [])}")
        for fe in (sem.get("for_expressions") or [])[:2]:
            lines.append(f"  - sample: `{fe.get('raw')}`")
        lines.append("")
    st = sem.get("state_transfers") or []
    if st:
        lines.append("## State Transfers")
        lines.append("")
        for t in st:
            bt = t.get("block_type")
            if bt == "moved":
                lines.append(
                    f"- moved: `{t.get('from')}` -> `{t.get('to')}`")
            elif bt == "import":
                lines.append(
                    f"- import: `{t.get('to')}` (id `{t.get('id')}`)")
            elif bt == "check":
                lines.append(f"- check: `{t.get('path')}`")
        lines.append("")
    return lines
