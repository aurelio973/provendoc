# -*- coding: utf-8 -*-
"""include graph + multi-file merge semantic layer for the

cnf family.

Positioning: a multi-file channel layered on top of the
CnfParser single-file channel (syntax + tool-profile
semantics). Entry point = ``CnfParser.parse_many_merged``
(paths) (thin wrapper, following the conf/plist/xml
precedent): each file goes through the existing single-file
contract channel — disk-reading responsibility belongs
entirely to the caller's paths list (restricted-IO
convention). This module builds the include graph at the
contract layer (three-tier matching + glob + optional tier),
adjudicates graph codes, preserves load order, and merges per
profile (two strategies).

Zero IO inside the module: no on-disk glob expansion, no
include-path file lookups, no stat, **no directory reads**
(includedir only does string-prefix matching); include
targets are matched purely as strings (fnmatchcase / path
prefix / path suffix / basename) against the
caller-provided file set only; targets outside the set stay
disclosed as unresolved_candidate (an include_if_exists miss
→ optional_if_exists, pure disclosure).

Iron rules:
- additive zero drift: the single-file channel is fully
  frozen — this module is only called by
  ``parse_many_merged`` and never touches
  ``CnfParser.parse`` / ``cnf_tool_profile``; input contract
  dicts are read-only (outputs are shallow copies; no new
  keys land on input entries/containers);
- honest boundary: the merged view is a static candidate
  (``merge_semantics`` stays validated=False /
  context_dependent=True; effective_value is always
  "unknown" — there is no complete set of startup parameters
  or platform context); basename_only / directory_prefix /
  glob_matched / optional_if_exists / effective_override /
  multi-value appends are all candidate wording and never
  claim a "runtime effective configuration";
- the four graph codes (INCLUDE_CYCLE / DUPLICATE_INCLUDE /
  INCLUDE_DEPTH_LIMIT / GLOB_OVERLAP) only land in the merged
  contract's loss.reasons + include_graph.codes attribution
  (no new vocabulary); single-file semantics are unchanged
  (the single-file INCLUDE_CYCLE self-reference /
  within-file DUPLICATE_INCLUDE stay in the cnf_tool_profile
  loss_codes channel);
- every ordering is occurrence order (entry DFS visit order
  + in-file include-statement order); alphabetical sorting
  is forbidden (MySQL "preserve load order").
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.cnf_parser import (
    _GLOB_CHARS,
)

# Include-graph depth threshold — a code is recorded, no
# truncation (the constant is disclosed in
# include_graph.depth_limit)
_INCLUDE_DEPTH_LIMIT = 64

# Merge-policy tiers (single source of truth = the cnf
# profile-gate criteria)
_POLICY_LAST_WINS = "later_include_last_wins"        # mysql/mariadb
_POLICY_INLINE = "include_point_inline_expansion"    # openssl

# mysql_cnf / mariadb_cnf are treated as one family (the same
# last-wins policy, dialect differences disclosed in sources)
_MYSQL_FAMILY = frozenset(("mysql_cnf", "mariadb_cnf"))

# Degradation tier when no file has an identified profile —
# flat candidate + order disclosure, the merged result is
# still produced
_GENERIC_POLICY = "generic_uncertain_flat_candidate"

# Indexed-key shape (OpenSSL 0.subject= style; cross-file
# append candidate, not shadowing)
_INDEXED_KEY_RE = re.compile(r"^\d+\.")


# ============================================
# Pure string helpers (zero IO — path matching is string
# arithmetic only)
# ============================================


def _norm_target(raw: str) -> str:
    """Normalize an include target: unquote + strip whitespace."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def _norm_path(p: str) -> str:
    """Normalize a path string (pure string ops): unify
    separators / drop ./ prefix / drop trailing slash."""
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
    path (the factual tier of the two-tier matching).

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


def _dir_prefix_evidence(target: str, fpath: str) -> bool:
    """Directory-prefix match between an includedir target and a
    file path (third tier).

    Hit when fpath == target or fpath starts with target +
    "/". Pure string arithmetic — disk directories are never
    enumerated.
    """
    if not target or not fpath:
        return False
    return fpath == target or fpath.startswith(target + "/")


