# ============================================================
# config_representation.py
# Config domain — unified three-granularity semantic representation
# ============================================================
"""Unified config-domain representation shared by all formats.

Data-source priority:
    1. Document.metadata["config_entries"] (config_entry_v1
       contract) — the fact layer produced by the parser
       (line numbers / comments / duplicate keys / five-state
       values)
    2. structured_data["structure"] flat fallback — degraded
       path when the contract is missing (old parse artifacts or
       failed contract build): scalar facts only, no line numbers
       or comments

Three granularities (aligned with narrative's coarse/base/fine,
but with different semantics):
    fine   — one atomic unit per entry; empty containers and
             non-enumerated inline containers also get marker units
    base   — one unit per container (section locality): direct
             child entries rendered line by line + empty/inline
             child-container marker lines; pure intermediate
             containers (no direct children) produce no unit
    coarse — one unit per top-level container (flattened subtree)
             + a root scalar group

raw / retrieval_text / metadata separation:
    - the raw facts (raw_value/override_chain/line numbers/
      comments) live only in the contract
      (document.metadata["config_entries"]); the representation
      copies no originals
    - unit.text is the retrieval rendering: "path: value" lines;
      sensitive keys (password/token/...) have values masked to
      "****" (is_sensitive_key); originals stay only in the
      contract
    - diagnostics (duplicate/value_state/masked) enter
      unit.metadata in aggregated/masked form

Compat contract:
    - base unit metadata["semantic_paths"] (asserted by the INI
      dispatch tests)
    - RepresentationResult.metadata["chunk_count"] = len(base)
    - content is a MultiGranularityOutput (provendoc_run/
      _normalize, exporter, and universal_parser all accept this
      container)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SemanticPrimitives,
    SourceContext,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.terraform_profile import (
    render_markdown_summary,
)
from provendoc.core.representations.narrative.granularity import (
    MultiGranularityOutput,
)

# label for the root scalar group at the coarse layer (top-level scalars with no section)
_ROOT_GROUP_LABEL = "(root)"


# ============================================================
# strategy knob surface
# ============================================================
CONFIG_STRATEGY_POLICY_VERSION = "config_strategy_v1"


def _record_sort_key(record: Dict[str, Any]) -> Tuple[int, str]:
    """Stable total-order key for fine_order=path_sorted (doc index + semantic path)."""
    return (
        int(record.get("doc_index") or 0),
        cc.path_str(record.get("path") or []),
    )


@dataclass(frozen=True)
class ConfigStrategyPolicy:
    """Strategy knob surface for the config pipeline.

    Defaults preserve current behavior. Injection precedence:
    policy= explicit kwarg > strategy= slot isinstance probe >
    legacy scalar kwargs (effective only without a policy; a
    policy, when present, is authoritative).

    Knob impact: mask_sensitive / max_base_chars /
    base_grouping affect the retrieval surface; fine_order
    additionally switches the locality-preservation dimension.
    """

    mask_sensitive: bool = True
    max_base_chars: Optional[int] = 4000
    base_grouping: Literal["section", "flat"] = "section"
    fine_order: Literal["document", "path_sorted"] = "document"

    def __post_init__(self):
        if self.base_grouping not in ("section", "flat"):
            raise ValueError(
                f"unknown base_grouping: {self.base_grouping!r} (expected 'section' or 'flat')",
            )
        if self.fine_order not in ("document", "path_sorted"):
            raise ValueError(
                f"unknown fine_order: {self.fine_order!r} (expected 'document' or 'path_sorted')",
            )

    @property
    def version(self) -> str:
        """Return the config strategy version constant."""
        return CONFIG_STRATEGY_POLICY_VERSION


class ConfigRepresentation(BaseRepresentation):
    """Unified config representation: fine (entry atoms) / base (container locality) / coarse (top-level subtrees).

    Subclasses only declare ``fmt``; the constructor kwargs
    ``strategy`` / ``grouping_strategy`` / ``max_entries_per_unit``
    exist for legacy callers — the config domain does semantic
    flattening, so chunk-size-style parameters do not affect
    output. ``max_base_chars`` is an honest size guardrail: base
    units are grouped per container, and pathological containers
    (huge direct-child counts, very long values) can produce
    units far beyond the embedding window. Oversized groups split
    on entry-line budget boundaries (never inside an entry atom,
    so config_atomicity holds); split facts are recorded in unit
    metadata split_* fields and in
    RepresentationResult.metadata base_size_guardrail stats.
    """

    fmt = "config"

    def __init__(
        self,
        strategy=None,
        grouping_strategy="section",
        max_entries_per_unit=20,
        mask_sensitive=True,
        max_base_chars=4000,
        policy=None,
    ):
        """Initialize the config representation.

        Args:
            strategy: legacy-caller compat (no-op); passing a
                ConfigStrategyPolicy instance adopts the knob
                surface (universal_parser's strategy=self.policy
                channel passes through unchanged).
            grouping_strategy: legacy compat parameter, no effect
                on output.
            max_entries_per_unit: legacy compat parameter, no
                effect on output.
            mask_sensitive: render sensitive values
                (password/key-like names) as ****. Effective only
                when no policy is given.
            max_base_chars: base unit size guardrail (chars,
                default 4000, roughly 4× the real-corpus p99;
                None disables splitting). Effective only when no
                policy is given.
            policy: ConfigStrategyPolicy knob surface; when
                present, all knobs come from the policy.

        Raises:
            TypeError: policy is not a ConfigStrategyPolicy
                instance.
        """
        if policy is None and isinstance(strategy, ConfigStrategyPolicy):
            policy = strategy
        if policy is not None:
            if not isinstance(policy, ConfigStrategyPolicy):
                raise TypeError(
                    f"policy must be a ConfigStrategyPolicy instance, got {type(policy).__name__}",
                )
            self.policy = policy
            self.mask_sensitive = policy.mask_sensitive
            self.max_base_chars = policy.max_base_chars
            self.base_grouping = policy.base_grouping
            self.fine_order = policy.fine_order
        else:
            self.policy = None
            self.mask_sensitive = mask_sensitive
            self.max_base_chars = max_base_chars
            self.base_grouping = "section"
            self.fine_order = "document"
        # legacy no-op parameters stored verbatim (existing
        # assertions/callers depend on them)
        self.strategy = strategy
        self.grouping_strategy = grouping_strategy
        self.max_entries_per_unit = max_entries_per_unit

    # ========================================================
    # public
    # ========================================================

    def build(self, document) -> RepresentationResult:
        """Build the three-granularity config representation (fine/base/coarse).

        Prefers the parser's config entry contract (entries/
        containers/comments); falls back to structured_data
        flattening when missing. Comment-only documents emit
        units from the comment facts instead of an empty
        representation.

        Args:
            document: Document (metadata carries config contract
                facts).

        Returns:
            RepresentationResult: representation_type="config",
            content is a MultiGranularityOutput (fine_enabled=True).
        """
        meta = document.metadata or {}
        contract = cc.get_contract(document)

        if contract is not None:
            entries = [dict(e) for e in contract.get("entries", [])]
            containers = [dict(c) for c in contract.get("containers", [])]
            contract_source = cc.CONFIG_ENTRY_CONTRACT_VERSION
        else:
            entries, containers = self._fallback_entries(meta)
            contract_source = "structured_data_fallback"

        structure = self._structure_tree(meta)
        file_name = meta.get("file_name")
        file_type = meta.get("file_type")

        fine = self._build_fine(entries, containers, structure, file_name, file_type)
        base = self._build_base(entries, containers, structure, file_name, file_type)
        split_units = sum(1 for u in base if (u.metadata or {}).get("split_reason"))

        # comment-only documents: a valid config whose entire content is
        # comments parses successfully but yields no entries/containers.
        # The comments are the document's real content — emit them as
        # units instead of an empty representation (every unit's text
        # comes verbatim from the contract's comment facts; nothing is
        # invented).
        if not fine and not base:
            comments = (
                [dict(c) for c in contract.get("comments", [])] if contract is not None else []
            )
            fine, base = self._build_comment_only_units(
                comments,
                file_name,
                file_type,
            )

        coarse = self._build_coarse(base)

        output = MultiGranularityOutput(
            coarse=coarse,
            base=base,
            fine=fine,
            fine_enabled=True,
        )

        base_texts = [u.text for u in base]
        transforms = [
            "config_entry_contract" if contract is not None else "structured_data_flattening",
            "semantic_config_flat_grouping"
            if self.base_grouping == "flat"
            else "semantic_config_grouping",
        ]
        if self.mask_sensitive:
            transforms.append("sensitive_masking")
        if split_units:
            transforms.append("base_size_guardrail")

        # honesty: flat grouping does not own section_locality
        invariants = ["semantic_path_preserved"]
        if self.base_grouping != "flat":
            invariants.append("section_locality")
        invariants.append("config_atomicity")

        metadata: Dict[str, Any] = {
            "chunk_count": len(base),
            "unit_count": len(base),
            "avg_chunk_length": round(sum(len(t) for t in base_texts) / max(len(base), 1), 2),
            "entry_count": len(entries),
            "container_count": len(containers),
            "format": self.fmt,
            "contract_source": contract_source,
            "granularity_stats": output.stats(),
            "masked_entry_count": self._count_masked(entries),
            "base_size_guardrail": {
                "max_chars": self.max_base_chars,
                "split_unit_count": split_units,
            },
            "contains_code": False,
            "contains_inline_code": False,
            "transforms_applied": transforms,
            "representation_invariants": invariants,
        }
        # knob-surface visibility: the strategy_policy key is the
        # only output difference between the default policy and no
        # policy
        if self.policy is not None:
            metadata["strategy_policy"] = {
                "version": self.policy.version,
                **asdict(self.policy),
            }

        return RepresentationResult(
            representation_type="config",
            content=output,
            metadata=metadata,
        )

    # ========================================================
    # data sources
    # ========================================================

    @staticmethod
    def _structure_tree(meta: Dict[str, Any]) -> Any:
        """Fetch the structure tree (shared by inline-container rendering and contract-missing fallback).

        Two mount shapes:
        - INI: structured_data = {"titles", "contents", "tables", "structure"}
        - YAML/JSON/TOML: structured_data = raw parsed data (dict/list)
        """
        structured = meta.get("structured_data")
        if isinstance(structured, dict):
            # INI-style wrapper (recognized only when the keys are
            # all present, to avoid swallowing ordinary configs
            # that happen to contain a "structure" key)
            if {"structure", "contents", "tables"} <= set(structured.keys()):
                return structured.get("structure")
            return structured
        # bare list (e.g. a top-level JSON array) or None
        return structured

    def _fallback_entries(self, meta: Dict[str, Any]):
        """Synthesize entries/containers by flattening structure when the contract is missing."""
        structure = self._structure_tree(meta)
        if not isinstance(structure, dict):
            return [], []
        flat = cc.flatten_structured_data(structure)
        entries: List[Dict[str, Any]] = []
        containers: List[Dict[str, Any]] = []
        seen = set()
        # direct-child counts per container path (for value_state
        # decisions)
        child_counts: Dict[str, int] = {}
        if flat:
            containers.append(cc.make_container([], "dict", child_count=0))
            seen.add("")
            child_counts[""] = 0
        for path_str_key in flat:
            parts = path_str_key.split(".")
            for d in range(1, len(parts) + 1):
                parent_key = cc.path_str(parts[: max(d - 1, 0)])
                child_counts[parent_key] = child_counts.get(parent_key, 0) + (
                    1 if d == len(parts) else 0
                )
        for path_str_key, value in flat.items():
            path = path_str_key.split(".")
            # parent container chain
            for d in range(1, len(path)):
                parent_key = cc.path_str(path[:d])
                if parent_key not in seen:
                    seen.add(parent_key)
                    containers.append(
                        cc.make_container(
                            path[:d],
                            "dict",
                            child_count=child_counts.get(parent_key, 0),
                        )
                    )
            entries.append(
                cc.make_entry(
                    path,
                    path[-1],
                    str(value),
                    value,
                )
            )
        # fix the root container's child_count
        if containers:
            containers[0]["child_count"] = child_counts.get("", 0)
        return entries, containers

    # ========================================================
    # rendering helpers
    # ========================================================

    @staticmethod
    def _display_path(path: List[str], doc_index: int = 0) -> str:
        p = cc.path_str(path)
        return f"doc{doc_index}.{p}" if doc_index else p

    @staticmethod
    def _hierarchy(path: List[str], doc_index: int = 0) -> List[str]:
        segments = [str(s) for s in path]
        if doc_index:
            segments = [f"doc{doc_index}"] + segments
        return segments

    @staticmethod
    def _leaf_of(record: Dict[str, Any]) -> str:
        path = record.get("path") or []
        key = str(record.get("key") or "")
        if key and not (key.startswith("#") or key.isdigit()):
            return key
        # aligned with contracts.scan_sensitive_entries: skip
        # #-prefixed special segments (XML #text/#comment) and pure
        # digit index segments (list subscripts), falling back to
        # the last real path segment — otherwise sensitive keys of
        # XML element-text entries (e.g. <password>…</password>)
        # would leak unmasked on the retrieval surface.
        for seg in reversed(path):
            text = str(seg)
            if text.startswith("#") or text.isdigit():
                continue
            return text
        return key or (str(path[-1]) if path else "")

    def _is_masked(self, leaf: str) -> bool:
        return bool(self.mask_sensitive and cc.is_sensitive_key(leaf))

    def _record_is_masked(self, record: Dict[str, Any]) -> bool:
        """Entry-level two-channel masking decision: the key-name
        channel (leaf semantic-segment vocabulary hit) ∪ the
        value-pattern channel (non-empty contract
        sensitive_candidates — flagged means masked, so value types
        like jwt_token reach the retrieval surface; disclosure and
        retrieval stay consistent). Both channels are off when the
        gate is off."""
        if not self.mask_sensitive:
            return False
        if record.get("sensitive_candidates"):
            return True
        return self._is_masked(self._leaf_of(record))

    def _count_masked(self, entries: List[Dict[str, Any]]) -> int:
        return sum(1 for e in entries if self._record_is_masked(e))

    def _render_entry_value(self, entry: Dict[str, Any]) -> str:
        """Retrieval rendering of an entry's value (no masking here; the caller decides by leaf)."""
        if entry.get("value_state") == cc.VALUE_STATE_EXPLICIT_NULL:
            return "null"
        v = entry.get("value")
        if isinstance(v, str):
            return v
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)

    def _masked_chain(
        self,
        chain: Optional[List[Dict[str, Any]]],
        leaf: str,
        masked: Optional[bool] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Masked rendering of override_chain (diagnostic facts; values may contain sensitive history)."""
        if not chain:
            return None
        if masked is None:
            masked = self._is_masked(leaf)
        out = []
        for occ in chain:
            v = occ.get("value")
            text = (
                v
                if isinstance(v, str)
                else json.dumps(
                    v,
                    ensure_ascii=False,
                )
            )
            out.append(
                {
                    "value": "****" if masked else text,
                    "line": occ.get("line"),
                }
            )
        return out

    def _render_inline(
        self,
        structure: Any,
        path: List[str],
        doc_index: int,
        leaf: str,
    ) -> str:
        """Whole-container rendering for non-enumerated inline containers (values back-filled from structured_data)."""
        node = self._lookup_structure(structure, path, doc_index)
        if node is None:
            return "<inline_container_not_enumerated>"
        try:
            text = json.dumps(node, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(node)
        return "****" if self._is_masked(leaf) else text

    @staticmethod
    def _lookup_structure(structure: Any, path: List[str], doc_index: int):
        """Navigate the structure tree by path segments (list index segments cast to int)."""
        if structure is None:
            return None
        node = structure
        if doc_index and isinstance(structure, list) and len(structure) > doc_index:
            node = structure[doc_index]
        for seg in path:
            if isinstance(node, dict):
                if seg not in node:
                    return None
                node = node[seg]
            elif isinstance(node, list):
                try:
                    node = node[int(seg)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return node

    def _make_unit(
        self,
        text: str,
        granularity: Literal["coarse", "base", "fine"],
        sequence_index: int,
        path: List[str],
        doc_index: int,
        locality_scope: str,
        file_name: Optional[str],
        file_type: Optional[str],
        line_start: Optional[int],
        line_end: Optional[int],
        metadata: Dict[str, Any],
    ) -> RepresentationUnit:
        hierarchy = self._hierarchy(path, doc_index)
        return RepresentationUnit(
            text=text,
            unit_type=UnitType.CONFIG,
            granularity=granularity,
            sequence_index=sequence_index,
            heading=str(path[-1]) if path else "",
            heading_path=hierarchy,
            source_context=SourceContext(
                file_name=file_name,
                file_type=file_type,
                line_start=line_start,
                line_end=line_end,
            ),
            semantic_primitives=SemanticPrimitives(
                hierarchy_path=hierarchy,
                locality_scope=locality_scope,
                semantic_role="body",
                integrity_guarantees=[
                    "hierarchy_continuity",
                    "config_atomicity",
                ],
            ),
            metadata=metadata,
        )

    # ========================================================
    # fine — entry-atom granularity
    # ========================================================

    def _build_fine(
        self,
        entries: List[Dict[str, Any]],
        containers: List[Dict[str, Any]],
        structure: Any,
        file_name: Optional[str],
        file_type: Optional[str],
    ) -> List[RepresentationUnit]:
        units: List[RepresentationUnit] = []
        idx = 0
        # fine emission-order knob: document = contract document
        # order (current behavior); path_sorted = stable
        # (doc_index, path) total order; marker units still come
        # after entry atoms
        if self.fine_order == "path_sorted":
            entries = sorted(entries, key=_record_sort_key)
            marker_containers = sorted(containers, key=_record_sort_key)
        else:
            marker_containers = containers
        for e in entries:
            path = e.get("path") or []
            doc_index = int(e.get("doc_index") or 0)
            leaf = self._leaf_of(e)
            disp = self._display_path(path, doc_index)
            masked = self._record_is_masked(e)
            value_text = "****" if masked else self._render_entry_value(e)
            units.append(
                self._make_unit(
                    text=f"{disp}: {value_text}",
                    granularity="fine",
                    sequence_index=idx,
                    path=path,
                    doc_index=doc_index,
                    locality_scope="entry",
                    file_name=file_name,
                    file_type=file_type,
                    line_start=e.get("line_start"),
                    line_end=e.get("line_end"),
                    metadata={
                        "granularity": "fine",
                        "semantic_path": disp,
                        "value_state": e.get("value_state"),
                        "duplicate": bool(e.get("duplicate")),
                        "masked": masked,
                        "line_start": e.get("line_start"),
                        "line_end": e.get("line_end"),
                        "doc_index": doc_index,
                        "override_chain": self._masked_chain(
                            e.get("override_chain"),
                            leaf,
                            masked,
                        ),
                        "references": e.get("references") or [],
                        "description": e.get("description"),
                    },
                )
            )
            idx += 1

        # marker units for empty containers / non-enumerated
        # inline containers (keep atoms retrievable)
        for c in marker_containers:
            path = c.get("path") or []
            if not path:
                continue  # the root container has no retrieval meaning
            if c.get("child_count") is not None and c.get("child_count") != 0:
                continue  # enumerated container: children have their own fine units
            doc_index = int(c.get("doc_index") or 0)
            leaf = self._leaf_of(c)
            disp = self._display_path(path, doc_index)
            if (
                c.get("value_state") in (cc.VALUE_STATE_EMPTY_DICT, cc.VALUE_STATE_EMPTY_LIST)
                or c.get("child_count") == 0
            ):
                marker = "[]" if c.get("container_type") == "list" else "{}"
            else:
                marker = self._render_inline(
                    structure,
                    path,
                    doc_index,
                    leaf,
                )
            units.append(
                self._make_unit(
                    text=f"{disp}: {marker}",
                    granularity="fine",
                    sequence_index=idx,
                    path=path,
                    doc_index=doc_index,
                    locality_scope="entry",
                    file_name=file_name,
                    file_type=file_type,
                    line_start=c.get("line_start"),
                    line_end=c.get("line_end"),
                    metadata={
                        "granularity": "fine",
                        "semantic_path": disp,
                        "value_state": c.get("value_state"),
                        "container_type": c.get("container_type"),
                        "masked": self._is_masked(leaf),
                        "line_start": c.get("line_start"),
                        "line_end": c.get("line_end"),
                        "doc_index": doc_index,
                        "description": c.get("description"),
                    },
                )
            )
            idx += 1

        return units

    # ========================================================
    # base — container-local granularity (section grouping)
    # ========================================================

    def _build_base(
        self,
        entries: List[Dict[str, Any]],
        containers: List[Dict[str, Any]],
        structure: Any,
        file_name: Optional[str],
        file_type: Optional[str],
    ) -> List[RepresentationUnit]:
        groups: Dict[Tuple[int, str], Dict[str, Any]] = {}
        # base grouping knob: flat = drop container locality, all
        # lines go to the (doc_index, "") document group; the size
        # guardrail still applies
        flat = self.base_grouping == "flat"

        def ensure_group(path: List[str], doc_index: int):
            """Get or create a container group by (doc_index, path), preserving document order."""
            if flat:
                path = []
            key = (doc_index, cc.path_str(path))
            if key not in groups:
                groups[key] = {
                    "path": list(path),
                    "doc_index": doc_index,
                    "lines": [],
                    "line_ranges": [],
                    "line_paths": [],
                    "semantic_paths": [],
                    "entry_count": 0,
                    "line_start": None,
                    "line_end": None,
                }
            return groups[key]

        def extend_range(g, start, end):
            """Merge an entry's line range into the group's line_start/line_end."""
            if start is not None:
                g["line_start"] = start if g["line_start"] is None else min(g["line_start"], start)
            if end is not None:
                g["line_end"] = end if g["line_end"] is None else max(g["line_end"], end)

        # register container groups first (preserve document order)
        for c in containers:
            ensure_group(c.get("path") or [], int(c.get("doc_index") or 0))

        # assign each entry to its parent group
        for e in entries:
            path = e.get("path") or []
            doc_index = int(e.get("doc_index") or 0)
            g = ensure_group(path[:-1], doc_index)
            leaf = self._leaf_of(e)
            disp = self._display_path(path, doc_index)
            masked = self._record_is_masked(e)
            value_text = "****" if masked else self._render_entry_value(e)
            g["lines"].append(f"{disp}: {value_text}")
            g["line_ranges"].append((e.get("line_start"), e.get("line_end")))
            g["line_paths"].append(disp)
            g["semantic_paths"].append(disp)
            g["entry_count"] += 1
            extend_range(g, e.get("line_start"), e.get("line_end"))

        # child-container marker lines go to the parent group
        # (empty containers / non-enumerated inline containers;
        # enumerated containers have their own group and are not
        # duplicated in the parent)
        for c in containers:
            path = c.get("path") or []
            if not path:
                continue
            doc_index = int(c.get("doc_index") or 0)
            child_count = c.get("child_count")
            if child_count is not None and child_count != 0:
                continue
            g = ensure_group(path[:-1], doc_index)
            leaf = self._leaf_of(c)
            disp = self._display_path(path, doc_index)
            if (
                c.get("value_state") in (cc.VALUE_STATE_EMPTY_DICT, cc.VALUE_STATE_EMPTY_LIST)
                or child_count == 0
            ):
                marker = "[]" if c.get("container_type") == "list" else "{}"
            else:
                marker = self._render_inline(
                    structure,
                    path,
                    doc_index,
                    leaf,
                )
            g["lines"].append(f"{disp}: {marker}")
            g["line_ranges"].append((c.get("line_start"), c.get("line_end")))
            g["line_paths"].append(None)
            extend_range(g, c.get("line_start"), c.get("line_end"))

        units: List[RepresentationUnit] = []
        idx = 0
        for g in groups.values():
            if not g["lines"]:
                continue
            disp_root = (
                self._display_path(
                    g["path"],
                    g["doc_index"],
                )
                if g["path"]
                else ""
            )
            for lines, ranges, paths, split_i, split_n in self._split_base_group(g):
                part_start, part_end = self._range_of(ranges)
                if part_start is None:
                    part_start, part_end = (
                        g["line_start"],
                        g["line_end"],
                    )
                unit_meta: Dict[str, Any] = {
                    "granularity": "base",
                    "unit_index": idx,
                    "semantic_paths": paths,
                    "sections": [g["path"][-1]] if g["path"] else [],
                    "container_path": disp_root,
                    "entry_count": g["entry_count"],
                    "line_start": part_start,
                    "line_end": part_end,
                    "doc_index": g["doc_index"],
                    "structural_role": "config_group",
                    "semantic_scope": "config",
                    # flat grouping does not own section_locality
                    # (honesty)
                    "representation_semantics": {
                        "semantic_path_preserved": True,
                        "section_locality": not flat,
                        "config_atomicity": True,
                    },
                }
                if split_n > 1:
                    # record the split honestly: consecutive entry
                    # segments of the same container, never broken
                    # mid-entry.
                    unit_meta["split_reason"] = "base_unit_size_guardrail"
                    unit_meta["split_part"] = split_i
                    unit_meta["split_total"] = split_n
                units.append(
                    self._make_unit(
                        text="\n".join(lines),
                        granularity="base",
                        sequence_index=idx,
                        path=g["path"],
                        doc_index=g["doc_index"],
                        locality_scope="document" if flat else "section",
                        file_name=file_name,
                        file_type=file_type,
                        line_start=part_start,
                        line_end=part_end,
                        metadata=unit_meta,
                    )
                )
                idx += 1
        return units

    @staticmethod
    def _range_of(ranges) -> Tuple[Optional[int], Optional[int]]:
        """Merge (start, end) line ranges into (min_start, max_end)."""
        starts = [r[0] for r in ranges if r and r[0] is not None]
        ends = [r[1] for r in ranges if r and r[1] is not None]
        return (
            min(starts) if starts else None,
            max(ends) if ends else None,
        )

    def _split_base_group(self, g):
        """Split an oversized container group to fit the max_base_chars budget.

        Splits only at entry line boundaries, never inside an entry
        atom; a single line over budget gets a shard of its own
        (it cannot be split further — pathological input, kept
        verbatim).

        Yields:
            (lines, ranges, semantic_paths, split_part, split_total);
            when under the threshold a single shard is yielded
            as-is, with split_part=split_total=1.
        """
        lines = g["lines"]
        budget = self.max_base_chars
        if budget is None or len(lines) <= 1:
            yield (lines, g["line_ranges"], g["semantic_paths"], 1, 1)
            return
        total_len = sum(len(line) for line in lines) + len(lines) - 1
        if total_len <= budget:
            yield (lines, g["line_ranges"], g["semantic_paths"], 1, 1)
            return
        chunks: List[Tuple[list, list, list]] = []
        cur_lines: list = []
        cur_ranges: list = []
        cur_paths: list = []
        cur_len = 0
        for line, rng, path in zip(
            lines,
            g["line_ranges"],
            g["line_paths"],
        ):
            add = len(line) + (1 if cur_lines else 0)
            if cur_lines and cur_len + add > budget:
                chunks.append((cur_lines, cur_ranges, cur_paths))
                cur_lines, cur_ranges, cur_paths = [], [], []
                cur_len = 0
                add = len(line)
            cur_lines.append(line)
            cur_ranges.append(rng)
            if path is not None:
                cur_paths.append(path)
            cur_len += add
        if cur_lines:
            chunks.append((cur_lines, cur_ranges, cur_paths))
        split_total = len(chunks)
        for split_part, (cl, cr, cp) in enumerate(chunks, 1):
            yield cl, cr, cp, split_part, split_total

    # ========================================================
    # comment-only fallback — honest units for comment-only
    # documents
    # ========================================================

    def _build_comment_only_units(
        self,
        comments: List[Dict[str, Any]],
        file_name: Optional[str],
        file_type: Optional[str],
    ) -> Tuple[List[RepresentationUnit], List[RepresentationUnit]]:
        """Comment-fallback units when entries/containers are both empty.

        Called only when the document has no entry or container at
        all: the comments are the document's entire real content.
        The fine layer emits one unit per comment (verbatim
        rendering); the base layer merges them into one
        document-level unit; line numbers and types come from the
        contract facts.
        """
        fine: List[RepresentationUnit] = []
        lines: List[str] = []
        line_start: Optional[int] = None
        line_end: Optional[int] = None

        for c in comments:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            rendered = (c.get("raw") or "").strip() or f"# {text}"
            ls = c.get("line_start")
            le = c.get("line_end")
            doc_index = int(c.get("doc_index") or 0)
            fine.append(
                self._make_unit(
                    text=rendered,
                    granularity="fine",
                    sequence_index=len(fine),
                    path=[],
                    doc_index=doc_index,
                    locality_scope="entry",
                    file_name=file_name,
                    file_type=file_type,
                    line_start=ls,
                    line_end=le,
                    metadata={
                        "granularity": "fine",
                        "semantic_path": "(comment)",
                        "comment_kind": c.get("kind"),
                        "comment_only": True,
                        "line_start": ls,
                        "line_end": le,
                        "doc_index": doc_index,
                    },
                )
            )
            lines.append(rendered)
            if ls is not None:
                line_start = ls if line_start is None else min(line_start, ls)
            if le is not None:
                line_end = le if line_end is None else max(line_end, le)

        base: List[RepresentationUnit] = []
        if lines:
            base.append(
                self._make_unit(
                    text="\n".join(lines),
                    granularity="base",
                    sequence_index=0,
                    path=[],
                    doc_index=0,
                    locality_scope="document",
                    file_name=file_name,
                    file_type=file_type,
                    line_start=line_start,
                    line_end=line_end,
                    metadata={
                        "granularity": "base",
                        "unit_index": 0,
                        "semantic_paths": ["(comments)"],
                        "sections": [],
                        "container_path": "",
                        "entry_count": 0,
                        "comment_count": len(lines),
                        "comment_only": True,
                        "line_start": line_start,
                        "line_end": line_end,
                        "doc_index": 0,
                        "structural_role": "config_group",
                        "semantic_scope": "config",
                        "representation_semantics": {
                            "semantic_path_preserved": True,
                            "section_locality": True,
                            "config_atomicity": True,
                        },
                    },
                )
            )
        return fine, base

    # ========================================================
    # coarse — top-level subtree granularity
    # ========================================================

    def _build_coarse(
        self,
        base_units: List[RepresentationUnit],
    ) -> List[RepresentationUnit]:
        if not base_units:
            return []
        coarse_groups: Dict[str, Dict[str, Any]] = {}

        for unit in base_units:
            m = unit.metadata or {}
            path: List[str] = list(unit.heading_path or [])
            doc_index = int(m.get("doc_index") or 0)
            if doc_index and path and path[0].startswith("doc"):
                top = path[0]
            elif path:
                top = str(path[0])
            else:
                top = _ROOT_GROUP_LABEL
            group = coarse_groups.setdefault(
                top,
                {
                    "path": [] if top == _ROOT_GROUP_LABEL else [top],
                    "doc_index": doc_index,
                    "lines": [],
                    "semantic_paths": [],
                    "source_units": 0,
                    "line_start": unit.source_context.line_start,
                    "line_end": unit.source_context.line_end,
                },
            )
            group["lines"].append(unit.text)
            group["semantic_paths"].extend(m.get("semantic_paths", []))
            group["source_units"] += 1
            if unit.source_context.line_start is not None:
                group["line_start"] = (
                    unit.source_context.line_start
                    if group["line_start"] is None
                    else min(group["line_start"], unit.source_context.line_start)
                )
            if unit.source_context.line_end is not None:
                group["line_end"] = (
                    unit.source_context.line_end
                    if group["line_end"] is None
                    else max(group["line_end"], unit.source_context.line_end)
                )

        # merge the root scalar group into the only top-level
        # group (if any) to avoid fragments
        root_group = coarse_groups.pop(_ROOT_GROUP_LABEL, None)
        if root_group:
            if len(coarse_groups) == 1:
                only = next(iter(coarse_groups.values()))
                only["lines"] = root_group["lines"] + only["lines"]
                only["semantic_paths"] = root_group["semantic_paths"] + only["semantic_paths"]
                only["source_units"] += root_group["source_units"]
            else:
                coarse_groups[_ROOT_GROUP_LABEL] = root_group

        first = base_units[0]
        file_name = first.source_context.file_name
        file_type = first.source_context.file_type

        units: List[RepresentationUnit] = []
        for idx, (top, g) in enumerate(coarse_groups.items()):
            units.append(
                self._make_unit(
                    text="\n".join(g["lines"]),
                    granularity="coarse",
                    sequence_index=idx,
                    path=g["path"],
                    doc_index=g["doc_index"],
                    locality_scope="document",
                    file_name=file_name,
                    file_type=file_type,
                    line_start=g["line_start"],
                    line_end=g["line_end"],
                    metadata={
                        "granularity": "coarse",
                        "top_path": top,
                        "semantic_paths": g["semantic_paths"],
                        "source_unit_count": g["source_units"],
                        "entry_count": len(g["semantic_paths"]),
                        "doc_index": g["doc_index"],
                    },
                )
            )
        return units


# ============================================================
# Format subclasses — keep the existing class names (the
# client/universal_parser assertions on
# metadata["representation"] and import paths depend on them)
# ============================================================


class YamlConfigRepresentation(ConfigRepresentation):
    """YAML configuration representation."""

    fmt = "yaml"


class JsonConfigRepresentation(ConfigRepresentation):
    """JSON configuration representation."""

    fmt = "json"


class Json5ConfigRepresentation(ConfigRepresentation):
    """JSON5 configuration representation (its own Profile).

    fmt="json5" is kept separate from
    JsonConfigRepresentation(fmt="json"): the representation
    identity follows the parser routing (.json5 → Json5Parser)
    and does not share the json fmt with JSONC — evidence-only.
    """

    fmt = "json5"


class DotfileConfigRepresentation(ConfigRepresentation):
    """Dotfile JSON/YAML configuration representation (.eslintrc/.babelrc family).

    fmt="dotfile_json_yaml" covers the eight file shapes
    (.eslintrc/.babelrc plus .json/.yml/.yaml variants): the
    content syntax is a runtime detection fact (strict JSON /
    lenient JSONC / YAML fallback), disclosed by the contract
    layer dotfile_identity (detected_syntax/parser_route)
    rather than by the Representation fmt.
    """

    fmt = "dotfile_json_yaml"


class TomlConfigRepresentation(ConfigRepresentation):
    """TOML configuration representation."""

    fmt = "toml"


class XmlConfigRepresentation(ConfigRepresentation):
    """XML configuration representation."""

    fmt = "xml"


class IniConfigRepresentation(ConfigRepresentation):
    """INI configuration representation."""

    fmt = "ini"


class PropertiesConfigRepresentation(ConfigRepresentation):
    """Java properties configuration representation."""

    fmt = "properties"


class EnvConfigRepresentation(ConfigRepresentation):
    """dotenv configuration representation."""

    fmt = "env"


# direnv .envrc configuration representation. Shares the full
# ConfigRepresentation chain (get_contract only checks
# contract_version, no format gating — both key-value entries
# and directive entries flatten semantically); the fmt label
# is disclosed independently.
class EnvrcConfigRepresentation(ConfigRepresentation):
    """direnv .envrc configuration representation (identify-only contract flattening)."""

    fmt = "envrc"


class PlistConfigRepresentation(ConfigRepresentation):
    """Apple plist configuration representation."""

    fmt = "plist"


# .tf/.tfvars share one HclConfigRepresentation (fmt="hcl");
# get_contract only checks contract_version with no format
# gating, the config_entry_v1.1 contract is flattened directly;
# Terraform profile differences are disclosed in parser
# metadata. When parser metadata carries terraform_profile,
# build() additionally attaches terraform_summary_md to the
# result metadata (an auxiliary view, not a replacement for
# the original HCL); with no profile, behavior is unchanged.
class HclConfigRepresentation(ConfigRepresentation):
    """Terraform HCL native configuration representation."""

    fmt = "hcl"

    def build(self, document):
        """Build: super() contract projection + terraform_profile summary."""
        result = super().build(document)
        try:
            profile = (document.metadata or {}).get("terraform_profile")
            if profile:
                contract = cc.get_contract(document)
                md = render_markdown_summary(profile, contract)
                if md:
                    result.metadata["terraform_summary_md"] = md
        except Exception:  # pragma: no cover - defensive guard
            # Summary is an auxiliary channel; failures degrade silently.
            pass
        return result


# HCL JSON variants (.tf.json / .tfvars.json / .tftest.json)
# share this representation chain; the contract is produced by
# hcl_json_parser in the same config_entry_v1.1 shape. The
# summary channel and flattening match the native variant
# exactly; only the fmt label discloses the JSON origin.
class HclJsonConfigRepresentation(HclConfigRepresentation):
    """Terraform HCL JSON variant configuration representation."""

    fmt = "hcl_json"


# Generic .conf configuration representation. The
# content-gated parser emits a config_entry_v1.1 contract
# (flattening five generic syntax families plus zero-IO
# include registration); when a file is handed off to an
# existing family, the corresponding parser produces the
# contract and this chain is shared all the same. The fmt
# label is "conf"; detection results are disclosed via
# parser metadata conf_profile.
class ConfConfigRepresentation(ConfigRepresentation):
    """Generic .conf configuration representation.

    Content-gated flattening of five generic syntax families.
    """

    fmt = "conf"

    def build(self, document):
        """Build: super() contract projection + tool profile summary.

        When parser metadata carries conf_tool_profile,
        conf_summary_md is attached to the super().build()
        result metadata (an auxiliary view, not a replacement
        for the original .conf); with no profile, behavior is
        unchanged.
        """
        result = super().build(document)
        try:
            profile = (document.metadata or {}).get("conf_tool_profile")
            if profile:
                from provendoc.core.parsers.config.conf_tool_profile import (
                    render_conf_summary,
                )

                contract = cc.get_contract(document)
                md = render_conf_summary(profile, contract)
                if md:
                    result.metadata["conf_summary_md"] = md
        except Exception:  # pragma: no cover - defensive guard
            # Summary is an auxiliary channel; failures degrade silently.
            pass
        return result


class CnfConfigRepresentation(ConfigRepresentation):
    """Generic `.cnf` configuration representation.

    Content-gated section/kv/flag flattening.
    """

    fmt = "cnf"

    def build(self, document):
        """Build: super() contract projection + profile summary.

        When parser metadata carries cnf_tool_profile,
        cnf_summary_md is attached to the super().build()
        result metadata (an auxiliary view, not a replacement
        for the contract); with no profile, behavior is
        unchanged.
        """
        result = super().build(document)
        try:
            profile = (document.metadata or {}).get("cnf_tool_profile")
            if profile:
                from provendoc.core.parsers.config.cnf_tool_profile import (
                    render_cnf_summary,
                )

                contract = cc.get_contract(document)
                md = render_cnf_summary(profile, contract)
                if md:
                    result.metadata["cnf_summary_md"] = md
        except Exception:  # pragma: no cover - defensive guard
            # Summary is an auxiliary channel; failures degrade silently.
            pass
        return result


class SystemdConfigRepresentation(ConfigRepresentation):
    """systemd `.service`/`.timer` unit configuration representation.

    Extension-gated flattening; semantic details are disclosed
    via parser metadata systemd_profile / systemd_status.
    """

    fmt = "systemd"

    def build(self, document):
        """Build: super() contract projection + three summary channels.

        When parser metadata carries systemd_profile,
        systemd_summary_md / systemd_summary_json /
        systemd_dependency_graph are attached to the
        super().build() result metadata (auxiliary views, not
        replacements for the contract); with no profile,
        behavior is unchanged.
        """
        result = super().build(document)
        try:
            profile = (document.metadata or {}).get("systemd_profile")
            if profile:
                from provendoc.core.parsers.config.systemd_unit_profile import (
                    render_systemd_graph,
                    render_systemd_json,
                    render_systemd_summary,
                )

                contract = cc.get_contract(document)
                md = render_systemd_summary(profile, contract)
                if md:
                    result.metadata["systemd_summary_md"] = md
                js = render_systemd_json(profile, contract)
                if js:
                    result.metadata["systemd_summary_json"] = js
                gr = render_systemd_graph(profile)
                if gr:
                    result.metadata["systemd_dependency_graph"] = gr
        except Exception:  # pragma: no cover - defensive guard
            # Summary is an auxiliary channel; failures degrade silently.
            pass
        return result


# Generic .config configuration representation. The extension
# family is not a single syntax, so the contract is produced by
# a five-way delegation (xml/json/yaml/ini/toml) or the
# ConfParser generic recovery layer. The fmt label is "config";
# detection results are disclosed via parser metadata
# config_profile.
class DotConfigConfigRepresentation(ConfigRepresentation):
    """Generic `.config` configuration representation."""

    fmt = "config"

    def build(self, document):
        """Build: super() contract projection + profile summary.

        When parser metadata carries dotnet_config_profile,
        dotconfig_summary_md is attached to the super().build()
        result metadata (an auxiliary view, not a replacement
        for the contract); with no profile, behavior is
        unchanged.
        """
        result = super().build(document)
        try:
            profile = (document.metadata or {}).get(
                "dotnet_config_profile"
            )
            if profile:
                from provendoc.core.parsers.config.dotconfig_profile import (
                    render_dotconfig_summary,
                )

                contract = cc.get_contract(document)
                config_profile = (document.metadata or {}).get(
                    "config_profile"
                )
                md = render_dotconfig_summary(
                    profile, contract, config_profile,
                )
                if md:
                    result.metadata["dotconfig_summary_md"] = md
        except Exception:  # pragma: no cover - defensive guard
            # Summary is an auxiliary channel; failures degrade silently.
            pass
        return result
