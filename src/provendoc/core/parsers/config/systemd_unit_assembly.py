# -*- coding: utf-8 -*-
"""systemd unit multi-file assembly (``assemble_units``, pure function).

Assembles drop-in/base unit files over an **explicitly provided set
of documents**. Non-negotiables: no execution, no IO — system
directories are never scanned, symlinks are never followed,
EnvironmentFile contents are never read. Assembly is a pure function
over the provided documents; multi-document ingestion wiring lives
in the pipeline layer.

Graph and verification semantics:

- Dependency graph: each group's winning base + drop-ins have their
  unit relations aggregated and deduplicated; resolution is scoped
  to the provided set (a target counts as resolved when its group
  exists). Strong-edge misses (requires/requisite/binds_to) are
  recorded as INVALID_DEPENDENCY.
- Cycle detection: DFS over positive dependency edges
  (requires/wants/requisite/binds_to/part_of, self-loops included);
  ordering/negative/failure/timer edges do not participate
  → DEPENDENCY_CYCLE.
- timer next_trigger is a zero-clock disclosure (computable stays
  False); enablement/systemd_version/verify/security_findings are
  zero-IO disclosure keys (disclosure is not loss, no new codes).

Key semantics:

- Grouping key = canonical unit name (instance names normalized via
  escaped_instance_id; an escaped/plain form conflict is
  TEMPLATE_INSTANCE_AMBIGUOUS).
- Bases never merge across directories: the highest tier_priority
  wins, the rest are disclosed as shadowed (systemd's real
  semantics); two bases in the same tier → DROP_IN_CONFLICT.
- Drop-ins from all tiers participate (tier_priority ascending, then
  filename lexicographic), merged per directive by cardinality:
  single_value cross-source last-wins is legal (merge_actions keep
  a trace); list appends; an empty reset clears; ExecStart with >1
  effective cross-source values → DROP_IN_CONFLICT.
- effective_config_status is always "context_dependent" — a provided
  file set never claims a valid configuration.
- EnvironmentFile references resolve by exact path match against
  the evidence set (separator-normalized); a non-optional reference
  not in the set → MISSING_ENVIRONMENT_FILE (scoped disclosure:
  not provided in the analysis set, never a claim of system-wide
  absence).
"""

from typing import Any

# Static tier table (single source of truth; order = matching
# priority — /usr/local/lib/systemd before /usr/lib/systemd so the
# shorter prefix cannot shadow it)
_TIER_RULES: tuple[tuple[str, str, int], ...] = (
    ("/usr/local/lib/systemd", "local", 2),
    ("/usr/lib/systemd", "vendor", 1),
    ("/run/systemd", "runtime", 3),
    ("/etc/systemd", "admin", 4),
)

# Strong dependency edges (target not in the evidence set → scoped
# INVALID_DEPENDENCY); weak edges (wants/part_of) are legally missing
# at runtime — only the resolution is recorded, no code.
_STRONG_DEPENDENCY_EDGES = frozenset(("requires", "requisite", "binds_to"))

# Edge types participating in cycle detection (positive dependency
# family, self-loops included). Ordering edges, negative edges and
# failure-propagation edges do not participate (After is not a
# dependency).
_CYCLE_EDGE_TYPES = frozenset(
    ("requires", "wants", "requisite", "binds_to", "part_of")
)

# timer schedule field family (feeds the next_trigger disclosure's
# schedules)
_TIMER_SCHEDULE_KEYS = frozenset(
    (
        "OnCalendar",
        "OnActiveSec",
        "OnBootSec",
        "OnStartupSec",
        "OnUnitActiveSec",
        "OnUnitInactiveSec",
    )
)


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/")


def _tier_of(path: str) -> tuple[str, int | None]:
    norm = _norm_path(path)
    for marker, tier, priority in _TIER_RULES:
        if marker in norm:
            return tier, priority
    return "unknown", None