# ============================================
# Policy adjudication (mixed profiles are rejected —
# following the SDK mixed-family rejection precedent)
# ============================================


def _resolve_policy(
    detections: List[Optional[Dict[str, Any]]],
) -> Tuple[Optional[str], str]:
    """Profile consistency gate → (tool_family, policy).

    Identified profiles (resolution_status == "resolved" and
    ∈ mysql_cnf/mariadb_cnf/openssl_cnf) must belong to one
    family — mysql_cnf + mariadb_cnf pass as one family;
    mixing the mysql family with openssl → **ValueError**
    (no market-semantic precedent, so no guessing). No
    identified profile at all → the generic degradation
    tier. A single identified family + unidentified files:
    merge with that family's policy (unidentified files are
    the norm for include fragments; disclosed per file via
    merge_semantics.unresolved_profile_files + sources —
    candidate wording).
    """
    identified: List[str] = []
    for det in detections:
        if isinstance(det, dict) and det.get("resolution_status") == "resolved":
            p = det.get("profile")
            if p in _MYSQL_FAMILY or p == "openssl_cnf":
                identified.append(p)
    fams = set()
    for p in identified:
        fams.add("openssl_cnf" if p == "openssl_cnf" else "mysql")
    if len(fams) > 1:
        raise ValueError(
            "mixed cnf tool profiles cannot be merged: "
            + " vs ".join(sorted(set(identified)))
        )
    if "openssl_cnf" in fams:
        return "openssl_cnf", _POLICY_INLINE
    if "mysql" in fams:
        # dialect differences are disclosed in sources — the
        # family name takes the first dialect seen
        return "mysql_family", _POLICY_LAST_WINS
    return None, _GENERIC_POLICY


# ============================================
# include-graph build (in-set matching, zero IO)
# ============================================


