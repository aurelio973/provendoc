# ============================================================
# Structured Config Domain
# TOML Semantic Structure Parser
# ============================================================
import datetime
import json
import logging
import math
import os
import re
import time

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)

_log = logging.getLogger(__name__)

# ============================================================
# Domain-profile identity map (a profile carries only identity and
# additive fields); case-normalized (Windows filesystems are
# case-insensitive).
# ============================================================
_TOML_PROFILES = {
    "pyproject.toml": "pyproject",
    "cargo.toml": "cargo_manifest",
    "cargo.lock": "cargo_lock",
    "poetry.toml": "poetry",
    "ruff.toml": "ruff",
    "uv.toml": "uv",
    "pdm.toml": "pdm",
}

# Conservative TOML 1.1 candidate-feature probe (flag only, no
# branching) — local time with omitted seconds `t = 12:30`
_TOML11_CANDIDATE_RE = re.compile(
    r"=\s*\d{1,2}:\d{2}\s*(?:#.*)?$", re.MULTILINE)

# datetime lexical forms (precision/timezone derived verbatim from the raw value)
_OFFSET_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})$")
_LOCAL_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_SUBSECOND_RE = re.compile(r"\.(\d+)")
_TZ_RE = re.compile(r"([Zz]|[+-]\d{2}:\d{2})$")

# Type family gating underscore-separated numeric lexical flags (four int forms + float)
_TOML_NUMERIC_VALUE_TYPES = frozenset({
    "int", "hex_int", "octal_int", "binary_int", "float",
})

# command-candidate heuristics (detect and register only; executed always false)
_COMMAND_KEY_RE = re.compile(
    r"^(build|command|script|pre-?build|post-?build|run)$", re.IGNORECASE)
_COMMAND_VALUE_RE = re.compile(
    r"^(python3?|node|npm|pnpm|yarn|bash|sh|cargo|make|go|rustc|"
    r"pip3?|uv|invoke|just)(\s|$)")
_SCRIPT_FILE_RE = re.compile(
    r"^[\w./\\+-]+\.(rs|py|sh|js|ts|ps1|bat|cmd)$", re.IGNORECASE)

_BASIC_ESCAPES = {
    '"': '"', "\\": "\\", "b": "\b", "t": "\t",
    "n": "\n", "f": "\f", "r": "\r",
}


