# -*- coding: utf-8 -*-
"""Include graph + multi-file merge semantics layer for the conf family.

Architectural position: a new multi-file channel above the
ConfParser single-file channel (syntax contract + tool-profile
semantics). Entry point = ``ConfParser.parse_many_merged(paths)``
(a thin wrapper following the plist/xml precedent): each file
goes through the existing single-file contract channel — disk
reading is entirely the caller's responsibility via the paths
list; this module builds the include graph, judges the graph
codes, preserves load order, and merges per policy — all at
the contract layer.

Zero IO inside this module: no glob expansion of on-disk
files, no include-target lookup on disk, no stat — include
targets are matched purely as strings (fnmatchcase / path
suffix / basename) against the caller-provided file set;
anything outside the set stays disclosed as
unresolved_candidate.

Hard rules:
- additive zero drift: the single-file channel stays fully
  frozen — this module is only called by
  ``parse_many_merged`` and never touches
  ``ConfParser.parse`` / ``conf_tool_profile``; input
  contracts are treated read-only (outputs are always shallow
  copies; no new keys land on input entries/containers);
- honest boundary: the merged view is a static candidate
  (``merge_semantics`` keeps validated=False /
  context_dependent=True); basename_only matches, glob
  hits, and scope_match are all candidate-level disclosures;
  the merged view never claims any "runtime effective
  configuration" determination;
- the four graph codes (INCLUDE_CYCLE / DUPLICATE_INCLUDE /
  INCLUDE_DEPTH_LIMIT / GLOB_OVERLAP) land only in the merged
  contract's loss.reasons + include_graph.codes attribution
  — single-file channel semantics unchanged (self-referential
  INCLUDE_CYCLE / in-file DUPLICATE_INCLUDE from the tool
  profile stay in the conf_tool_profile.loss_codes channel);
- every order is an occurrence order (entry DFS visit order
  + in-file include-statement order); alphabetical sorting is
  forbidden.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.conf_parser import (
    _GLOB_CHARS,
    _INCLUDE_DIRECTIVES,
)
from provendoc.core.parsers.config.conf_tool_profile import (
    _INHERITANCE_POLICY,
)

# include-graph depth threshold — code recorded only, never
# truncated (the constant is disclosed via include_graph.depth_limit)
_INCLUDE_DEPTH_LIMIT = 64

# inline-expansion policy set (single source of truth = the
# _INHERITANCE_POLICY mapping in conf_tool_profile)
_POLICY_INLINE = frozenset(
    (
        "block_inheritance",                          # nginx / bind
        "container_inheritance_sibling_later_wins",  # apache
    )
)
# fallback policy when every file is profile_uncertain — flat
# candidate + order disclosure, the merged result is still
# produced
_GENERIC_POLICY = "generic_uncertain_flat_candidate"


# ============================================
# pure string helpers (zero IO — path matching is string
# arithmetic only)
# ============================================


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def _norm_target(raw: str) -> str:
    """Normalize an include target: unquote + strip (same
    caliber as the single-file channel)."""
    return _unquote((raw or "").strip())


def _norm_path(p: str) -> str:
    """Normalize a path string (pure string work): unify
    separators / drop ./ prefixes / drop trailing slashes."""
    s = (p or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    if len(s) > 1:
        s = s.rstrip("/")
    return s


def _basename_of(p: str) -> str:
    return p.rstrip("/").rsplit("/", 1)[-1]


def _path_match_evidence(target: str, fpath: str) -> Optional[str]:
    """Match-evidence tier between a literal target and a file
    path (the fact tier of the two-tier matching).

    full_path: exact equality; path_suffix: the target is a
    suffix of the file path (or vice versa — an absolute
    target against a relative file). Neither holds → None.
    """
    if not target or not fpath:
        return None
    if target == fpath:
        return "full_path"
    if fpath.endswith("/" + target) or target.endswith("/" + fpath):
        return "path_suffix"
    return None


def _is_include_entry(e: Dict[str, Any]) -> bool:
    """Whether a contract entry is an include statement
    (recognized via the registered face)."""
    if e.get("include") is not None:
        return True
    return (e.get("key") or "").lower() in _INCLUDE_DIRECTIVES


# ============================================
# policy adjudication (mixed tools are rejected — following
# the mixed-family rejection precedent)
# ============================================


def _resolve_policy(
    tool_profiles: List[Optional[Dict[str, Any]]],
) -> Tuple[Optional[str], str]:
    """Profile consistency gate → (tool, policy).

    All files' detection profiles must name the same tool;
    two or more distinct identified tools → ValueError
    (no market-semantics precedent, no guessing). No
    identified tool at all (every file profile_uncertain /
    handed_off) → the generic fallback policy. One identified
    tool + remaining unidentified files: merge under that
    tool's policy (unidentified files lacking tool clues is
    the norm for include fragments — a mime.types-style
    fragment carries no nginx syntax clues; this does not
    count as mixed tools, and the unidentified face is
    disclosed per-file via merge_semantics.unresolved_profile_files
    + sources — candidate caliber).
    """
    tools = [
        tp.get("tool") if isinstance(tp, dict) else None
        for tp in tool_profiles
    ]
    present = sorted({t for t in tools if t})
    if len(present) > 1:
        raise ValueError(
            "mixed conf tool profiles cannot be merged: "
            + " vs ".join(repr(t) for t in present)
        )
    if present:
        tool = present[0]
        return tool, _INHERITANCE_POLICY[tool]
    return None, _GENERIC_POLICY


# ============================================
# include-graph construction (in-set matching, zero IO)
# ============================================


def _include_records(
    contract: Dict[str, Any],
    tool_profile: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Per-file include-statement records (the enhanced face
    first, falling back to the registered face)."""
    recs: List[Dict[str, Any]] = []
    if isinstance(tool_profile, dict):
        for r in (tool_profile.get("includes") or []):
            recs.append(
                {
                    "order": r.get(
                        "include_source_order", r.get("order", len(recs)),
                    ),
                    "raw_target": r.get("raw_target") or "",
                    "normalized": r.get("normalized_target")
                    or _norm_target(r.get("raw_target") or ""),
                    "target_type": r.get("target_type"),
                    "line_start": r.get("line_start"),
                }
            )
        if recs:
            return recs
    for r in (contract.get("includes") or []):
        recs.append(
            {
                "order": len(recs),
                "raw_target": r.get("raw_target") or "",
                "normalized": _norm_target(r.get("raw_target") or ""),
                "target_type": r.get("target_type"),
                "line_start": r.get("line_start"),
            }
        )
    return recs