def _include_records(
    contract: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Per-file include-statement records (read directly from
    the contract registration).

    The include semantics enrich each contract["includes"]
    record directly (include_order / normalized_target /
    duplicate_of_order / self_reference) — unlike conf's
    profile.includes face, this reads the contract
    registration itself.
    """
    recs: List[Dict[str, Any]] = []
    for r in (contract.get("includes") or []):
        recs.append(
            {
                "order": r.get("include_order", len(recs)),
                "raw_target": r.get("raw_target") or "",
                "normalized": r.get("normalized_target")
                or _norm_target(r.get("raw_target") or ""),
                "target_type": r.get("include_type") or "file",
                "optional": r.get("include_type") == "file_if_exists",
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
    """In-set matching per include statement → (edges,
    unresolved).

    cnf specifics:
    - url / variable targets: always unresolved (zero IO, no
      expansion);
    - glob targets: fnmatchcase pure-string matching against
      the in-set file basenames (no on-disk expansion) → hit
      edges + glob_matched disclosure;
    - directory targets (!includedir): the directory-prefix
      tier — the target (trailing / stripped) is a prefix of
      an in-set file path → edge + match_kind=
      directory_prefix (in-directory files follow the
      caller's order, no disk enumeration); with no prefix
      hit, fall back to the two literal tiers;
    - file / file_if_exists targets: two tiers — exact_path
      (full_path / path_suffix evidence) as the factual tier;
      otherwise a unique basename hit → basename_only
      candidate tier; multiple candidates → ambiguous
      unresolved (no guessing);
    - include_if_exists miss: unresolved + optional_if_exists
      pure disclosure (the directive's semantics allow
      absence, so no code is recorded).
    """
    edges: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    def _literal_matches(mtarget: str) -> List[Tuple[int, str]]:
        exact = [
            (j, ev)
            for j in range(n)
            for ev in (_path_match_evidence(mtarget, norm_paths[j]),)
            if ev is not None
        ]
        if exact:
            return exact
        tbase = mtarget.rstrip("/").rsplit("/", 1)[-1]
        return [
            (j, "basename") for j in range(n) if basenames[j] == tbase
        ]

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
            if ttype == "directory":
                # includedir directory-prefix tier (pure string,
                # no directory reads)
                pref = [
                    j for j in range(n)
                    if _dir_prefix_evidence(mtarget, norm_paths[j])
                ]
                if pref:
                    for j in pref:
                        edges.append(
                            dict(
                                base,
                                to=j,
                                match_kind="directory_prefix",
                                match_evidence="directory_prefix",
                            ),
                        )
                    continue
            # fall back to the two literal tiers (honest handling
            # when the target happens to be a single file)
            lit = _literal_matches(mtarget)
            if len(lit) == 1:
                j, ev = lit[0]
                edges.append(
                    dict(
                        base,
                        to=j,
                        match_kind=(
                            "exact_path"
                            if ev in ("full_path", "path_suffix")
                            else "basename_only"
                        ),
                        match_evidence=ev,
                    ),
                )
            elif lit:
                unresolved.append(
                    dict(
                        base,
                        reason="ambiguous_match",
                        candidates=[j for j, _ in lit],
                    ),
                )
            elif rec.get("optional"):
                # include_if_exists miss — optional absence,
                # pure disclosure
                unresolved.append(
                    dict(
                        base,
                        reason="no_match_in_provided_set",
                        optional_if_exists=True,
                    ),
                )
            else:
                unresolved.append(
                    dict(base, reason="no_match_in_provided_set"),
                )
    return edges, unresolved


# ============================================
# Graph codes (full-graph DFS detection + in-degree tally)
# ============================================


def _graph_codes(
    n: int,
    edges_by_src: Dict[int, List[Dict[str, Any]]],
    edges: List[Dict[str, Any]],
    file_names: List[str],
) -> Tuple[List[List[str]], int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Graph-level adjudication → (cycles, max_depth,
    dup_nodes, overlap).

    - INCLUDE_CYCLE: DFS gray-edge hit → cycle member order
      disclosed (filename order);
    - INCLUDE_DEPTH_LIMIT: DFS depth over the threshold — a
      code is recorded, no truncation;
    - DUPLICATE_INCLUDE: in-degree ≥2 (several include edges
      point at the same file; load relations are fully kept,
      never silently deduplicated);
    - GLOB_OVERLAP: a glob edge and a literal/directory-prefix
      edge (or several glob patterns) point at the same file
      → overlap candidate.
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
        other_edges = [e for e in ins if not e.get("glob_matched")]
        if glob_edges and other_edges:
            overlap.append(
                {
                    "file": file_names[j],
                    "glob_patterns": sorted(
                        {e["normalized_target"] for e in glob_edges},
                    ),
                    "literal_targets": sorted(
                        {e["normalized_target"] for e in other_edges},
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
# Load order (entry DFS visit order + orphan append —
# alphabetical sorting forbidden)
# ============================================


def _resolved_file_order(
    n: int,
    edges_by_src: Dict[int, List[Dict[str, Any]]],
) -> Tuple[List[int], List[int]]:
    """DFS visit order from the entry (paths[0]); unresolved
    edges take no slot.

    Isolated files (unreachable from the entry) are appended
    after the order in caller order, disclosed separately as
    orphan_files.
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
# Occurrence-chain expansion (following the shared-layer
# occurrence channel)
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
            "value": entry.get("raw_value"),
            "line": entry.get("line_start"),
        },
    ]


# ============================================
# later_include_last_wins / generic degradation tier
# (global path keying)
# ============================================


