# -*- coding: utf-8 -*-
"""Terraform profile semantic layer.

Sits as an independent semantic pass after the HCL syntax parser:
input = syntax tree + flat contract + file identity, output =
``metadata["terraform_profile"]`` (disclosure-only structure).

Non-negotiables:
- No execution, no IO: functions / provisioners / validation
  conditions are never executed; files and environment variables
  are never read; no network; blocked objects get a code and the raw
  text is preserved.
- Honest boundaries: validated=False / not_attempted /
  context_dependent / resolved=False are constant; a candidate never
  impersonates a fact.
- Additive: existing contract keys are never touched (including
  loss.reasons — the syntax/recovery code channel stays zero-drift);
  semantic-layer codes land in the profile's own loss_codes; new
  fields go to entry additive fields (function_calls /
  sensitivity_source / derived_sensitive_candidates /
  variable_assignment / command_candidate) and new metadata keys.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.terraform_expressions import (
    _split_top_level,
    build_expression_semantics,
    render_expression_sections,
)

# ---- constants ----

# Root namespace classification: var/local/module/data → declarative
# namespaces; path/terraform/each/self/count → meta namespaces;
# everything else is classified as a resource-style prefix candidate
# (classification only, never resolved to a resource instance).
_ROOT_NS = {
    "var": "variable",
    "local": "locals",
    "module": "module",
    "data": "data",
}
_META_NS = frozenset({"path", "terraform", "each", "self", "count"})

# Function families never evaluated: a hit means evaluation=blocked
# + FUNCTION_EVALUATION_BLOCKED; file-reading functions additionally
# get EXTERNAL_FILE_READ_BLOCKED.
_DANGEROUS_EVAL = frozenset({
    "file", "filebase64", "templatefile",
    "yamldecode", "jsondecode", "external", "run",
})
_DANGEROUS_IO = frozenset({"file", "filebase64", "templatefile"})

_KNOWN_BLOCK_TYPES = frozenset({
    "terraform", "provider", "resource", "data", "module",
    "variable", "output", "locals", "moved", "import", "check",
})

_FUNC_CALL_RE = re.compile(r"(?<![A-Za-z0-9_.\-])([a-z_][a-z0-9_]*)\s*\(")
_DIRECTIVE_RE = re.compile(r"%\{[^{}]*\}")
_DIRECTIVE_OPEN_RE = re.compile(r"%\{")


# The evidence-splitting helpers (zero evaluation) live in
# terraform_expressions; _split_top_level is reused via import here.


def _extract_call_args(text: str, open_idx: int) -> Optional[str]:
    """Match parentheses starting at '(' and return the raw argument
    region; None when unclosed."""
    depth = 0
    in_str = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return None


# ---- type_expression structure recovery (no validation) ----

_TYPE_LEAF = frozenset({"string", "number", "bool", "any"})


def _parse_type_expr(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()

    def inner(text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if text in _TYPE_LEAF:
            return {"kind": text}
        m = re.fullmatch(r"(list|set|map)\s*\((.*)\)", text, re.S)
        if m:
            return {"kind": m.group(1), "inner": inner(m.group(2))}
        m = re.fullmatch(r"object\s*\(\s*(\{.*\})\s*\)", text, re.S)
        if m:
            body = m.group(1).strip()
            if body.startswith("{") and body.endswith("}"):
                body = body[1:-1]
            attrs: Dict[str, Any] = {}
            for part in _split_top_level(body, seps=(",", "\n")):
                kv = part.split("=", 1)
                if len(kv) == 2:
                    attrs[kv[0].strip()] = inner(kv[1].strip())
                else:
                    attrs.setdefault("__unparsed__", []).append(part)
            return {"kind": "object", "attributes": attrs}
        m = re.fullmatch(r"tuple\s*\(\s*(\[.*\])\s*\)", text, re.S)
        if m:
            body = m.group(1).strip()
            if body.startswith("[") and body.endswith("]"):
                body = body[1:-1]
            return {
                "kind": "tuple",
                "elements": [inner(p) for p in _split_top_level(body)],
            }
        m = re.fullmatch(r"optional\s*\((.*)\)", text, re.S)
        if m:
            parts = _split_top_level(m.group(1))
            node = {"kind": "optional", "inner": inner(parts[0])}
            if len(parts) > 1:
                node["default"] = parts[1]
            return node
        return {"kind": "unparsed", "raw": text}

    return inner(raw)


# ---- dependency edges ----

def _classify_namespace(target: str) -> str:
    head = target.split(".")[0].strip().lower()
    if head in _ROOT_NS:
        return _ROOT_NS[head]
    if head in _META_NS:
        return "meta_namespace"
    return "resource"


def _make_edge(source: Sequence[str], target: str, relation: str,
               attribute: str) -> Dict[str, Any]:
    return {
        "source_node": cc.path_str(list(source)),
        "target": target,
        "target_namespace": _classify_namespace(target),
        "relation": relation,
        "attribute": attribute,
        "resolved": False,
    }


# ---- main pass ----

def build_terraform_profile(
    tree: Optional[Dict[str, Any]],
    contract: Optional[Dict[str, Any]],
    file_name: Optional[str],
    is_tfvars: bool,
    source_variant: str = "native",
    is_tftest: bool = False,
) -> Dict[str, Any]:
    """Build ``metadata["terraform_profile"]``.

    Enriches contract entries additively (new keys only); existing
    keys are untouched; semantic-layer loss codes land in
    profile["loss_codes"] (never in contract loss.reasons, keeping
    the syntax layer zero-drift).

    ``source_variant`` ("native"|"json", defaults to native for zero
    drift) and ``is_tftest`` (tftest file →
    file_role="test_configuration") are optional; profile_version is
    terraform_profile_p2b, with the new source_variant key and the
    new file_role value disclosed alongside the version.
    """
    loss: Dict[str, int] = {}

    def hit(code: str) -> None:
        loss[code] = loss.get(code, 0) + 1

    blocks: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    type_expressions: List[Dict[str, Any]] = []
    function_calls: List[Dict[str, Any]] = []
    sensitive_explicit: List[Dict[str, Any]] = []
    derived_candidates: List[Dict[str, Any]] = []
    directives: List[Dict[str, Any]] = []
    lifecycle_rules: List[Dict[str, Any]] = []
    provisioners: List[Dict[str, Any]] = []
    block_paths = set()

    # ---- block walk: classification / provisioner / lifecycle /
    # type / explicit sensitive ----
    def walk(items, prefix):
        for item in items or []:
            if item.get("kind") != "block":
                continue
            bt = item.get("block_type")
            labels = item.get("labels") or []
            path = list(prefix) + [bt] + labels
            block_paths.add(tuple(path))
            node_kind = bt if bt in _KNOWN_BLOCK_TYPES \
                else "unknown_block_candidate"
            blocks.append({
                "node_kind": node_kind,
                "block_type": bt,
                "labels": labels,
                "path": path,
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
            })
            body = (item.get("body") or {}).get("items", []) or []

            # provisioner: record the command candidate + a blocked code,
            # zero execution
            if bt == "provisioner":
                rec: Dict[str, Any] = {
                    "path": path,
                    "labels": labels,
                    "execution_blocked": True,
                    "command_candidate": None,
                }
                for child in body:
                    if (child.get("kind") == "attr"
                            and child.get("name") == "command"):
                        expr = child.get("expr") or {}
                        # prefer the decoded value for display (template
                        # strings keep their ${}); fall back to the
                        # quoted raw
                        val = expr.get("value")
                        command = (val if isinstance(val, str)
                                   else expr.get("raw"))
                        rec["command_candidate"] = {
                            "command": command,
                            "execution_blocked": True,
                            "reason_code": "PROVISIONER_EXECUTION_BLOCKED",
                        }
                        hit("PROVISIONER_EXECUTION_BLOCKED")
                provisioners.append(rec)
            # lifecycle: rule container (no judgment)
            elif bt == "lifecycle":
                rules = [c.get("name") for c in body
                         if c.get("kind") == "attr"]
                lifecycle_rules.append({"path": path, "rules": rules})
            # variable.*.type → type_expression
            elif bt == "variable" and labels:
                for child in body:
                    if (child.get("kind") == "attr"
                            and child.get("name") == "type"):
                        raw = (child.get("expr") or {}).get("raw")
                        if isinstance(raw, str) and raw.strip():
                            type_expressions.append({
                                "path": path,
                                "raw": raw,
                                "type_ast": _parse_type_expr(raw),
                                "validated": False,
                            })

            # explicit sensitive = true (variable / output etc.)
            has_default = any(
                c.get("kind") == "attr" and c.get("name") == "default"
                for c in body
            )
            for child in body:
                if (child.get("kind") == "attr"
                        and child.get("name") == "sensitive"):
                    expr = child.get("expr") or {}
                    if expr.get("value") is True:
                        sensitive_explicit.append({
                            "path": path,
                            "sensitivity_source": "explicit_terraform_flag",
                            "value_present": (
                                has_default if bt == "variable" else None),
                        })

            walk(body, path)

    walk((tree or {}).get("items", []), [])

    entries = (contract or {}).get("entries", []) or []

    # ---- function_call modeling (raw evidence scan, zero evaluation) ----
    for e in entries:
        raw = e.get("raw_value")
        if not isinstance(raw, str) or "(" not in raw:
            continue
        calls: List[Dict[str, Any]] = []
        for m in _FUNC_CALL_RE.finditer(raw):
            name = m.group(1)
            args_text = _extract_call_args(raw, m.end() - 1)
            if args_text is None:
                # unclosed parentheses: the syntax layer owns the code,
                # not counted again here
                continue
            blocked = name in _DANGEROUS_EVAL
            reason = None
            if name in _DANGEROUS_IO:
                reason = "EXTERNAL_FILE_READ_BLOCKED"
                hit("EXTERNAL_FILE_READ_BLOCKED")
            if blocked:
                hit("FUNCTION_EVALUATION_BLOCKED")
            rec = {
                "path": e.get("path"),
                "name": name,
                "arguments": _split_top_level(args_text),
                "evaluation": "blocked" if blocked else "not_attempted",
            }
            if reason:
                rec["reason_code"] = reason
            elif blocked:
                rec["reason_code"] = "FUNCTION_EVALUATION_BLOCKED"
            calls.append(rec)
            function_calls.append(rec)
        if calls:
            e["function_calls"] = calls

    # ---- template directive %{...} candidates (unclosed ones get a code) ----
    for e in entries:
        raw = e.get("raw_value")
        if not isinstance(raw, str) or "%{" not in raw:
            continue
        for dm in _DIRECTIVE_RE.finditer(raw):
            directives.append({
                "path": e.get("path"), "syntax": dm.group(0),
            })
        opens = len(_DIRECTIVE_OPEN_RE.findall(raw))
        closed = len(_DIRECTIVE_RE.findall(raw))
        if opens > closed:
            hit("INVALID_TEMPLATE")

    # ---- entry additive field for explicit sensitive ----
    for e in entries:
        p = e.get("path") or []
        if (len(p) >= 2 and p[-1] == "sensitive"
                and e.get("value") is True):
            e["sensitivity_source"] = "explicit_terraform_flag"

    # ---- derived single-hop candidates (reference targets matching
    # sensitive naming) ----
    for e in entries:
        hits: List[Dict[str, Any]] = []
        for r in e.get("references") or []:
            target = (r.get("target") or "").strip()
            if not target:
                continue
            leaf = target.split(".")[-1].strip()
            if leaf and cc.is_sensitive_key(leaf):
                hits.append({"via": target, "masked": cc._SENSITIVE_MASK})
        if hits:
            e["derived_sensitive_candidates"] = hits
            derived_candidates.append({
                "path": e.get("path"),
                "via": [h["via"] for h in hits],
            })

    # ---- dependency edges (expression references + explicit depends_on) ----
    for e in entries:
        path = list(e.get("path") or [])
        if not path:
            continue
        attr_name = path[-1]
        source = None
        for k in range(len(path) - 1, 0, -1):
            if tuple(path[:k]) in block_paths:
                source = path[:k]
                break
        if source is None:
            source = path[:-1]
        if attr_name == "depends_on":
            raw = e.get("raw_value") or ""
            inner = raw.strip()
            if inner.startswith("[") and inner.endswith("]"):
                inner = inner[1:-1]
            for el in _split_top_level(inner):
                edges.append(
                    _make_edge(source, el, "depends_on", "depends_on"))
            continue
        for r in e.get("references") or []:
            target = (r.get("target") or "").strip()
            if not target:
                continue
            edges.append(_make_edge(
                source, target, "expression_reference", attr_name))

    # ---- tfvars file role + variable assignment association ----
    file_role: str
    variable_values: List[Dict[str, Any]] = []
    if is_tfvars:
        base = (file_name or "").lower().replace("\\", "/").split("/")[-1]
        if base == "terraform.tfvars":
            file_role = "default_variables"
        elif base.endswith(".auto.tfvars"):
            file_role = "auto_variables"
        else:
            file_role = "explicit_variables_file"
        for item in (tree or {}).get("items", []) or []:
            if item.get("kind") != "attr":
                continue
            expr = item.get("expr") or {}
            value = expr.get("value")
            if value is None:
                value = expr.get("raw")
            variable_values.append({
                "variable": item.get("name"),
                "file_value": value,
                "effective_value": None,
                "effective_status": "context_dependent",
            })
        for e in entries:
            p = e.get("path") or []
            if len(p) == 1:
                e["variable_assignment"] = True
    else:
        file_role = "configuration"

    # A tftest file identity takes precedence over configuration
    # (.tftest.hcl/.tftest.json top-level run/assert/variables are all
    # legal structure, never routed through the tfvars branch).
    if is_tftest:
        file_role = "test_configuration"

    # ---- expression-semantics sub-pass ----
    # Produces expression_semantics (an additive key) + extra dependency
    # edge specs (function arguments / for collections / splat sources
    # / dynamic for_each / state transfers / check conditions); the
    # edges all go through _make_edge (source derivation and
    # namespace classification reuse the same rules);
    # PARTIAL_EVALUATION_BLOCKED codes also land in profile loss_codes
    # (zero drift). Guarded degradation as in the main pass.
    try:
        sem, sem_edge_specs = build_expression_semantics(tree, entries, hit)
        for src_path, target, relation, attribute in sem_edge_specs:
            edges.append(_make_edge(
                src_path, target, relation, attribute))
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_sem = __import__("logging").getLogger(__name__)
        _log_sem.debug("expression semantics pass skipped: %s", exc)
        sem = {}
        sem_edge_specs = []

    profile: Dict[str, Any] = {
        "profile_version": "terraform_profile_p2b",
        "source_variant": source_variant,
        "blocks": blocks,
        "dependency_edges": edges,
        "type_expressions": type_expressions,
        "function_calls": function_calls,
        "sensitive": {
            "explicit": sensitive_explicit,
            "derived_candidates": derived_candidates,
            "name_based": "shared_channel_scan_sensitive_entries",
            "schema_sensitive": "not_applicable_p1",
        },
        "provisioners": provisioners,
        "lifecycle_rules": lifecycle_rules,
        "directives": directives,
        "file_role": file_role,
        "variable_values": variable_values,
        "expression_semantics": sem,
        "disclosures": {
            "static_reference_graph_not_execution_graph": True,
            "validation_not_executed": True,
            "effective_values_context_dependent": True,
            "markdown_summary_not_replacement": True,
        },
    }

    # Semantic-layer codes land in the profile's own loss_codes, never
    # appended to contract loss.reasons — contract loss is the
    # syntax/recovery code channel and stays zero-drift; the meaning
    # of the codes (blocked + raw preserved) is unchanged.
    if loss:
        profile["loss_codes"] = [
            f"{code}:{n}" for code, n in sorted(loss.items())
        ]

    # tftest disclosure: run/assert/variables structure registered;
    # apply inside tests is registered but never executed.
    if is_tftest:
        profile["disclosures"]["test_apply_registered_not_executed"] = True

    return profile


# ---- Markdown summary (auxiliary view, not a replacement for raw HCL) ----

def render_markdown_summary(
    profile: Optional[Dict[str, Any]],
    contract: Optional[Dict[str, Any]],
) -> str:
    """Render the profile and contract as a Markdown summary.

    Args:
        profile: terraform profile dict (may be empty).
        contract: reference contract dict (may be empty).

    Returns:
        Markdown text; empty string when the contract has no entries.
    """
    blocks = (profile or {}).get("blocks") or []
    entries = (contract or {}).get("entries", []) or []
    if not entries:
        return ""

    def entry_under(prefix):
        out = []
        for e in entries:
            p = e.get("path") or []
            if len(p) == len(prefix) + 1 and p[:len(prefix)] == list(prefix):
                out.append(e)
        return out

    lines: List[str] = []
    targets = [b for b in blocks
               if b.get("block_type") in ("resource", "variable", "module")]
    # Non-default identities (JSON variant / tftest) still render an
    # identity section even without regular render targets (tftest has
    # only run/assert); native configuration output is zero-drift.
    _identity = (
        (profile or {}).get("source_variant") == "json"
        or (profile or {}).get("file_role") == "test_configuration"
    )
    if not targets and not _identity:
        return ""

    for b in blocks:
        bt = b.get("block_type")
        path = b.get("path") or []
        if bt == "resource":
            name = ".".join(path[1:]) if len(path) > 1 else \
                ".".join(path)
            lines.append(f"## resource `{name}`")
            lines.append("")
            rows = entry_under(path)
            refs: List[str] = []
            if rows:
                lines.append("| Attribute | Expression |")
                lines.append("|---|---|")
                for e in rows:
                    attr = (e.get("path") or [])[-1]
                    # prefer the decoded value for display (str literals
                    # unquoted / template strings keep ${}); complex
                    # expressions fall back to raw
                    raw = e.get("raw_value")
                    val = e.get("value")
                    if isinstance(val, str):
                        raw = val
                    elif raw is None:
                        raw = val
                    if raw is None:
                        # container/unclosed nodes have no single-line
                        # expression to show; skip the row
                        continue
                    if cc.is_sensitive_key(attr):
                        raw = cc._SENSITIVE_MASK
                    raw = str(raw).replace("|", "\\|")
                    lines.append(f"| `{attr}` | `{raw}` |")
                lines.append("")
            for e in rows:
                for r in e.get("references") or []:
                    t = (r.get("target") or "").strip()
                    if t:
                        refs.append(t)
            if refs:
                lines.append("### References")
                lines.append("")
                for t in refs:
                    lines.append(f"- `{t}`")
                lines.append("")
        elif bt == "variable":
            labels = b.get("labels") or []
            name = labels[0] if labels else \
                (".".join(path[1:]) if len(path) > 1 else "")
            if not name:
                continue
            lines.append(f"## variable `{name}`")
            lines.append("")
            attrs = {((e.get("path") or [])[-1]): e
                     for e in entry_under(path)}
            t_raw = (attrs.get("type") or {}).get("raw_value", "any")
            lines.append(f"- Type: `{t_raw}`")
            if "default" in attrs:
                d_raw = attrs["default"].get("raw_value")
                d_val = attrs["default"].get("value")
                if isinstance(d_val, str):
                    d_raw = d_val
                elif d_raw is None:
                    d_raw = d_val
                if d_raw is None:
                    d_raw = "null"
                if cc.is_sensitive_key(name):
                    d_raw = cc._SENSITIVE_MASK
                lines.append(f"- Default: `{d_raw}`")
            sens = attrs.get("sensitive")
            if sens is not None:
                lines.append(f"- Sensitive: `{sens.get('value')}`")
            lines.append("")
        elif bt == "module":
            labels = b.get("labels") or []
            name = labels[0] if labels else \
                (".".join(path[1:]) if len(path) > 1 else "")
            if not name:
                continue
            lines.append(f"## module `{name}`")
            lines.append("")
            attrs = {((e.get("path") or [])[-1]): e
                     for e in entry_under(path)}
            if "source" in attrs:
                s_val = attrs["source"].get("value")
                s_raw = attrs["source"].get("raw_value")
                if not isinstance(s_val, str):
                    s_val = s_raw
                lines.append(f"- Source: `{s_val}`")
            if "version" in attrs:
                v_val = attrs["version"].get("value")
                v_raw = attrs["version"].get("raw_value")
                if not isinstance(v_val, str):
                    v_val = v_raw
                lines.append(f"- Version: `{v_val}`")
            inputs = [k for k in attrs
                      if k not in ("source", "version", "providers",
                                   "depends_on", "count")]
            if inputs:
                lines.append(f"- Inputs: `{', '.join(inputs)}`")
            lines.append("")

    # Expressions / State Transfers sections (counts + samples)
    lines.extend(render_expression_sections(
        (profile or {}).get("expression_semantics")))

    # JSON Variant / Test Configuration identity sections (rendered
    # only for non-default identities; native config files are
    # zero-drift).
    source_variant = (profile or {}).get("source_variant")
    if source_variant == "json":
        lines.append("### JSON Variant")
        lines.append("")
        lines.append(
            "- Source: HCL JSON mapping (structure-preserving, "
            "expressions preserved as raw strings; never evaluated)."
        )
        lines.append("")
    if (profile or {}).get("file_role") == "test_configuration":
        lines.append("### Test Configuration")
        lines.append("")
        lines.append(
            "- `run` / `assert` / `variables` structure registered; "
            "`apply` inside tests is registered but never executed "
            "(spec batch-2 §8)."
        )
        lines.append("")

    lines.append("---")
    lines.append("Markdown summary is an auxiliary view and does not "
                 "replace raw HCL (expressions, templates and heredocs "
                 "cannot be fully represented).")
    return "\n".join(lines) + "\n"