def _canonical_unit_name(profile: dict) -> str:
    """profile → canonical unit name (instances normalized).

    plain/template take unit_name directly; an instance rebuilds the
    canonical name from escaped_instance_id (or instance_id) so that
    ``demo@b-c.service`` and the escaped form
    ``demo@b\\x2dc.service`` land on the same key (a conflict is
    the ambiguity code).
    """
    name = profile.get("unit_name") or ""
    ti = profile.get("template_instance") or {}
    if ti.get("kind") != "instance":
        return name
    inst = ti.get("escaped_instance_id")
    if inst is None:
        inst = ti.get("instance_id") or ""
    tmpl = ti.get("template_name") or ""  # e.g. demo@.service
    stem, dot, ext = tmpl.rpartition(".")
    base = stem[:-1] if stem.endswith("@") else stem
    return f"{base}@{inst}.{ext}" if dot else f"{base}@{inst}"


def _source_sort_key(rec: dict):
    """Sort key for base resolution: tier_priority descending, unknown
    tier last (stable sort keeps provided order).

    Drop-in merge order does not use this key (see
    ``_dropin_sort_key``) — under last-wins semantics drop-ins must
    be processed in ascending order (higher tiers written later win),
    the opposite direction from the base's "highest tier wins".
    """
    priority = (rec["unit_source"] or {}).get("tier_priority")
    return (priority is None, -(priority if priority is not None else 0))


def _dropin_sort_key(rec: dict):
    """Drop-in merge order: tier_priority ascending, then filename
    lexicographic within a tier.

    The parser layer always fills tier unknown/None for drop-ins
    (single-file parsing has no path evidence), so assembly
    re-derives the tier here from the file's own path via
    ``_tier_of`` — honoring the parser contract that merge priority
    is re-judged by assembly from the file's own path. systemd
    semantics: lower tiers load first, higher tiers load later and
    override (ascending order under last-wins = higher tier wins);
    within a tier, drop-in filename lexicographic order (00-a.conf
    before 20-b.conf). Unrankable files (None) sort first = lowest
    override power (no tier evidence means overriding nobody).
    """
    priority = (rec["unit_source"] or {}).get("tier_priority")
    if priority is None:
        _, priority = _tier_of(rec.get("file") or "")
    name = (rec["unit_source"] or {}).get("drop_in_file") or (
        (rec.get("file") or "").rsplit("/", 1)[-1]
    )
    return (
        priority is not None,
        priority if priority is not None else 0,
        name,
    )