def _build_edges(
    n: int,
    records: List[List[Dict[str, Any]]],
    norm_paths: List[str],
    basenames: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Match each include statement against the provided set →
    (edges, unresolved).

    - url / variable targets: always unresolved (zero IO,
      never expanded);
    - glob targets: fnmatchcase pure-string matching against
      the basenames of files in the set (no on-disk
      expansion) → hit edges + glob_matched disclosure;
    - literal targets: two tiers — exact_path (full_path /
      path_suffix evidence) is the fact tier; when that
      fails, a unique basename hit → basename_only
      candidate tier; multiple candidates → ambiguous
      unresolved (no guessing).
    """
    edges: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for i in range(n):
        for rec in records[i]:
            norm = rec["normalized"]
            ttype = rec["target_type"] or "file"
            mtarget = _norm_path(norm)
            base = {
                "from": i,
                "order": rec["order"],
                "line_start": rec["line_start"],
                "raw_target": rec["raw_target"],
                "normalized_target": norm,
                "target_type": ttype,
            }
            if ttype in ("url", "variable") or not mtarget:
                unresolved.append(
                    dict(base, reason="target_type_unresolvable"),
                )
                continue
            if any(c in mtarget for c in _GLOB_CHARS):
                matched = [
                    j for j in range(n)
                    if fnmatch.fnmatchcase(basenames[j], mtarget)
                ]
                if matched:
                    for j in matched:
                        edges.append(
                            dict(
                                base,
                                to=j,
                                match_kind="glob",
                                match_evidence="glob_pattern",
                                glob_matched=True,
                            ),
                        )
                else:
                    unresolved.append(
                        dict(base, reason="no_glob_match_in_set"),
                    )
                continue
            exact = [
                (j, ev)
                for j in range(n)
                for ev in (_path_match_evidence(mtarget, norm_paths[j]),)
                if ev is not None
            ]
            if exact:
                if len(exact) == 1:
                    j, ev = exact[0]
                    edges.append(
                        dict(
                            base,
                            to=j,
                            match_kind="exact_path",
                            match_evidence=ev,
                        ),
                    )
                else:
                    unresolved.append(
                        dict(
                            base,
                            reason="ambiguous_path_match",
                            candidates=[j for j, _ in exact],
                        ),
                    )
                continue
            tbase = mtarget.rstrip("/").rsplit("/", 1)[-1]
            base_matches = [
                j for j in range(n) if basenames[j] == tbase
            ]
            if len(base_matches) == 1:
                edges.append(
                    dict(
                        base,
                        to=base_matches[0],
                        match_kind="basename_only",
                        match_evidence="basename",
                    ),
                )
            elif base_matches:
                unresolved.append(
                    dict(
                        base,
                        reason="ambiguous_basename_match",
                        candidates=base_matches,
                    ),
                )
            else:
                unresolved.append(
                    dict(base, reason="no_match_in_provided_set"),
                )
    return edges, unresolved


# ============================================
# graph codes (full-graph DFS detection + in-degree stats)
# ============================================


def _graph_codes(
    n: int,
    edges_by_src: Dict[int, List[Dict[str, Any]]],
    edges: List[Dict[str, Any]],
    file_names: List[str],
) -> Tuple[List[List[str]], int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Graph-level judgments → (cycles, max_depth, dup_nodes,
    glob_overlap).

    - INCLUDE_CYCLE: a DFS gray-edge hit → cycle members
      disclosed in order (file-name order);
    - INCLUDE_DEPTH_LIMIT: DFS depth over the threshold —
      code recorded only, never truncated;
    - DUPLICATE_INCLUDE: in-degree >= 2 (multiple include
      edges point at the same file; the load relation is
      fully kept, never silently deduplicated);
    - GLOB_OVERLAP: the same file is pointed at by a literal
      include and a glob (or by several glob patterns)
      at once → overlap candidate.
    """
    color = [0] * n  # 0 white / 1 gray / 2 black
    cycles: List[List[str]] = []
    max_depth = 0

    def dfs(u: int, stack: List[int]) -> None:
        nonlocal max_depth
        color[u] = 1
        stack.append(u)
        if len(stack) > max_depth:
            max_depth = len(stack)
        for edge in edges_by_src.get(u, []):
            v = edge["to"]
            if color[v] == 1:
                idx = stack.index(v)
                cycles.append(
                    [file_names[x] for x in stack[idx:]] + [file_names[v]],
                )
            elif color[v] == 0:
                dfs(v, stack)
        stack.pop()
        color[u] = 2

    for root in range(n):
        if color[root] == 0:
            dfs(root, [])

    in_edges: Dict[int, List[Dict[str, Any]]] = {}
    for e in edges:
        in_edges.setdefault(e["to"], []).append(e)
    dup_nodes: List[Dict[str, Any]] = []
    overlap: List[Dict[str, Any]] = []
    for j in range(n):
        ins = in_edges.get(j, [])
        if len(ins) >= 2:
            dup_nodes.append(
                {
                    "file": file_names[j],
                    "edge_count": len(ins),
                    "from_files": [file_names[e["from"]] for e in ins],
                }
            )
        glob_edges = [e for e in ins if e.get("glob_matched")]
        literal_edges = [e for e in ins if not e.get("glob_matched")]
        if glob_edges and literal_edges:
            overlap.append(
                {
                    "file": file_names[j],
                    "glob_patterns": sorted(
                        {e["normalized_target"] for e in glob_edges},
                    ),
                    "literal_targets": sorted(
                        {e["normalized_target"] for e in literal_edges},
                    ),
                },
            )
        elif len({e["normalized_target"] for e in glob_edges}) >= 2:
            overlap.append(
                {
                    "file": file_names[j],
                    "glob_patterns": sorted(
                        {e["normalized_target"] for e in glob_edges},
                    ),
                },
            )
    return cycles, max_depth, dup_nodes, overlap


# ============================================
# load order (entry DFS visit order + orphan append —
# alphabetical sorting forbidden)
# ============================================


def _resolved_file_order(
    n: int,
    edges_by_src: Dict[int, List[Dict[str, Any]]],
) -> Tuple[List[int], List[int]]:
    """Entry (paths[0]) DFS visit order; unresolved edges take
    no slot.

    Isolated files (unreachable from the entry) are appended
    after the order in caller order, and orphan_files are
    disclosed separately.
    """
    visited = [False] * n
    resolved: List[int] = []

    def visit(u: int) -> None:
        visited[u] = True
        resolved.append(u)
        for edge in edges_by_src.get(u, []):
            if not visited[edge["to"]]:
                visit(edge["to"])

    visit(0)
    orphans = [i for i in range(n) if not visited[i]]
    return resolved + orphans, orphans


# ============================================
# occurrence-chain expansion (along the shared-layer
# occurrence channel caliber)
# ============================================


def _entry_occurrences(
    entry: Dict[str, Any],
    file_index: int,
    file_name: Optional[str],
) -> List[Dict[str, Any]]:
    """All occurrences of a path within one file (in-file
    override_chain expansion)."""
    chain = entry.get("override_chain")
    if chain:
        out = []
        for occ in chain:
            item = dict(occ)
            item.setdefault("file", file_name)
            item.setdefault("file_index", file_index)
            out.append(item)
        return out
    return [
        {
            "file": file_name,
            "file_index": file_index,
            "value": entry.get("value"),
            "line": entry.get("line_start"),
        },
    ]


def _emitted_occurrences(e: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Occurrence chain of an emitted entry (with source_file
    attribution)."""
    return _entry_occurrences(e, e.get("source_file_index", 0),
                              e.get("source_file"))


# ============================================
# flat / first / append policy merge (global path keying)
# ============================================


def _merge_flat(
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    resolved_order: List[int],
    policy: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """flat_override (later shadows) / first_obtained_wins
    (first occurrence wins — the opposite of the shared
    layer, a conf-specific parameterization point) /
    multi_value_append_candidate (all occurrences kept) /
    the generic fallback policy (same as flat candidate).

    Cascade order = load order (resolved_file_order); key =
    (doc_index, path_str) (along the shared-layer caliber);
    cross-file shadowing is fully disclosed in
    override_chain, and appended candidates in
    merged_occurrences.
    """
    entry_slots: Dict[tuple, Dict[str, Any]] = {}
    entry_order: List[tuple] = []
    for fi in resolved_order:
        fname = sources[fi].get("file_name")
        for e in (contracts[fi].get("entries") or []):
            key = (e.get("doc_index"), e["path_str"])
            slot = entry_slots.get(key)
            if slot is None:
                slot = {"occs": [], "alls": []}
                entry_slots[key] = slot
                entry_order.append(key)
            slot["occs"].extend(_entry_occurrences(e, fi, fname))
            slot["alls"].append((e, fi, fname))

    merged_entries: List[Dict[str, Any]] = []
    for key in entry_order:
        slot = entry_slots[key]
        occs = slot["occs"]
        if policy == "multi_value_append_candidate":
            # keep all occurrences without shadowing (the
            # resolv/openvpn append-candidate caliber)
            for e, fi, fname in slot["alls"]:
                out = dict(e)
                out["source_file"] = fname
                out["source_file_index"] = fi
                out["merged_occurrences"] = [dict(o) for o in occs]
                merged_entries.append(out)
        elif policy == "first_obtained_wins":
            e, fi, fname = slot["alls"][0]
            out = dict(e)
            out["source_file"] = fname
            out["source_file_index"] = fi
            if len(occs) > 1:
                out["duplicate"] = True
                out["override_chain"] = [dict(o) for o in occs]
            merged_entries.append(out)
        else:
            # flat_override / generic fallback: later shadows
            e, fi, fname = slot["alls"][-1]
            out = dict(e)
            out["source_file"] = fname
            out["source_file_index"] = fi
            if len(occs) > 1:
                out["duplicate"] = True
                out["override_chain"] = [dict(o) for o in occs]
            merged_entries.append(out)

    # containers follow the same caliber (the append policy
    # keeps all, the other policies pick per policy)
    cont_slots: Dict[tuple, Dict[str, Any]] = {}
    cont_order: List[tuple] = []
    for fi in resolved_order:
        fname = sources[fi].get("file_name")
        for cont in (contracts[fi].get("containers") or []):
            key = (cont.get("doc_index"), cont["path_str"])
            slot = cont_slots.get(key)
            if slot is None:
                slot = {"alls": []}
                cont_slots[key] = slot
                cont_order.append(key)
            slot["alls"].append((cont, fi, fname))

    merged_containers: List[Dict[str, Any]] = []
    for key in cont_order:
        slot = cont_slots[key]
        if policy == "multi_value_append_candidate":
            for cont, fi, fname in slot["alls"]:
                out = dict(cont)
                out["source_file"] = fname
                out["source_file_index"] = fi
                merged_containers.append(out)
        else:
            idx = 0 if policy == "first_obtained_wins" else -1
            cont, fi, fname = slot["alls"][idx]
            out = dict(cont)
            out["source_file"] = fname
            out["source_file_index"] = fi
            merged_containers.append(out)
    return merged_entries, merged_containers


# ============================================
# include-point inline expansion (nginx / bind / apache)
# ============================================


def _scope_match(
    tool_profile: Optional[Dict[str, Any]],
    include_scope: List[Any],
) -> str:
    """Compare the fragment's expected_parent_scope with the
    include-point scope → matched / mismatch_candidate /
    context_dependent (all candidate calibers).
    """
    fr = (tool_profile or {}).get("file_role") or {}
    expected = fr.get("expected_parent_scope")
    if expected is None:
        return "context_dependent"
    return "matched" if list(expected) == list(include_scope) \
        else "mismatch_candidate"


def _merge_inline(
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    tool_profiles: List[Optional[Dict[str, Any]]],
    edges: List[Dict[str, Any]],
    unresolved: List[Dict[str, Any]],
    orphans: List[int],
    file_names: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]],
           List[Dict[str, Any]]]:
    """Include-point inline expansion (the core of nginx
    semantics).

    The included file's entries/containers have their paths
    rewritten as "the include statement's scope + the
    in-fragment relative path"; the same key under different
    scopes coexists naturally (distinguished by scope_path,
    no shadowing); duplicates within one scope across files
    or expansion points → later wins + a full
    override_chain. Cycles: a hit in the expansion stack →
    cycle_skipped disclosure (INCLUDE_CYCLE is already
    recorded as a graph code; no infinite recursion).
    Orphan files expand at the root scope (orphan_expansion
    disclosure).
    """
    edges_by_line: Dict[tuple, List[Dict[str, Any]]] = {}
    for e in edges:
        edges_by_line.setdefault((e["from"], e["line_start"]), []).append(e)
    unres_by_line: Dict[tuple, List[Dict[str, Any]]] = {}
    for u in unresolved:
        unres_by_line.setdefault(
            (u["from"], u["line_start"]), [],
        ).append(u)

    emitted_entries: List[Dict[str, Any]] = []
    emitted_containers: List[Dict[str, Any]] = []
    expansions: List[Dict[str, Any]] = []
    stack: List[int] = []

    def expand(fi: int, scope_prefix: List[Any], orphan: bool) -> None:
        stack.append(fi)
        fname = sources[fi].get("file_name")
        contract = contracts[fi]
        for e in (contract.get("entries") or []):
            new_path = list(scope_prefix) + list(e.get("path") or [])
            out = dict(e)
            out["path"] = new_path
            out["path_str"] = cc.path_str(new_path)
            out["source_file"] = fname
            out["source_file_index"] = fi
            if orphan:
                out["orphan_expansion"] = True
            emitted_entries.append(out)
            if not _is_include_entry(e):
                continue
            resolutions: List[Dict[str, Any]] = []
            # the include-statement line keys = the entry's
            # first-occurrence line + each override_chain
            # occurrence's line (several include statements in
            # one scope are folded into a chain by the
            # single-file channel)
            line_keys: List[Any] = []
            for lk in [e.get("line_start")] + [
                occ.get("line") for occ in (e.get("override_chain") or [])
            ]:
                if lk is not None and lk not in line_keys:
                    line_keys.append(lk)
            for lk in line_keys:
                for edge in edges_by_line.get((fi, lk), []):
                    tj = edge["to"]
                    inc_scope = new_path[:-1]
                    if tj in stack:
                        resolutions.append(
                            {
                                "status": "cycle_skipped",
                                "target_file": file_names[tj],
                                "raw_target": edge["raw_target"],
                            },
                        )
                        continue
                    sm = _scope_match(tool_profiles[tj], inc_scope)
                    expansions.append(
                        {
                            "source_file_index": fi,
                            "source_file": fname,
                            "target_file_index": tj,
                            "target_file": file_names[tj],
                            "raw_target": edge["raw_target"],
                            "match_kind": edge["match_kind"],
                            "glob_matched": bool(edge.get("glob_matched")),
                            "include_scope": list(inc_scope),
                            "scope_match": sm,
                        },
                    )
                    resolutions.append(
                        {
                            "status": "resolved",
                            "target_file_index": tj,
                            "target_file": file_names[tj],
                            "match_kind": edge["match_kind"],
                            "scope_match": sm,
                        },
                    )
                    expand(tj, inc_scope, False)
                for u in unres_by_line.get((fi, lk), []):
                    resolutions.append(
                        {
                            "status": "unresolved_candidate",
                            "raw_target": u["raw_target"],
                            "reason": u["reason"],
                        },
                    )
            if resolutions:
                out["include_resolution"] = resolutions
        for cont in (contract.get("containers") or []):
            new_path = list(scope_prefix) + list(cont.get("path") or [])
            cout = dict(cont)
            cout["path"] = new_path
            cout["path_str"] = cc.path_str(new_path)
            cout["source_file"] = fname
            cout["source_file_index"] = fi
            if orphan:
                cout["orphan_expansion"] = True
            emitted_containers.append(cout)
        stack.pop()

    expand(0, [], False)
    for o in orphans:
        expand(o, [], True)

    # duplicates within one scope across files or expansion
    # points → later wins + full override_chain
    entry_slots: Dict[tuple, Dict[str, Any]] = {}
    entry_order_keys: List[tuple] = []
    for e in emitted_entries:
        key = (e.get("doc_index"), e["path_str"])
        slot = entry_slots.get(key)
        if slot is None:
            slot = {"occs": [], "last": e}
            entry_slots[key] = slot
            entry_order_keys.append(key)
        else:
            slot["last"] = e
        slot["occs"].extend(_emitted_occurrences(e))

    merged_entries: List[Dict[str, Any]] = []
    for key in entry_order_keys:
        slot = entry_slots[key]
        out = dict(slot["last"])
        if len(slot["occs"]) > 1:
            out["duplicate"] = True
            out["override_chain"] = [dict(o) for o in slot["occs"]]
        merged_entries.append(out)

    cont_slots: Dict[tuple, Dict[str, Any]] = {}
    cont_order_keys: List[tuple] = []
    for cont in emitted_containers:
        key = (cont.get("doc_index"), cont["path_str"])
        if key not in cont_slots:
            cont_slots[key] = cont
            cont_order_keys.append(key)
        else:
            cont_slots[key] = cont

    merged_containers: List[Dict[str, Any]] = [
        dict(cont_slots[key]) for key in cont_order_keys
    ]
    return merged_entries, merged_containers, expansions


# ============================================
# include_source_order merged view (per-include-statement
# order pass-through)
# ============================================


def _include_source_order_view(
    records: List[List[Dict[str, Any]]],
    edges: List[Dict[str, Any]],
    unresolved: List[Dict[str, Any]],
    resolved_all: List[int],
    file_names: List[str],
) -> List[Dict[str, Any]]:
    edge_by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for e in edges:
        edge_by_key.setdefault((e["from"], e["order"]), []).append(e)
    unres_by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for u in unresolved:
        unres_by_key.setdefault((u["from"], u["order"]), []).append(u)

    out: List[Dict[str, Any]] = []
    for fi in resolved_all:
        for rec in records[fi]:
            key = (fi, rec["order"])
            es = edge_by_key.get(key, [])
            us = unres_by_key.get(key, [])
            item = {
                "file_index": fi,
                "file_name": file_names[fi],
                "order": rec["order"],
                "raw_target": rec["raw_target"],
                "normalized_target": rec["normalized"],
                "target_type": rec["target_type"],
                "status": "resolved" if es else "unresolved_candidate",
            }
            if es:
                item["resolved_to"] = [file_names[e["to"]] for e in es]
                item["match_kinds"] = [e["match_kind"] for e in es]
            if us:
                item["unresolved_reason"] = us[0]["reason"]
                if us[0].get("candidates") is not None:
                    item["ambiguous_candidates"] = [
                        file_names[j] for j in us[0]["candidates"]
                    ]
            out.append(item)
    return out


# ============================================
# main entry (graph + four codes + four orders + per-policy
# merge + contract header)
# ============================================


def merge_conf_contracts(
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    tool_profiles: List[Optional[Dict[str, Any]]],
    file_paths: List[str],
) -> Dict[str, Any]:
    """Main entry for conf-family multi-file merging (a pure
    function; zero IO inside this module).

    Inputs are fully prepared by the caller
    (``ConfParser.parse_many_merged``): per-file contracts +
    source identities + conf_tool_profile + the actual file
    paths (the comparison baseline for in-set include
    matching). Returns the merged contract dict (not a
    Document — across files there is no single original
    text; line-number semantics are attributed via
    source_file).
    """
    n = len(contracts)
    file_names = [
        sources[i].get("file_name") or _basename_of(str(file_paths[i]))
        for i in range(n)
    ]
    norm_paths = [_norm_path(str(p)) for p in file_paths]
    basenames = [_basename_of(p) for p in norm_paths]

    tool, policy = _resolve_policy(tool_profiles)

    records = [
        _include_records(contracts[i], tool_profiles[i]) for i in range(n)
    ]
    edges, unresolved = _build_edges(n, records, norm_paths, basenames)

    edges_by_src: Dict[int, List[Dict[str, Any]]] = {}
    for e in edges:
        edges_by_src.setdefault(e["from"], []).append(e)
    # records iterate per file by order → edges_by_src is
    # naturally in include-statement order (every order is
    # an occurrence order)

    cycles, max_depth, dup_nodes, overlap = _graph_codes(
        n, edges_by_src, edges, file_names,
    )
    resolved_order, orphans = _resolved_file_order(n, edges_by_src)

    if policy in _POLICY_INLINE:
        merged_entries, merged_containers, expansions = _merge_inline(
            contracts, sources, tool_profiles, edges, unresolved,
            orphans, file_names,
        )
    else:
        merged_entries, merged_containers = _merge_flat(
            contracts, sources, resolved_order, policy,
        )
        expansions = []

    # directive_order / scope_order: stable occurrence-order
    # sequence numbers
    for idx, e in enumerate(merged_entries):
        e["directive_order"] = idx
    for idx, c in enumerate(merged_containers):
        c["scope_order"] = idx

    graph_codes: List[str] = []
    if cycles:
        graph_codes.append("INCLUDE_CYCLE")
    if dup_nodes:
        graph_codes.append("DUPLICATE_INCLUDE")
    if max_depth > _INCLUDE_DEPTH_LIMIT:
        graph_codes.append("INCLUDE_DEPTH_LIMIT")
    if overlap:
        graph_codes.append("GLOB_OVERLAP")

    reasons = set(graph_codes)
    for c in contracts:
        reasons.update((c.get("loss") or {}).get("reasons") or [])
    loss_reasons = sorted(reasons)

    nodes: List[Dict[str, Any]] = []
    orphan_set = set(orphans)
    for i in range(n):
        if i == 0:
            role = "entry"
        elif i in orphan_set:
            role = "orphan"
        else:
            role = "included"
        nodes.append({"index": i, "file_name": file_names[i], "role": role})

    codes: Dict[str, Any] = {}
    if cycles:
        codes["INCLUDE_CYCLE"] = cycles
    if dup_nodes:
        codes["DUPLICATE_INCLUDE"] = dup_nodes
    if max_depth > _INCLUDE_DEPTH_LIMIT:
        codes["INCLUDE_DEPTH_LIMIT"] = {
            "max_depth": max_depth,
            "limit": _INCLUDE_DEPTH_LIMIT,
        }
    if overlap:
        codes["GLOB_OVERLAP"] = overlap

    enriched_sources: List[Dict[str, Any]] = []
    for i in range(n):
        tp_raw = tool_profiles[i]
        tp = tp_raw if isinstance(tp_raw, dict) else {}
        fr = tp.get("file_role") or {}
        enriched_sources.append(
            {
                "index": sources[i].get("index", i),
                "file_name": file_names[i],
                "sha256_prefix": sources[i].get("sha256_prefix"),
                "file_role": fr.get("file_role"),
                "tool_profile": tp.get("tool"),
            },
        )

    unresolved_profile_files = [
        file_names[i]
        for i in range(n)
        if not (tool_profiles[i] or {}).get("tool")
    ]

    include_graph: Dict[str, Any] = {
        "nodes": nodes,
        "edges": edges,
        "depth_limit": _INCLUDE_DEPTH_LIMIT,
        "resolved_file_order": resolved_order,
        "orphan_files": orphans,
    }
    if codes:
        include_graph["codes"] = codes

    merged: Dict[str, Any] = {
        "contract_version": cc.CONFIG_ENTRY_CONTRACT_VERSION,
        "format": "conf",
        "merged": True,
        "duplicate_key_semantics": policy,
        "merge_semantics": {
            "tool": tool,
            "policy": policy,
            "validated": False,
            "context_dependent": True,
            "include_point_inline_expansion": policy in _POLICY_INLINE,
            "unresolved_profile_files": unresolved_profile_files,
            "disclosure": (
                "merged view is a static candidate over the provided "
                "file set with load order preserved; it is not a "
                "runtime effective-configuration determination"
            ),
        },
        "sources": enriched_sources,
        "include_graph": include_graph,
        "include_source_order": _include_source_order_view(
            records, edges, unresolved, resolved_order, file_names,
        ),
        "entries": merged_entries,
        "containers": merged_containers,
        "loss": {"reasons": loss_reasons},
    }
    if expansions:
        merged["include_expansions"] = expansions

    # authoritative sensitive-count rescan (shared-layer
    # caliber: strip the stale per-file pass-through counts,
    # recompute one authoritative count — count strings
    # differ, so the union is not deduplicated)
    loss_reasons = [
        r for r in loss_reasons
        if not r.startswith("sensitive_entry_values:")
    ]
    sensitive_count = cc.scan_sensitive_entries(merged_entries)
    if sensitive_count:
        loss_reasons.append(f"sensitive_entry_values:{sensitive_count}")
    # the rescan produces a new list — it must be reattached
    # to the contract (otherwise merged references the old
    # list and the authoritative count is lost)
    merged["loss"]["reasons"] = loss_reasons

    try:
        cc.fill_child_counts(merged_entries, merged_containers)
    except Exception as e:  # pragma: no cover - defensive guard
        loss_reasons.append(f"child_count_recount_failed:{e}")
    try:
        cc.resolve_references(merged)
    except Exception as e:  # pragma: no cover - defensive guard
        loss_reasons.append(f"resolve_references_failed:{e}")
    return merged