def _merge_last_wins(
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    resolved_order: List[int],
    policy: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """mysql/mariadb later_include_last_wins + the generic
    degradation tier.

    MySQL option-file market semantics: for the same section
    and key across files, the **later one wins** in load
    order + the override_chain discloses every occurrence +
    the winner carries effective_override=
    "last_wins_candidate" (candidate wording — the true
    effective_value stays unknown); **multi-value key
    exception** (value_semantics == multi_value_candidate):
    all occurrences across files are appended +
    merged_occurrences candidate.

    Cascade order = load order (resolved_file_order); key =
    (doc_index, path_str) (shared-layer convention);
    same-name section containers merge across files — winning
    container + occurrences juxtaposed across files +
    merged_from_files disclosure (child_count is recounted in
    the main entry).
    """
    entry_slots: Dict[tuple, Dict[str, Any]] = {}
    entry_order: List[tuple] = []
    for fi in resolved_order:
        fname = sources[fi].get("file_name")
        for e in (contracts[fi].get("entries") or []):
            key = (e.get("doc_index"), e["path_str"])
            slot = entry_slots.get(key)
            if slot is None:
                slot = {"occs": [], "alls": [], "multi": False}
                entry_slots[key] = slot
                entry_order.append(key)
            if e.get("value_semantics") == "multi_value_candidate":
                slot["multi"] = True
            slot["occs"].extend(_entry_occurrences(e, fi, fname))
            slot["alls"].append((e, fi, fname))

    merged_entries: List[Dict[str, Any]] = []
    for key in entry_order:
        slot = entry_slots[key]
        occs = slot["occs"]
        if slot["multi"]:
            # multi-value keys: all occurrences across files
            # appended as candidates (no shadowing)
            for e, fi, fname in slot["alls"]:
                out = dict(e)
                out["source_file"] = fname
                out["source_file_index"] = fi
                out["merged_occurrences"] = [dict(o) for o in occs]
                merged_entries.append(out)
            continue
        e, fi, fname = slot["alls"][-1]
        out = dict(e)
        out["source_file"] = fname
        out["source_file_index"] = fi
        if len(occs) > 1:
            out["duplicate"] = True
            out["override_chain"] = [dict(o) for o in occs]
            # winner disclosed as a candidate (not a
            # runtime effective-value determination)
            out["effective_override"] = "last_wins_candidate"
        merged_entries.append(out)

    # containers: same-name sections merge across files
    # (winning container + juxtaposed disclosure)
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
        cont, fi, fname = slot["alls"][-1]
        out = dict(cont)
        out["source_file"] = fname
        out["source_file_index"] = fi
        if len(slot["alls"]) > 1:
            out["duplicate"] = True
            # occurrences juxtaposed across files
            # (occurrence order)
            occs = []
            for c, _, _ in slot["alls"]:
                occs.extend(
                    [dict(o) for o in (c.get("occurrences") or [])],
                )
            out["occurrences"] = occs
            out["merged_from_files"] = [fn for _, _, fn in slot["alls"]]
        merged_containers.append(out)
    return merged_entries, merged_containers


# ============================================
# include-point inline expansion (OpenSSL .include
# textual-inline semantics)
# ============================================


def _include_scope(
    contract: Dict[str, Any],
    line_start: Optional[int],
) -> Optional[str]:
    """The section an include statement sits in (positional
    method, pure contract-data arithmetic).

    INI semantics: the **nearest** section header before the
    include line is its section (a container's line_end
    covers only the header line, so interval containment
    does not apply); before the first header → None (top
    level). OpenSSL ``.include`` often appears inside
    sections — this is the scope-rewrite baseline for inline
    semantics.
    """
    if line_start is None:
        return None
    best_line: Optional[int] = None
    best_name: Optional[str] = None
    for cont in (contract.get("containers") or []):
        for o in (cont.get("occurrences") or [{}]):
            ls = o.get("line_start", cont.get("line_start"))
            if ls is None:
                continue
            if line_start > ls and (best_line is None or ls > best_line):
                best_line = ls
                best_name = (cont.get("path") or [None])[0]
    return best_name


def _is_append_candidate(e: Dict[str, Any]) -> bool:
    """Indexed keys (OpenSSL ``0.subject=`` style) or
    multi-value candidates → the append tier."""
    if e.get("value_semantics") == "multi_value_candidate":
        return True
    path = e.get("path") or []
    return bool(path) and bool(_INDEXED_KEY_RE.match(str(path[-1])))


def _merge_inline(
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    orphans: List[int],
    file_names: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]],
           List[Dict[str, Any]]]:
    """include-point inline expansion (core of the OpenSSL
    ``.include`` semantics).

    Top-level entries (path == [key]) of an include target
    file have their path rewritten to "the include
    statement's section + key" (textual inline); entries and
    containers already inside sections in the fragment keep
    their original path (same-name sections merge key-wise
    with the later file winning — naturally covered by path
    keying); indexed keys / multi-value keys are **append
    candidates** across files, not shadowed. Cycles: an
    expansion-stack hit → cycle_skipped disclosure
    (INCLUDE_CYCLE is already recorded in the graph codes, no
    infinite recursion). Orphan files are appended at root
    scope (orphan_expansion disclosure).
    """
    edges_by_file: Dict[int, List[Dict[str, Any]]] = {}
    for e in edges:
        edges_by_file.setdefault(e["from"], []).append(e)

    emitted_entries: List[Dict[str, Any]] = []
    emitted_containers: List[Dict[str, Any]] = []
    expansions: List[Dict[str, Any]] = []
    stack: List[int] = []
    skipped_cycles: List[Dict[str, Any]] = []

    def expand(fi: int, prefix: Optional[str], orphan: bool) -> None:
        stack.append(fi)
        fname = sources[fi].get("file_name")
        contract = contracts[fi]
        for e in (contract.get("entries") or []):
            out = dict(e)
            path = list(e.get("path") or [])
            if prefix is not None and len(path) == 1:
                # top-level key inline rewrite: [key] →
                # [include-point section, key]
                path = [prefix] + path
                out["path"] = path
                out["path_str"] = cc.path_str(path)
                out["include_expansion_scope"] = prefix
            out["source_file"] = fname
            out["source_file_index"] = fi
            if orphan:
                out["orphan_expansion"] = True
            emitted_entries.append(out)
        for cont in (contract.get("containers") or []):
            out = dict(cont)
            out["source_file"] = fname
            out["source_file_index"] = fi
            if orphan:
                out["orphan_expansion"] = True
            emitted_containers.append(out)
        for edge in edges_by_file.get(fi, []):
            tgt = edge["to"]
            if tgt in stack:
                skipped_cycles.append(
                    {
                        "from_file": fname,
                        "target_file": file_names[tgt],
                        "line_start": edge.get("line_start"),
                    },
                )
                continue
            scope = _include_scope(contract, edge.get("line_start"))
            expansions.append(
                {
                    "include_file": fname,
                    "target_file": file_names[tgt],
                    "include_line": edge.get("line_start"),
                    "match_kind": edge.get("match_kind"),
                    "scope": scope,
                },
            )
            expand(tgt, scope, orphan=False)
        stack.pop()

    expand(0, None, orphan=False)
    for oi in orphans:
        expand(oi, None, orphan=True)

    if skipped_cycles:
        expansions.append({"cycle_skipped": skipped_cycles})

    # keyed win: indexed/multi-value keys append as
    # candidates, others last-wins + override_chain
    entry_slots: Dict[str, Dict[str, Any]] = {}
    entry_order: List[str] = []
    for e in emitted_entries:
        key = e["path_str"]
        slot = entry_slots.get(key)
        if slot is None:
            slot = {"occs": [], "alls": [], "multi": False}
            entry_slots[key] = slot
            entry_order.append(key)
        if _is_append_candidate(e):
            slot["multi"] = True
        slot["occs"].extend(
            _entry_occurrences(e, e.get("source_file_index", 0),
                               e.get("source_file")),
        )
        slot["alls"].append(e)

    merged_entries: List[Dict[str, Any]] = []
    for key in entry_order:
        slot = entry_slots[key]
        occs = slot["occs"]
        if slot["multi"]:
            for e in slot["alls"]:
                e["merged_occurrences"] = [dict(o) for o in occs]
                merged_entries.append(e)
            continue
        e = slot["alls"][-1]
        if len(occs) > 1:
            e["duplicate"] = True
            e["override_chain"] = [dict(o) for o in occs]
        merged_entries.append(e)

    cont_slots: Dict[str, Dict[str, Any]] = {}
    cont_order: List[str] = []
    for cont in emitted_containers:
        key = cont["path_str"]
        slot = cont_slots.get(key)
        if slot is None:
            slot = {"alls": []}
            cont_slots[key] = slot
            cont_order.append(key)
        slot["alls"].append(cont)

    merged_containers: List[Dict[str, Any]] = []
    for key in cont_order:
        slot = cont_slots[key]
        out = slot["alls"][-1]
        if len(slot["alls"]) > 1:
            out["duplicate"] = True
            occs = []
            for c in slot["alls"]:
                occs.extend([dict(o) for o in (c.get("occurrences") or [])])
            out["occurrences"] = occs
            out["merged_from_files"] = [
                c.get("source_file") for c in slot["alls"]
            ]
        merged_containers.append(out)
    return merged_entries, merged_containers, expansions