def _value_bucket(v):
    """Homogeneity bucketing (bool before int — bool is an int subclass)."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    if v is None:
        return "null"
    return None


class TomlParser(BaseParser):
    """TOML parser.

    duplicate_key_semantics = "rejected":
        The TOML spec makes repeated keys at the same level a parse
        error (tomllib fail-fast). Duplicate keys detected in the
        contract mean the source file is invalid; recorded as a
        diagnostic fact, excluded from normal scoring.
    """

    def __init__(
        self,
        encoding="utf-8",
        tolerant=True,
    ):
        """Initialize with encoding and tolerant switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    # ========================================
    # config entry contract
    # ========================================

    def _build_contract(self, text, data):
        """Build the contract from a line scan: section/dotted-key
        paths, line numbers, comment binding.

        Values come from the tomllib parse (lookup by path); the
        scanner's path set is cross-checked against tomllib's flat
        path set, and mismatches are recorded as loss (the scanner is
        never silently trusted).
        """
        contract = cc.build_contract("toml", cc.DUP_REJECTED)
        reasons = contract["loss"]["reasons"]

        parse_ok = isinstance(data, dict) and "_error" not in data
        if not parse_ok:
            reasons.append("toml_parse_failed_contract_is_diagnostic_only")

        try:
            scanned = self._scan(text)
            entries = scanned["entries"]
            containers = scanned["containers"]
            comments = scanned["comments"]
            # table-level conflicts as independent reasons (passed through by the scanner state machine)
            reasons.extend(scanned.get("conflicts", ()))

            # backfill parsed values from the tomllib result
            if parse_ok:
                flat = cc.flatten_structured_data(data)
                for entry in entries:
                    if entry["value_state"] in (
                        cc.VALUE_STATE_CONTAINER,
                        cc.VALUE_STATE_EMPTY_LIST,
                        cc.VALUE_STATE_EMPTY_DICT,
                    ):
                        # inline-container entry — value from struct-tree
                        # navigation (flat only holds leaf scalar paths)
                        node = self._lookup_path(data, entry["path"])
                        if node is self._PATH_MISS:
                            reasons.append(
                                f"path_lookup_miss:{entry['path_str']}"
                            )
                            entry["value"] = None
                        else:
                            entry["value"] = cc.json_safe(node)
                        continue
                    v = flat.get(entry["path_str"], None)
                    if entry["path_str"] not in flat:
                        reasons.append(f"path_lookup_miss:{entry['path_str']}")
                        entry["value"] = None
                    else:
                        if isinstance(
                            v, (datetime.datetime, datetime.date, datetime.time)
                        ):
                            # datetime-family values keep the raw lexical
                            # form (json_safe's str() would drop T/Z)
                            entry["value"] = (
                                entry.get("raw_value") or cc.json_safe(v)
                            )
                        else:
                            entry["value"] = cc.json_safe(v)
                        if isinstance(v, (dict, list)):
                            entry["value_state"] = cc.VALUE_STATE_CONTAINER
                        # type layer (tomllib authoritative type + raw lexical refinement)
                        vt = self._value_type_of(v, entry.get("raw_value"))
                        if vt is not None:
                            entry["value_type"] = vt
                            entry["type_source"] = cc.TYPE_SOURCE_SOURCE
                # inline-container derived-projection expansion
                # (parent=source fact, child=derived projection,
                #   source_precision="line_only")
                self._expand_inline_entries(data, entries, containers)
                # verify: scanner scalar paths ⊆ tomllib paths (some
                # false positives tolerated; container entries are
                # backfilled via struct-tree navigation, not checked here)
                miss = sum(
                    1 for e in entries
                    if e["value_state"] == cc.VALUE_STATE_SCALAR
                    and e["path_str"] not in flat
                )
                if miss:
                    reasons.append(f"path_binding_verification_failed:{miss}")
            else:
                # pathological-document degraded backfill — scanner
                # lexical inference recovers line by line (a candidate
                # line loses only itself, never poisoning the whole
                # value layer); type_source=lexical_fallback never
                # masquerades as tomllib authority.
                degraded = 0
                for entry in entries:
                    if entry["value_state"] != cc.VALUE_STATE_SCALAR:
                        continue
                    v, vt = self._infer_lexical_value(entry.get("raw_value"))
                    if vt is not None:
                        entry["value"] = v
                        entry["value_type"] = vt
                        entry["type_source"] = "lexical_fallback"
                        degraded += 1
                if degraded:
                    reasons.append("value_backfill_degraded")

            # datetime lexical fields (four forms / precision digits / timezone verbatim, derived from raw)
            for entry in entries:
                self._stamp_datetime_lexical(entry)
            # underscore-separated numeric lexical flag (raw already
            # preserves it; this makes it explicit). Gated on the
            # numeric-family value_type — a hex 0xdead_beef underscore
            # sits between letters, not enumerable as a regex shape,
            # so the type family is the criterion
            for entry in entries:
                if (
                    entry.get("lexical_format") is None
                    and entry.get("value_type") in _TOML_NUMERIC_VALUE_TYPES
                    and "_" in (entry.get("raw_value") or "")
                ):
                    entry["lexical_format"] = {"underscores": True}

            contract["entries"] = entries
            contract["containers"] = containers
            contract["comments"] = comments
            # Sensitive disclosure via the shared
            # contracts.scan_sensitive_entries helper — dual-channel
            # key name + value patterns, never store value, raw as
            # the fact source; the unified rules are a superset:
            # array-element entries gain disclosure via the last
            # effective path segment, additive only.
            sensitive_count = cc.scan_sensitive_entries(entries)
            if sensitive_count:
                reasons.append(
                    f"sensitive_entry_values:{sensitive_count}"
                )
            cc.resolve_references(contract)
            # list-container homogeneity (fact-layer input for the syntax/schema split)
            self._stamp_homogeneity(contract)
        except Exception as e:  # pragma: no cover - final safety net
            reasons.append(f"contract_build_failed: {e}")

        return contract

    # ----------------------------------------
    # Line scanner
    # ----------------------------------------

    def _scan(self, text):
        lines = text.splitlines()
        n = len(lines)
        entries, containers, comments = [], [], []
        key_lines = []   # [{line, col, path, kind}]
        section_path = []
        conflicts = []           # table-level conflicts (toml_conflict:* reason family)
        explicit_tables = set()     # paths declared by [header]s (duplicate-header detection)
        inline_table_paths = set()  # paths defined by inline {…} values (reopening detection)
        key_defined_paths = set()   # paths defined by key assignments (incl. dotted middle segments)
        section_seen = {}     # path_str → container record (header dedup)
        entry_map = {}        # path_str → entry record (duplicate-key diagnostic merge)
        array_occurrences = {}  # resolved path → occurrence count
        array_last = {}       # resolved AoT path → current element index
                                  # (TOML: subtables attach to the parent array's last element)

        i = 0
        pending_block = None  # (start, end, indent, text) comment block awaiting binding
        in_ml = None          # multiline-string delimiter (""" or ''')

        def flush_block(target):
            """Bind the pending comment block to target (a key/section line record)."""
            nonlocal pending_block
            if pending_block is None or target is None:
                if pending_block is not None:
                    s, e, _, btext = pending_block
                    comments.append(cc.make_comment(
                        "block", btext, "\n".join(lines[s:e + 1]), s, e,
                        bound_path=None,
                    ))
                pending_block = None
                return
            s, e, _, btext = pending_block
            comments.append(cc.make_comment(
                "block", btext, "\n".join(lines[s:e + 1]), s, e,
                bound_path=target["path"], bound_kind=target["kind"],
            ))
            bound_desc.setdefault(cc.path_str(target["path"]), (btext, [s, e]))
            pending_block = None

        bound_desc = {}

        def add_entry(entry_path, key, value_raw, value_state, i, j,
                      raw_key=None, key_segment_style=None,
                      array_style=None):
            """Record the entry (incl. TOML duplicate-key diagnostic merge; raw-key/array fidelity fields)."""
            desc = bound_desc.get(cc.path_str(entry_path))
            p = cc.path_str(entry_path)
            if p in entry_map:
                # duplicate key (rejected by the TOML spec): merged as a
                # diagnostic fact; occ carries raw_key/key_segment_style —
                # when a quoted key segment renders identical to a
                # dotted form (path_str ambiguity), both source forms
                # stay traceable on the chain
                existing = entry_map[p]
                occ = {"value": value_raw, "line": i,
                       "raw_key": raw_key,
                       "key_segment_style": list(key_segment_style or [])}
                if existing["override_chain"] is None:
                    existing["override_chain"] = [
                        {"value": existing["raw_value"],
                         "line": existing["line_start"],
                         "raw_key": existing.get("raw_key"),
                         "key_segment_style": list(
                             existing.get("key_segment_style") or [])},
                        occ,
                    ]
                else:
                    existing["override_chain"].append(occ)
                existing["duplicate"] = True
                existing["raw_value"] = value_raw
                existing["raw_key"] = raw_key
                existing["key_segment_style"] = list(key_segment_style or [])
                existing["line_start"] = i
                existing["line_end"] = j
            else:
                entry = cc.make_entry(
                    entry_path,
                    key,
                    value_raw,
                    None,  # backfilled from tomllib by _build_contract
                    value_state=value_state,
                    line_start=i, line_end=j,
                    description=desc[0] if desc else None,
                    description_lines=desc[1] if desc else None,
                )
                # raw key + key-segment style + array style (additive;
                # filled only by the TOML channel, zero drift elsewhere)
                entry["raw_key"] = raw_key
                entry["key_segment_style"] = list(key_segment_style or [])
                if array_style is not None:
                    entry["array_style"] = array_style
                entry_map[p] = entry
                entries.append(entry)

        while i < n:
            line = lines[i]
            stripped = line.strip()

            if in_ml is not None:
                if in_ml in line:
                    in_ml = None
                i += 1
                continue

            if not stripped:
                i += 1
                continue

            # comment line
            if stripped.startswith("#"):
                indent = len(line) - len(line.lstrip())
                if pending_block is None:
                    pending_block = (i, i, indent, stripped.lstrip("#").strip())
                else:
                    s, _, ind, t = pending_block
                    pending_block = (s, i, ind, t)
                i += 1
                continue

            # array-of-tables header [[a.b]]
            if stripped.startswith("[[") and stripped.endswith("]]"):
                raw_name = stripped[2:-2].strip()
                # AoT context resolution: [[servers.network]] after
                # [[servers]] → servers.<current index>.network
                # (aligned with tomllib's flattening)
                base = self._resolve_aot_context(
                    self._split_key(raw_name), array_last
                )
                cnt = array_occurrences.get(cc.path_str(base), 0)
                array_occurrences[cc.path_str(base)] = cnt + 1
                array_last[cc.path_str(base)] = cnt
                # implicit parent chain (dict); the array itself = list container
                self._ensure_containers(base[:-1], containers, section_seen, i)
                p = cc.path_str(base)
                if p not in section_seen:
                    rec = cc.make_container(base, "list", line_start=i, line_end=i)
                    containers.append(rec)
                    section_seen[p] = rec
                # tomllib: a.b = [ {...}, {...} ] → index segment
                section_path = base + [str(cnt)]
                self._ensure_containers(section_path, containers, section_seen, i)
                kl = {"line": i, "col": 0, "path": list(section_path),
                      "kind": "section"}
                key_lines.append(kl)
                flush_block(kl)
                i += 1
                continue

            # table header [a.b]
            if stripped.startswith("[") and stripped.endswith("]"):
                raw_name = stripped[1:-1].strip()
                # same AoT context resolution: [fruit.physical] attaches
                # to the last [[fruit]] element → fruit.<idx>.physical
                base = self._resolve_aot_context(
                    self._split_key(raw_name), array_last
                )
                # table-level conflict diagnostics (four forms as independent reasons, scanner state machine)
                hp = cc.path_str(base)
                if hp in inline_table_paths:
                    conflicts.append(
                        f"toml_conflict:table_redefined_after_inline:{hp}")
                elif hp in key_defined_paths:
                    conflicts.append(
                        f"toml_conflict:table_redefined_after_key:{hp}")
                elif hp in explicit_tables:
                    conflicts.append(
                        f"toml_conflict:table_redefined:{hp}")
                explicit_tables.add(hp)
                section_path = base
                self._ensure_containers(base, containers, section_seen, i)
                kl = {"line": i, "col": 0, "path": list(base), "kind": "section"}
                key_lines.append(kl)
                flush_block(kl)
                i += 1
                continue

            # key = value
            eq = self._find_eq(line)
            if eq is not None:
                key_raw = line[:eq].strip()
                # quoted-key-segment fidelity (raw_key + segment style, resolves path_str ambiguity)
                key_parts, key_styles = self._split_key_detailed(key_raw)
                entry_path = section_path + key_parts
                p = cc.path_str(entry_path)
                # key-assignment path registration (incl. dotted middle segments — later [t] reopen detection)
                key_defined_paths.add(p)
                # implicit containers (dotted-key middle segments)
                for d in range(len(section_path), len(entry_path) - 1):
                    mid = cc.path_str(entry_path[:d + 1])
                    key_defined_paths.add(mid)
                    if mid in inline_table_paths:
                        conflicts.append(
                            f"toml_conflict:extended_inline_table:{mid}")
                    self._ensure_containers(entry_path[:d + 1], containers,
                                            section_seen, i,
                                            implicit=True)
                # consume multi-line value
                j, in_ml_after, value_raw = self._consume_value(lines, i, eq)
                # inline comment
                inline_idx = self._inline_comment_index(value_raw)
                inline_text = None
                if inline_idx is not None:
                    inline_text = value_raw[inline_idx:].lstrip("#").strip()
                    value_raw = value_raw[:inline_idx].rstrip()

                col = len(line) - len(line.lstrip())
                kl = {"line": i, "col": col, "path": entry_path, "kind": "key"}
                key_lines.append(kl)
                flush_block(kl)

                value_state = cc.VALUE_STATE_SCALAR
                if value_raw.strip() == "[]":
                    value_state = cc.VALUE_STATE_EMPTY_LIST
                elif value_raw.strip() == "{}":
                    value_state = cc.VALUE_STATE_EMPTY_DICT
                elif value_raw.strip().startswith(("[", "{")):
                    value_state = cc.VALUE_STATE_CONTAINER
                vstrip = value_raw.strip()
                # inline-table path registration ([t] reopen / dotted extension detection)
                if vstrip.startswith("{"):
                    inline_table_paths.add(p)
                # array-style fields (form / trailing comma / spans lines)
                array_style = None
                if vstrip.startswith("["):
                    inner = (vstrip[1:-1].rstrip()
                             if vstrip.endswith("]")
                             else vstrip[1:].rstrip())
                    array_style = {
                        "form": ("multiline"
                                 if (j > i or "\n" in vstrip) else "inline"),
                        "trailing_comma": inner.endswith(","),
                        "spans_lines": j > i,
                    }

                # inline-container keys also yield an entry (source
                # fact); children are expanded as derived projections
                # by _build_contract
                add_entry(
                    entry_path,
                    key_parts[-1] if key_parts else key_raw,
                    value_raw.strip(),
                    value_state,
                    i, j,
                    raw_key=key_raw,
                    key_segment_style=key_styles,
                    array_style=array_style,
                )

                if value_state in (cc.VALUE_STATE_EMPTY_LIST,
                                   cc.VALUE_STATE_EMPTY_DICT,
                                   cc.VALUE_STATE_CONTAINER):
                    # inline container: kind recognizable; child count backfilled by expansion
                    if value_state == cc.VALUE_STATE_EMPTY_LIST:
                        ctype = "list"
                    elif value_state == cc.VALUE_STATE_EMPTY_DICT:
                        ctype = "dict"
                    else:
                        ctype = self._container_kind(value_raw)
                    containers.append(cc.make_container(
                        entry_path, ctype,
                        child_count=(None
                                     if value_state == cc.VALUE_STATE_CONTAINER
                                     else 0),
                        value_state=value_state,
                        line_start=i, line_end=j,
                    ))
                if inline_text:
                    comments.append(cc.make_comment(
                        "inline", inline_text, "", i, i,
                        bound_path=entry_path, bound_kind="key",
                    ))

                in_ml = in_ml_after
                i = j + 1
                continue

            # unrecognized line (scanner tolerance)
            i += 1

        # finalize: trailing comment block → orphan
        flush_block(None)

        # backfill container child_count + empty-container tri-state correction
        cc.fill_child_counts(entries, containers)

        # backfill description onto containers (block binding is only known after the container record exists)
        for c in containers:
            d = bound_desc.get(c["path_str"])
            if d and not c.get("description"):
                c["description"] = d[0]
                c["description_lines"] = d[1]
        for e in entries:
            if e.get("description") is None:
                d = bound_desc.get(e["path_str"])
                if d:
                    e["description"] = d[0]
                    e["description_lines"] = d[1]

        return {"entries": entries, "containers": containers,
                "comments": comments, "conflicts": conflicts}

    # ----------------------------------------
    # Scanner helpers
    # ----------------------------------------

    _PATH_MISS = object()  # structural-tree navigation miss sentinel

    @classmethod
    def _lookup_path(cls, data, path):
        """Navigate the parsed structure tree by path segments; return _PATH_MISS on a miss.

        list index segments are cast to int; out-of-range segment counts or
        type mismatches all count as a miss.
        """
        node = data
        for seg in path:
            if isinstance(node, dict):
                if not isinstance(seg, str) or seg not in node:
                    return cls._PATH_MISS
                node = node[seg]
            elif isinstance(node, list):
                try:
                    node = node[int(seg)]
                except (ValueError, IndexError):
                    return cls._PATH_MISS
            else:
                return cls._PATH_MISS
        return node

    @staticmethod
    def _canonical_raw(value):
        """Canonical raw_value rendering for derived entries (same convention as the JSON parser)."""
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    def _expand_inline_entries(self, data, entries, containers):
        """Expand inline-container derived projections.

        Contract semantics:
            - the parent entry (source fact) is produced by the scanner, value = the full container
            - child entries = derived projections: values come from the
              tomllib authoritative structure, line numbers inherit from the parent entry, source_precision="line_only"
            - nested intermediate containers get derived container records
              (hierarchy connectivity stays unbroken), child_count backfilled

        Expansion preserves document order: derived subtrees are inserted right after the parent entry.
        """
        container_by_path = {}
        for c in containers:
            container_by_path.setdefault(c["path_str"], c)

        out = []
        for e in entries:
            out.append(e)
            if e.get("source_precision") is not None:
                continue  # already a derived entry (no re-entry)
            if e["value_state"] not in (
                cc.VALUE_STATE_CONTAINER,
                cc.VALUE_STATE_EMPTY_LIST,
                cc.VALUE_STATE_EMPTY_DICT,
            ):
                continue
            node = self._lookup_path(data, e["path"])
            if node is self._PATH_MISS or not isinstance(node, (dict, list)):
                # lookup failed / scalar misjudgment: no children to
                # expand; the container record keeps child_count=None
                # (not-enumerated semantics)
                continue
            # backfill the parent container's child_count (direct
            # children) — the presentation layer uses it to skip the
            # duplicate marker unit
            c = container_by_path.get(e["path_str"])
            if c is not None and c.get("child_count") is None:
                c["child_count"] = len(node)
                if c["child_count"] == 0:
                    c["value_state"] = (
                        cc.VALUE_STATE_EMPTY_DICT
                        if c.get("container_type") == "dict"
                        else cc.VALUE_STATE_EMPTY_LIST
                    )
            subtree = []
            self._project_children(
                node, e["path"], e, containers, subtree,
                container_by_path,
            )
            out.extend(subtree)

        entries[:] = out

        # recompute container child_count / empty-container tri-state (incl. derived intermediate containers)
        cc.fill_child_counts(entries, containers)

    def _project_children(
        self, node, base_path, parent_entry, containers, out,
        container_by_path,
    ):
        """Recursively project the inline-container subtree as derived entries (incl. intermediate container records)."""
        if isinstance(node, dict):
            items = list(node.items())
        elif isinstance(node, list):
            items = [(str(i), v) for i, v in enumerate(node)]
        else:
            return
        for key, value in items:
            child_path = base_path + [str(key)]
            if isinstance(value, (dict, list)):
                state = cc.VALUE_STATE_CONTAINER
            elif value is None:
                state = cc.VALUE_STATE_EXPLICIT_NULL
            else:
                state = cc.VALUE_STATE_SCALAR
            out.append(cc.make_entry(
                child_path,
                str(key),
                self._canonical_raw(value),
                cc.json_safe(value),
                value_state=state,
                line_start=parent_entry["line_start"],
                line_end=parent_entry["line_end"],
                doc_index=parent_entry.get("doc_index", 0),
                source_precision=cc.SOURCE_PRECISION_LINE_ONLY,
            ))
            if isinstance(value, (dict, list)):
                # intermediate container: derived container record (connectivity)
                if cc.path_str(child_path) not in container_by_path:
                    rec = cc.make_container(
                        child_path,
                        "dict" if isinstance(value, dict) else "list",
                        child_count=len(value),
                        value_state=(
                            cc.VALUE_STATE_EMPTY_DICT
                            if isinstance(value, dict) and not value
                            else cc.VALUE_STATE_EMPTY_LIST
                            if isinstance(value, list) and not value
                            else cc.VALUE_STATE_CONTAINER
                        ),
                        line_start=parent_entry["line_start"],
                        line_end=parent_entry["line_end"],
                        doc_index=parent_entry.get("doc_index", 0),
                    )
                    containers.append(rec)
                    container_by_path[cc.path_str(child_path)] = rec
                self._project_children(
                    value, child_path, parent_entry, containers, out,
                    container_by_path,
                )

    @staticmethod
    def _resolve_aot_context(segments, array_last):
        """AoT context index resolution (TOML semantics).

        `[[a.b]]` / `[a.b]` after `[[a]]` attaches to the **last element**
        of the a array; contract paths must match tomllib's flattened
        paths: `[[a]]` + `[[a.b]]` → a.<idx_a>.b.<idx_b>.

        segments: header key segments (_split_key output)
        array_last: resolved AoT path → current element index
        """
        if not array_last or not segments:
            return list(segments)
        resolved = []
        for seg in segments[:-1]:
            resolved.append(seg)
            p = cc.path_str(resolved)
            if p in array_last:
                resolved.append(str(array_last[p]))
        resolved.append(segments[-1])
        return resolved

    @staticmethod
    def _split_key(raw):
        """Split a dotted key (quote-aware). 'a."b.c"' → ['a', 'b.c']"""
        parts, buf, quote = [], "", None
        for ch in raw:
            if quote:
                if ch == quote:
                    quote = None
                else:
                    buf += ch
            elif ch in ('"', "'"):
                quote = ch
            elif ch == ".":
                parts.append(buf.strip())
                buf = ""
            else:
                buf += ch
        if buf.strip():
            parts.append(buf.strip())
        return [p for p in parts if p != ""]

    @staticmethod
    def _split_key_detailed(raw):
        """Split a dotted key (quote-aware) with key-segment style fidelity.

        'a."b.c"' → (['a', 'b.c'], ['bare', 'basic']);
        styles: 'bare' | 'basic' (double-quoted, escapable) | 'literal' (single-quoted).
        When a quoted key segment renders identical to a dotted form
        (path_str ambiguity), consumers distinguish the two source forms
        via key_segment_style.
        Empty-segment filtering matches _split_key (quoted empty keys are kept).
        """
        parts, styles = [], []
        buf, quote = "", None
        seg_style = "bare"
        for ch in raw:
            if quote:
                if ch == quote:
                    quote = None
                else:
                    buf += ch
            elif ch in ('"', "'"):
                seg_style = "basic" if ch == '"' else "literal"
                quote = ch
            elif ch == ".":
                if buf.strip() or seg_style != "bare":
                    parts.append(buf.strip())
                    styles.append(seg_style)
                buf, seg_style = "", "bare"
            else:
                buf += ch
        if buf.strip() or seg_style != "bare":
            parts.append(buf.strip())
            styles.append(seg_style)
        return parts, styles

    @staticmethod
    def _find_eq(line):
        """Index of the first '=' in the line (outside quotes)."""
        quote = None
        for idx, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "=":
                return idx
        return None

    @staticmethod
    def _container_kind(value_raw):
        v = value_raw.strip()
        return "list" if v.startswith("[") else "dict"

    @staticmethod
    def _consume_value(lines, start, eq_col):
        """Consume a multi-line value (array / multiline string) starting after eq on the start line.

        Returns (last_line, in_ml_delimiter, value_raw_text).
        """
        first = lines[start][eq_col + 1:]
        buf = [first]
        j = start
        quote = None
        depth = 0

        def scan(s):
            """Scan one line of text, updating quote state and bracket depth."""
            nonlocal quote, depth
            k = 0
            while k < len(s):
                ch = s[k]
                if quote:
                    if s.startswith(quote, k):
                        # quote close (single-char and triple quotes handled
                        # alike; otherwise a multiline string never closes
                        # and would swallow the whole file)
                        n = len(quote)
                        quote = None
                        k += n
                        continue
                    k += 1
                    continue
                if s.startswith('"""', k) or s.startswith("'''", k):
                    quote = s[k:k + 3]
                    k += 3
                    continue
                if ch in ('"', "'"):
                    quote = ch
                elif ch in "[{(":
                    depth += 1
                elif ch in "]})":
                    depth = max(0, depth - 1)
                k += 1

        scan(first)
        while (quote or depth > 0) and j + 1 < len(lines):
            j += 1
            buf.append(lines[j])
            scan(lines[j])
        # unclosed multiline string → return the residual state
        in_ml = quote if (quote and len(quote) == 3) else None
        return j, in_ml, "\n".join(buf)

    @staticmethod
    def _inline_comment_index(value_raw):
        """Index of a '#' outside quotes in the value text."""
        quote = None
        for idx, ch in enumerate(value_raw):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "#" and (idx == 0 or value_raw[idx - 1] in (" ", "\t")):
                return idx
        return None

    def _ensure_containers(self, path, containers, section_seen, line,
                           implicit=False):
        """Ensure the container chain along path exists (incl. section dedup and implicit containers)."""
        for d in range(1, len(path) + 1):
            sub = path[:d]
            p = cc.path_str(sub)
            if p in section_seen:
                continue
            record = cc.make_container(
                sub, "dict", line_start=line, line_end=line,
                implicit=implicit or d < len(path),
            )
            containers.append(record)
            section_seen[p] = record

    # ----------------------------------------
    # Contract helper methods
    # ----------------------------------------

    @staticmethod
    def _value_type_of(v, raw=None):
        """Value-type layer: authoritative tomllib types + raw lexical refinement.

        int-prefix refinement: 0x/0o/0b → hex/octal/binary (contracts
        constants); datetime family → timestamp/date/time (the
        four-form breakdown lives in _stamp_datetime_lexical);
        dict/list are not typed (containers are expressed via
        value_state); unknown types return None.
        """
        if isinstance(v, bool):
            return cc.VALUE_TYPE_BOOL
        if isinstance(v, int):
            if raw:
                r = raw.strip().lower()
                if r.startswith("0x"):
                    return cc.VALUE_TYPE_HEX_INT
                if r.startswith("0o"):
                    return cc.VALUE_TYPE_OCTAL_INT
                if r.startswith("0b"):
                    return cc.VALUE_TYPE_BINARY_INT
            return cc.VALUE_TYPE_INT
        if isinstance(v, float):
            # float special-value refinement (family constant group, YAML precedent)
            if math.isinf(v):
                return cc.VALUE_TYPE_FLOAT_INFINITY if v > 0 \
                    else cc.VALUE_TYPE_FLOAT_NEG_INFINITY
            if math.isnan(v):
                return cc.VALUE_TYPE_FLOAT_NAN
            return cc.VALUE_TYPE_FLOAT
        if isinstance(v, str):
            return cc.VALUE_TYPE_STRING
        if isinstance(v, datetime.datetime):
            return cc.VALUE_TYPE_TIMESTAMP
        if isinstance(v, datetime.date):
            return cc.VALUE_TYPE_DATE
        if isinstance(v, datetime.time):
            return cc.VALUE_TYPE_TIME
        if v is None:
            return cc.VALUE_TYPE_NULL
        return None

    @classmethod
    def _infer_lexical_value(cls, raw):
        """Lexical scalar inference for degraded-path backfill (when no tomllib authority exists).

        Only unambiguous forms are inferred: booleans / decimal and
        prefixed integers / floats / quoted strings (basic escapes
        decoded) / the four datetime forms (value keeps the raw form
        verbatim); everything else (arrays/tables/complex values)
        returns (None, None) rather than guessing.
        """
        if raw is None:
            return None, None
        s = raw.strip()
        if not s:
            return None, None
        if s == "true":
            return True, cc.VALUE_TYPE_BOOL
        if s == "false":
            return False, cc.VALUE_TYPE_BOOL
        if (len(s) >= 2 and s[0] in ('"', "'") and s.endswith(s[0])):
            inner = s[1:-1]
            if s[0] == '"':
                inner = cls._decode_basic_string(inner)
            return inner, cc.VALUE_TYPE_STRING
        if _OFFSET_DT_RE.match(s) or _LOCAL_DT_RE.match(s):
            return s, cc.VALUE_TYPE_TIMESTAMP
        if _DATE_RE.match(s):
            return s, cc.VALUE_TYPE_DATE
        if _TIME_RE.match(s):
            return s, cc.VALUE_TYPE_TIME
        cleaned = s.replace("_", "")
        try:
            vt = cc.VALUE_TYPE_INT
            low = cleaned.lower()
            if low.startswith("0x"):
                vt = cc.VALUE_TYPE_HEX_INT
            elif low.startswith("0o"):
                vt = cc.VALUE_TYPE_OCTAL_INT
            elif low.startswith("0b"):
                vt = cc.VALUE_TYPE_BINARY_INT
            return int(cleaned, 0), vt
        except ValueError:
            pass
        try:
            return float(cleaned), cc.VALUE_TYPE_FLOAT
        except ValueError:
            pass
        return None, None

    @staticmethod
    def _decode_basic_string(inner):
        r"""Decode escapes in a basic string (\b \t \n \f \r \" \\ + \uXXXX/\UXXXXXXXX)."""
        if "\\" not in inner:
            return inner
        out, i = [], 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in _BASIC_ESCAPES:
                    out.append(_BASIC_ESCAPES[nxt])
                    i += 2
                    continue
                if nxt in ("u", "U"):
                    width = 4 if nxt == "u" else 8
                    hexpart = inner[i + 2:i + 2 + width]
                    try:
                        out.append(chr(int(hexpart, 16)))
                        i += 2 + width
                        continue
                    except ValueError:
                        pass
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _stamp_datetime_lexical(entry):
        """Datetime lexical fields (derived from raw as the single source of truth, additive).

        Four forms: offset_datetime / local_datetime / local_date /
        local_time; precision: fractional-second digits (0 when
        absent); tz: the timezone lexeme verbatim ("Z"/"+08:00"…,
        None for local forms).
        """
        vt = entry.get("value_type")
        if vt not in (cc.VALUE_TYPE_TIMESTAMP, cc.VALUE_TYPE_DATE,
                      cc.VALUE_TYPE_TIME):
            return
        raw = (entry.get("raw_value") or "").strip()
        if not raw:
            return
        info = {}
        m = _SUBSECOND_RE.search(raw)
        precision = len(m.group(1)) if m else 0
        if _OFFSET_DT_RE.match(raw):
            info["form"] = "offset_datetime"
            info["precision"] = precision
            tz = _TZ_RE.search(raw)
            info["tz"] = tz.group(1) if tz else None
        elif _LOCAL_DT_RE.match(raw):
            info["form"] = "local_datetime"
            info["precision"] = precision
            info["tz"] = None
        elif _DATE_RE.match(raw):
            info["form"] = "local_date"
            info["precision"] = 0
            info["tz"] = None
        elif _TIME_RE.match(raw):
            info["form"] = "local_time"
            info["precision"] = precision
            info["tz"] = None
        else:
            return
        entry["datetime_lexical"] = info

    @staticmethod
    def _stamp_homogeneity(contract):
        """List-container homogeneity (structural projection, zero IO, additive).

        Buckets direct-child entries by value type; ≥2 children with a
        unique type → homogeneous=True; mixed types → False; fewer
        than 2 children or unknown types → not stamped (None
        semantics, no guessing). element_types discloses the actual
        type set for the syntax/schema separation consumers.
        """
        buckets = {}
        for e in contract.get("entries") or []:
            path = e.get("path") or []
            if len(path) < 2:
                continue  # top-level scalar: parent is the document root, not a container
            t = _value_bucket(e.get("value"))
            if t is None:
                continue
            buckets.setdefault(cc.path_str(path[:-1]), []).append(t)
        for c in contract.get("containers") or []:
            if c.get("container_type") != "list":
                continue
            types = buckets.get(c.get("path_str"))
            if not types or len(types) < 2:
                continue
            c["element_types"] = sorted(set(types))
            c["homogeneous"] = len(c["element_types"]) == 1

    @staticmethod
    def _extract_lock_graph(data):
        """Cargo.lock dependency graph (structural projection, zero IO; cargo_lock profile only).

        packages: [{name, version}]; edges: [{from, to}] (dependency
        names — version resolution is left to consumers, since a
        lockfile uniquely pins each name); dependency targets absent
        from the lockfile (git/external sources) → disclosed under
        unresolved_targets. Missing package section or wrong shape
        → None (no guessing).
        """
        if not isinstance(data, dict):
            return None
        packages = data.get("package")
        if not isinstance(packages, list) or not packages:
            return None
        nodes, edges, seen = [], [], set()
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            name = pkg.get("name")
            if not isinstance(name, str):
                continue
            nodes.append({"name": name, "version": pkg.get("version")})
            seen.add(name)
            deps = pkg.get("dependencies")
            if isinstance(deps, list):
                for dep in deps:
                    if isinstance(dep, str):
                        tokens = dep.split()
                        if tokens:
                            edges.append({"from": name, "to": tokens[0]})
        if not nodes:
            return None
        graph = {"packages": nodes, "edges": edges}
        unresolved = sorted({e["to"] for e in edges if e["to"] not in seen})
        if unresolved:
            graph["unresolved_targets"] = unresolved
        return graph

    @staticmethod
    def _extract_command_candidates(entries):
        """Command-candidate heuristic registration (executed is always false).

        A key-name hit (build/command/script/pre-build/run…) with a
        command-like value (interpreter prefix / script filename) →
        registered; pure fact disclosure, no executability judgment,
        and never enters any execution path.
        """
        out = []
        for e in entries:
            if e.get("value_state") != cc.VALUE_STATE_SCALAR:
                continue
            key = str(e.get("key") or "")
            value = e.get("value")
            raw = str(value) if value is not None else ""
            if not _COMMAND_KEY_RE.match(key):
                continue
            if not (_COMMAND_VALUE_RE.match(raw)
                    or _SCRIPT_FILE_RE.match(raw)):
                continue
            out.append({
                "path": e.get("path_str"),
                "key": key,
                "value": raw,
                "executed": False,
            })
        return out

    # ========================================
    # parse
    # ========================================

    def parse(self, file_path):
        """Parse a TOML file into a Document (duplicate keys are illegal per spec)."""
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "toml", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            # toml parse
            try:
                data = tomllib.loads(text)
                metadata["parse_success"] = True
            except Exception as e:
                data = {"_error": str(e)}
                metadata["parse_success"] = False
                metadata["warnings"].append("toml parse failed")

            metadata["top_level_keys"] = (
                list(data.keys()) if isinstance(data, dict) else []
            )

            # ConfigEntry contract (fact layer; failure never blocks parsing)
            metadata["config_entries"] = self._build_contract(text, data)

            # domain profile identity (exact basename, case-normalized;
            # Core/Profile separation — identity and additive fields only)
            basename = os.path.basename(file_path).lower()
            toml_profile = _TOML_PROFILES.get(basename)
            if toml_profile:
                metadata["toml_profile"] = toml_profile
            # conservative TOML 1.1 candidate-feature detection (mark
            # only, no branching) — second-omitted local times (e.g.
            # `t = 12:30`) are currently rejected by tomllib
            if _TOML11_CANDIDATE_RE.search(text):
                metadata["toml11_candidate"] = True

            contract = metadata["config_entries"]
            # command-candidate heuristic registration (executed is always false)
            command_candidates = self._extract_command_candidates(
                contract.get("entries") or []
            )
            if command_candidates:
                metadata["command_candidates"] = command_candidates
            # Cargo.lock dependency graph (cargo_lock profile only)
            if (toml_profile == "cargo_lock"
                    and metadata.get("parse_success")):
                lock_graph = self._extract_lock_graph(data)
                if lock_graph is not None:
                    metadata["toml_lock"] = lock_graph

            # object-level unresolved registry: derived from the
            # contract's tri-state existing fact sources (toml declares
            # rejected, no derivation here), never rewrites original
            # keys, written only when non-empty; guarded silent
            # degradation that never blocks the main parse
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            # honest failure: TOML syntax is illegal AND the tolerant
            # scanner recovered nothing (no entries / containers /
            # comments). With no recoverable content, we must not
            # pretend success and let the presentation layer emit an
            # empty document — aligned with the narrative domain's
            # "nothing extractable" failure contract.
            contract = metadata["config_entries"]
            if (
                not metadata.get("parse_success")
                and not contract.get("entries")
                and not contract.get("containers")
                and not contract.get("comments")
            ):
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "toml syntax error with no recoverable content: "
                    + str(data.get("_error", "") if isinstance(data, dict)
                          else "")
                )
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                metadata["parse_duration_ms"] = round(elapsed_ms, 3)
                return Document(
                    text="",
                    metadata={**metadata, "structured_data": data},
                    source_text=ctx.source_text,
                )

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)

            return Document(
                text=text,
                metadata={**metadata, "structured_data": data},
                source_text=ctx.source_text,
            )

        except Exception as e:
            metadata = {"parse_failed": True, "parse_error": str(e)}
            return Document(text="", metadata=metadata)

    # ========================================
    # Multi-file cascade merge (thin wrapper over cc.merge_contracts)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Multi-file cascade merge: later files shadow earlier ones (shared cc.merge_contracts layer).

        Each file goes through the existing single-file contract
        channel (with all disclosure semantics; a source file's
        degraded contract and loss.reasons for duplicate keys ride
        along verbatim — fact disclosure). The merged header's
        ``duplicate_key_semantics`` is uniformly ``last_file_wins``
        (cross-file shadowing semantics of the merge layer; the
        in-file rejected semantics stays in the entries/loss layer —
        the two layers are orthogonal). Returns the merged contract
        dict (not a Document — line-number semantics rely on
        source_file). The base parse_many semantics stays frozen.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            contracts.append(meta.get("config_entries") or cc.build_contract(
                "toml", cc.DUP_REJECTED))
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
        return cc.merge_contracts("toml", contracts, sources)