def _canonical_cycle(path: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize a cycle path (rotate so the lexicographically
    smallest node comes first — dedups the same cycle found from
    different start points)."""
    if len(path) > 1:
        i = path.index(min(path))
        path = path[i:] + path[:i]
    return path


def _find_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    """DFS cycle detection over positive dependency edges (self-loops
    included; deterministic sorted output).

    One path-restricted DFS per start node; an edge back to the
    start closes a cycle; the same cycle found from different starts
    is deduped via ``_canonical_cycle`` rotation. The graph is small
    (the evidence set is explicit) — naive enumeration suffices, no
    need for Johnson's algorithm.
    """
    found: set[tuple[str, ...]] = set()
    for start in sorted(adj):
        stack: list[str] = []
        on_path: set[str] = set()

        def dfs(node: str) -> None:
            stack.append(node)
            on_path.add(node)
            for nxt in sorted(adj.get(node, ())):
                if nxt == start:
                    found.add(_canonical_cycle(tuple(stack)))
                elif nxt not in on_path:
                    dfs(nxt)
            stack.pop()
            on_path.discard(node)

        dfs(start)
    return [list(c) for c in sorted(found)]


def _merge_slot(cardinality: str) -> dict:
    return {
        "cardinality": cardinality,
        "effective_values": [],
        "final_value": None,
        "merge_actions": [],
    }


def _merge_group(recs_in_order: list[dict]) -> dict[str, dict]:
    """Merge per directive by cardinality (a fold over cross-source
    occurrence streams)."""
    merged: dict[str, dict] = {}
    for rec in recs_in_order:
        contract = (rec["doc"].metadata or {}).get("config_entries") or {}
        for entry in contract.get("entries", []):
            key = entry.get("key")
            if key is None:
                continue
            section = entry.get("section") or ""
            slot = f"{section}/{key}"
            slot_data = merged.setdefault(
                slot,
                {"section": section, "key": key, **_merge_slot(
                    entry.get("cardinality") or "unknown",
                )},
            )
            cardinality = slot_data["cardinality"]
            for occ in entry.get("systemd_occurrences") or []:
                raw = occ.get("raw_value") or ""
                action = occ.get("action") or "set"
                if action == "reset":
                    slot_data["effective_values"].clear()
                    slot_data["final_value"] = None
                    slot_data["merge_actions"].append(
                        {"file": rec["file"], "role": rec["unit_source"].get("role"), "action": "reset", "value": ""}
                    )
                    continue
                if not raw:
                    continue
                if cardinality == "single_value":
                    # Cross-source last-wins is legal (drop-in overrides
                    # base — systemd semantics; duplicates within one
                    # file are already coded at the parser layer).
                    # "override" is decided by whether the slot already
                    # has a value (cross-source overrides leave a trace)
                    merge_action = (
                        "set" if slot_data["final_value"] is None else "override"
                    )
                    slot_data["final_value"] = raw
                    slot_data["effective_values"] = [raw]
                else:
                    merge_action = "append"
                    slot_data["effective_values"].append(raw)
                    slot_data["final_value"] = raw
                slot_data["merge_actions"].append(
                    {
                        "file": rec["file"],
                        "role": rec["unit_source"].get("role"),
                        "action": merge_action,
                        "value": raw,
                    }
                )
    return merged


def assemble_units(documents: list) -> dict:
    """Assembly entry point: a list of Documents → an assembly view
    grouped by canonical unit name.

    Pure function, zero IO: only reads each Document's metadata
    (systemd_profile / config_entries / file_path). Non-systemd
    documents do not join groups, but their file_path still counts
    into the evidence set (for EnvironmentFile resolution matching).
    """
    groups: dict[str, list[dict]] = {}
    evidence_paths: list[str] = []
    for doc in documents:
        md = doc.metadata or {}
        fp = _norm_path(md.get("file_path") or "")
        if fp:
            evidence_paths.append(fp)
        profile = md.get("systemd_profile")
        if not profile:
            continue
        unit_source = profile.get("unit_source") or {}
        groups.setdefault(_canonical_unit_name(profile), []).append(
            {
                "doc": doc,
                "profile": profile,
                "file": fp,
                "unit_source": unit_source,
            }
        )
    evidence = set(evidence_paths)

    units: list[dict[str, Any]] = []
    # cross-group dependency edge collection (feeds cycle detection
    # and the dependency_graph output)
    graph_edges: list[dict[str, Any]] = []
    for canonical in sorted(groups):
        recs = groups[canonical]
        bases = [r for r in recs if r["unit_source"].get("role") != "drop_in"]
        dropins = [r for r in recs if r["unit_source"].get("role") == "drop_in"]

        loss_codes: list[str] = []
        merge_conflicts: list[dict] = []
        # escaped/plain normalization conflict: same canonical key but
        # more than one distinct raw unit_name → disclosed as
        # ambiguous, not merged
        raw_names = {r["profile"].get("unit_name") for r in recs}
        ambiguous = len(raw_names) > 1
        if ambiguous:
            loss_codes.append("TEMPLATE_INSTANCE_AMBIGUOUS")
            merge_conflicts.append(
                {
                    "code": "TEMPLATE_INSTANCE_AMBIGUOUS",
                    "detail": f"distinct unit names normalize to {canonical!r}: {sorted(raw_names)}",
                }
            )

        # base resolution: highest tier wins, the rest disclosed as shadowed
        bases_sorted = sorted(bases, key=_source_sort_key)
        winning_base = bases_sorted[0] if bases_sorted else None
        same_tier_bases = len(bases) > 1 and all(
            r["unit_source"].get("tier") == bases[0]["unit_source"].get("tier")
            for r in bases
        )
        if same_tier_bases:
            loss_codes.append("DROP_IN_CONFLICT")
            merge_conflicts.append(
                {
                    "code": "DROP_IN_CONFLICT",
                    "detail": f"{len(bases)} base files for {canonical!r} share tier "
                    f"{bases[0]['unit_source'].get('tier')!r}",
                }
            )

        unit_sources: list[dict] = []
        for rec in bases_sorted:
            us = dict(rec["unit_source"])
            us["file"] = rec["file"]
            us["shadowed"] = rec is not winning_base
            unit_sources.append(us)
        sorted_dropins = sorted(dropins, key=_dropin_sort_key)
        for rec in sorted_dropins:
            us = dict(rec["unit_source"])
            us["file"] = rec["file"]
            us["shadowed"] = False
            unit_sources.append(us)

        base_present = winning_base is not None
        if dropins and not base_present:
            loss_codes.append("DROP_IN_NOT_RESOLVED")
            merge_conflicts.append(
                {
                    "code": "DROP_IN_NOT_RESOLVED",
                    "detail": f"drop-in(s) present but no base unit file for {canonical!r} in evidence set",
                }
            )

        merged_candidate = None
        if not ambiguous:
            merge_recs = ([winning_base] if winning_base else []) + sorted_dropins
            merged_candidate = _merge_group(merge_recs)
            # ExecStart cross-source invalid duplicate (assembly-level
            # extension of the single-file semantics)
            exec_slot = merged_candidate.get("Service/ExecStart")
            if exec_slot and len(exec_slot["effective_values"]) > 1:
                loss_codes.append("DROP_IN_CONFLICT")
                merge_conflicts.append(
                    {
                        "code": "DROP_IN_CONFLICT",
                        "detail": "ExecStart has >1 effective values after merge: "
                        f"{exec_slot['effective_values']}",
                    }
                )

        # EnvironmentFile evidence-set resolution (exact path match,
        # separator-normalized)
        environment_files: list[dict] = []
        seen_env: set[tuple[str, bool]] = set()
        env_recs = ([winning_base] if winning_base else []) + sorted_dropins
        for rec in env_recs:
            for node in rec["profile"].get("environment_files") or []:
                path = node.get("path") or ""
                optional = bool(node.get("optional"))
                if (path, optional) in seen_env:
                    continue
                seen_env.add((path, optional))
                resolved_path = _norm_path(path)
                hit = resolved_path in evidence
                environment_files.append(
                    {
                        "path": path,
                        "optional": optional,
                        "resolved": hit,
                        "evidence_file": resolved_path if hit else None,
                    }
                )
                if not hit and not optional:
                    loss_codes.append("MISSING_ENVIRONMENT_FILE")

        # dependency edge aggregation (winning base + drop-ins — a
        # shadowed base's edges are not part of the effective
        # configuration) + scoped resolution (a target group existing
        # means resolved; strong/weak edge coding rules as documented
        # above)
        seen_edges: set[tuple[str, str]] = set()
        strong_missing: list[dict[str, str]] = []
        for rec in env_recs:
            for rel in rec["profile"].get("unit_relations") or []:
                edge_type = rel.get("edge_type") or ""
                target = rel.get("target") or ""
                if not edge_type or not target or (edge_type, target) in seen_edges:
                    continue
                seen_edges.add((edge_type, target))
                resolved = target in groups
                graph_edges.append(
                    {
                        "source": canonical,
                        "target": target,
                        "edge_type": edge_type,
                        "resolution_status": "resolved" if resolved else "target_missing",
                    }
                )
                if not resolved and edge_type in _STRONG_DEPENDENCY_EDGES:
                    strong_missing.append(
                        {"edge_type": edge_type, "target": target}
                    )
        for miss in strong_missing:
            loss_codes.append("INVALID_DEPENDENCY")
            merge_conflicts.append(
                {
                    "code": "INVALID_DEPENDENCY",
                    "detail": (
                        "strong dependency target "
                        f"{miss['target']!r} ({miss['edge_type']}) "
                        "not provided in evidence set (scoped)"
                    ),
                }
            )

        # zero-clock / zero-IO disclosure keys (evidence-only — never
        # judged without evidence; disclosure is not loss, no new codes)
        unit_type_now = next(
            (
                r["profile"].get("unit_type")
                for r in env_recs
                if r["profile"].get("unit_type")
            ),
            None,
        )
        schedules: list[dict[str, Any]] = []
        if unit_type_now == "timer" and merged_candidate:
            for slot_key in sorted(merged_candidate):
                if not slot_key.startswith("Timer/"):
                    continue
                slot = merged_candidate[slot_key]
                if slot.get("key") in _TIMER_SCHEDULE_KEYS and slot.get("effective_values"):
                    schedules.append(
                        {
                            "key": slot["key"],
                            "effective_values": list(slot["effective_values"]),
                        }
                    )
        enablement_candidates = sorted(
            {
                target
                for rec in env_recs
                for targets in (rec["profile"].get("install_targets") or {}).values()
                for target in targets
            }
        )
        security_findings: list[dict[str, Any]] = []
        seen_findings: set[tuple[Any, Any]] = set()
        for rec in env_recs:
            for finding in rec["profile"].get("security_directive_findings") or []:
                marker = (finding.get("code"), finding.get("key"))
                if marker in seen_findings:
                    continue
                seen_findings.add(marker)
                security_findings.append(finding)

        unit_record = {
            "unit_name": canonical,
            "base_present": base_present,
            "ambiguous": ambiguous,
            "unit_sources": unit_sources,
            "merged_candidate": merged_candidate,
            "merge_conflicts": merge_conflicts,
            "environment_files": environment_files,
            "loss_codes": sorted(set(loss_codes)),
            "effective_config_status": "context_dependent",
            # enablement (symlink status is zero-IO — enabled cannot
            # be asserted statically) / version (no version evidence)
            # / verify (external validators prohibited by default) /
            # security findings summary (static analysis never asserts
            # secure/insecure, only aggregates the parser-layer
            # finding list)
            "enablement": {
                "candidates": enablement_candidates,
                "link_status": "unknown",
                "preset": "unknown",
            },
            "systemd_version": "unknown",
            "verify": {
                "tool": "systemd-analyze verify",
                "status": "not_executed",
                "reason": "R1 zero-execution; external validator prohibited by default",
            },
            "security_findings": security_findings,
            "security_status": "review_required",
        }
        # timer units get a next_trigger candidate attached (zero-clock;
        # non-timer units get no key — the trigger surface is pinned)
        if unit_type_now == "timer":
            unit_record["next_trigger_candidate"] = {
                "computable": False,
                "status": "not_computed",
                "requires": [
                    "timezone",
                    "current_time",
                    "systemd_version",
                    "calendar_event_semantics",
                ],
                "schedules": schedules,
            }
        units.append(unit_record)

    # cycle detection over positive dependency edges (ordering /
    # negative / failure / timer edges do not participate); units in
    # a cycle get DEPENDENCY_CYCLE
    adjacency: dict[str, list[str]] = {}
    for edge in graph_edges:
        if edge["edge_type"] in _CYCLE_EDGE_TYPES:
            adjacency.setdefault(edge["source"], []).append(edge["target"])
    cycles = _find_cycles(adjacency)
    cycle_members = {node for path in cycles for node in path}
    for unit in units:
        if unit["unit_name"] in cycle_members:
            unit["loss_codes"] = sorted(set(unit["loss_codes"]) | {"DEPENDENCY_CYCLE"})
            unit["merge_conflicts"].append(
                {
                    "code": "DEPENDENCY_CYCLE",
                    "detail": (
                        "unit participates in a dependency cycle "
                        "(see dependency_graph.cycles)"
                    ),
                }
            )

    return {
        "units": units,
        "dependency_graph": {
            "nodes": sorted(groups),
            "edges": graph_edges,
            "cycles": cycles,
        },
    }