# ============================================
# include_source_order view (per-include statement order
# pass-through)
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
                if us[0].get("optional_if_exists"):
                    item["optional_if_exists"] = True
                if us[0].get("candidates") is not None:
                    item["ambiguous_candidates"] = [
                        file_names[j] for j in us[0]["candidates"]
                    ]
            out.append(item)
    return out


# ============================================
# Main entry (graph + codes + orders + per-profile merge +
# contract header)
# ============================================


def merge_cnf_contracts(
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    detections: List[Optional[Dict[str, Any]]],
    tool_profiles: List[Optional[Dict[str, Any]]],
    file_paths: List[str],
) -> Dict[str, Any]:
    """cnf-family multi-file merge main entry (pure function,
    zero IO inside the module).

    All inputs are prepared by the caller
    (``CnfParser.parse_many_merged``): per-file contracts +
    sources identity + cnf_profile detections + tool profiles
    + actual file paths (the comparison baseline for in-set
    include matching). Returns the merged contract dict (not
    a Document — across files there is no single original
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

    tool_family, policy = _resolve_policy(detections)

    records = [_include_records(contracts[i]) for i in range(n)]
    edges, unresolved = _build_edges(n, records, norm_paths, basenames)

    edges_by_src: Dict[int, List[Dict[str, Any]]] = {}
    for e in edges:
        edges_by_src.setdefault(e["from"], []).append(e)
    # records iterate per file by order → edges_by_src
    # naturally follows include-statement order (every order
    # is occurrence order)

    cycles, max_depth, dup_nodes, overlap = _graph_codes(
        n, edges_by_src, edges, file_names,
    )
    resolved_order, orphans = _resolved_file_order(n, edges_by_src)

    if policy == _POLICY_INLINE:
        merged_entries, merged_containers, expansions = _merge_inline(
            contracts, sources, edges, orphans, file_names,
        )
    else:
        merged_entries, merged_containers = _merge_last_wins(
            contracts, sources, resolved_order, policy,
        )
        expansions = []

    # directive_order / scope_order: stable occurrence-order
    # indices
    for idx, e in enumerate(merged_entries):
        e["directive_order"] = idx
    for idx, cont in enumerate(merged_containers):
        cont["scope_order"] = idx

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
        det_raw = detections[i]
        det = det_raw if isinstance(det_raw, dict) else {}
        tp_raw = tool_profiles[i]
        tp = tp_raw if isinstance(tp_raw, dict) else {}
        fr = tp.get("file_role") or {}
        enriched_sources.append(
            {
                "index": sources[i].get("index", i),
                "file_name": file_names[i],
                "sha256_prefix": sources[i].get("sha256_prefix"),
                "profile": det.get("profile"),
                "resolution_status": det.get("resolution_status"),
                "file_role": fr.get("file_role"),
            },
        )

    unresolved_profile_files = [
        file_names[i]
        for i in range(n)
        if (detections[i] or {}).get("resolution_status") != "resolved"
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
        "format": "cnf",
        "merged": True,
        "duplicate_key_semantics": policy,
        "merge_semantics": {
            "tool_family": tool_family,
            "policy": policy,
            "validated": False,
            "context_dependent": True,
            "include_point_inline_expansion": policy == _POLICY_INLINE,
            # without complete startup parameters and platform
            # context → effective_value: unknown (full
            # adjudication is out of scope)
            "effective_value": "unknown",
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

    # Authoritative rescan of the sensitive-count reason:
    # strip the stale per-file pass-through counts and
    # recompute one authoritative value (count strings differ,
    # so a union would not dedupe)
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
