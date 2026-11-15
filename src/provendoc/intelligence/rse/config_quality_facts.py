"""Fact-layer methods of ConfigQuality (mixin).

Contains Layer 1 fact-view construction and the Phase 0/1 pre-stages (split from
config_quality.py in 2026-09, moved verbatim):
- Unit extraction (_extract_units, MultiGranularityOutput aware)
- Fact view (_build_fact_view / _facts_from_contract / _facts_from_units)
- Path utilities (_unit_path / _representation_paths)
- Source-structure checks (_source_structure / _is_error_marker / _source_verifiable)
- Format capability profiling (_detect_format_capabilities / _max_depth)
- Applicability check (_check_applicability)

The methods are composed via ConfigQuality (see provendoc.intelligence.rse.config_quality)
as a mixin; class constants (_INTERPOLATION_PATTERNS / _DOC_PREFIX_RE
etc.) and the public orchestration are provided by that facade class. The move only fixed existing mypy type debt
and two stale weight comments, with no runtime behavior change.
"""

import re
from typing import TYPE_CHECKING, Dict, List, Set

from provendoc.core.parsers.config import contracts as cc
from provendoc.intelligence.rse.config_quality_model import (
    FormatCapabilityProfile,
    _EntryFact,
    _FactView,
)

if TYPE_CHECKING:
    from typing import Callable


class ConfigQualityFactsMixin:
    """Collection of fact-layer and pre-stage methods of ConfigQuality (no fields, methods only).

    Class constants (_INTERPOLATION_PATTERNS / _DOC_PREFIX_RE) and
    _count_sibling_groups are provided by the facade class ConfigQuality; the annotations here
    only provide a contract for static checking.
    """

    if TYPE_CHECKING:
        _INTERPOLATION_PATTERNS: List[re.Pattern[str]]
        _DOC_PREFIX_RE: re.Pattern[str]
        _count_sibling_groups: Callable[[_FactView], int]

    # ==================================
    # Unit extraction (MultiGranularityOutput aware)
    # ==================================

    @staticmethod
    def _extract_units(semantic_representation):
        """Extract the (fine, base) unit lists from RepresentationResult.content.

        - MultiGranularityOutput: three-granularity container, take the fine/base views
        - Legacy list: treated as base (old one-chunk-per-entry representation)
        """
        content = semantic_representation.content
        if hasattr(content, "base"):
            return list(content.fine), list(content.base)
        if isinstance(content, list):
            return [], list(content)
        return [], []

    # ==================================
    # Layer 1: Fact view construction
    # ==================================

    def _build_fact_view(
        self, document, fine_units, base_units
    ) -> _FactView:
        """Build the fact view contract-first; synthesize from representation units when no contract exists."""
        contract = cc.get_contract(document)
        if contract is not None:
            return self._facts_from_contract(contract)
        return self._facts_from_units(fine_units, base_units)

    @staticmethod
    def _facts_from_contract(contract) -> _FactView:
        entries: List[_EntryFact] = []
        for e in contract.get("entries", []):
            path = e.get("path") or []
            leaf = str(
                e.get("key") or (path[-1] if path else "")
            )
            entries.append(_EntryFact(
                path_str=(
                    e.get("path_str")
                    or cc.path_str(path)
                ),
                value=e.get("value"),
                masked=cc.is_sensitive_key(leaf),
                duplicate=bool(e.get("duplicate")),
                override_chain=e.get("override_chain"),
                references=list(e.get("references") or []),
                doc_index=int(e.get("doc_index") or 0),
                derived=(
                    e.get("source_precision")
                    == cc.SOURCE_PRECISION_LINE_ONLY
                ),
            ))

        containers = [
            dict(c) for c in contract.get("containers", [])
        ]
        container_paths: Set[str] = {""}
        for c in containers:
            container_paths.add(
                c.get("path_str") or cc.path_str(c.get("path") or [])
            )

        loss = contract.get("loss") or {}
        return _FactView(
            source=cc.CONFIG_ENTRY_CONTRACT_VERSION,
            entries=entries,
            containers=containers,
            container_paths=container_paths,
            refs=list(contract.get("references") or []),
            duplicate_key_semantics=contract.get(
                "duplicate_key_semantics"
            ),
            loss_reasons=[
                str(r) for r in (loss.get("reasons") or []) if r
            ],
        )

    def _facts_from_units(
        self, fine_units, base_units
    ) -> _FactView:
        """No-contract fallback: synthesize facts from representation units (value-check dimensions will record N/A)."""
        units = fine_units or base_units
        entries: List[_EntryFact] = []
        for u in units:
            path = self._unit_path(u)
            if not path:
                continue
            entries.append(_EntryFact(
                path_str=path,
                value=u.metadata.get("value"),
                masked=bool(u.metadata.get("masked")),
                duplicate=bool(
                    u.metadata.get(
                        "duplicate", getattr(u, "duplicate_flag", False)
                    )
                ),
                override_chain=u.metadata.get("override_chain"),
                references=list(
                    u.metadata.get("references")
                    or getattr(u, "references", [])
                    or []
                ),
            ))

        # Synthesize container set from ancestor prefixes (connectivity check degrades to always-true on the fallback path)
        container_paths: Set[str] = {""}
        for e in entries:
            segs = e.path_str.split(".")
            for d in range(1, len(segs)):
                container_paths.add(".".join(segs[:d]))

        return _FactView(
            source="representation_fallback",
            entries=entries,
            container_paths=container_paths,
        )

    @classmethod
    def _unit_path(cls, unit) -> str:
        """Unit -> dot-separated path string (strips the multidoc docN prefix)."""
        p = unit.metadata.get("semantic_path")
        if isinstance(p, str) and p:
            path = p
        else:
            hp = unit.semantic_primitives.hierarchy_path
            path = ".".join(str(s) for s in hp) if hp else ""
        if path:
            path = cls._DOC_PREFIX_RE.sub("", path, count=1)
        return path

    def _representation_paths(
        self, fine_units, base_units
    ) -> Set[str]:
        """All paths appearing in the representation (fine units + base-group semantic_paths)."""
        paths: Set[str] = set()
        for u in fine_units:
            p = self._unit_path(u)
            if p:
                paths.add(p)
        for u in base_units:
            for sp in u.metadata.get("semantic_paths") or []:
                if isinstance(sp, str) and sp:
                    paths.add(
                        self._DOC_PREFIX_RE.sub("", sp, count=1)
                    )
        return paths

    # ==================================
    # Source structure (INI-aware)
    # ==================================

    @staticmethod
    def _source_structure(document):
        """structured_data -> structure tree.

        INI mounts as a {"titles", "contents", "tables", "structure"}
        wrapper; YAML/JSON/TOML mount the bare parsed data.
        """
        structured = (document.metadata or {}).get("structured_data")
        if isinstance(structured, dict):
            if {"structure", "contents", "tables"} <= set(
                structured.keys()
            ):
                return structured.get("structure")
            return structured
        return structured

    @classmethod
    def _is_error_marker(cls, obj) -> bool:
        """Parse-failure marker shape (parser fault-tolerance contract): {"_error": ...}
        or {"_error": ..., "_partial_data": ...}."""
        return (
            isinstance(obj, dict)
            and "_error" in obj
            and set(obj.keys()) <= {"_error", "_partial_data"}
        )

    @classmethod
    def _source_verifiable(cls, document) -> bool:
        """Whether the source structure is verifiable (used for value_fidelity applicability).

        Not verifiable: structure is missing, or has degraded into a parse-error marker
        (e.g. {"_error": ...}, including single-doc error placeholders in a multidoc
        list) — value comparison is meaningless there; the parse failure itself is
        exposed as an issue (severe loss reason).
        """
        structure = cls._source_structure(document)
        if structure is None:
            return False
        if isinstance(structure, list):
            return not any(
                cls._is_error_marker(x) for x in structure
            )
        return not cls._is_error_marker(structure)

    # ==================================
    # Phase 0: Format Capability Detection
    # ==================================

    def _detect_format_capabilities(
        self, document, facts, fine_units, base_units
    ) -> FormatCapabilityProfile:
        """Detect format capabilities from document and facts."""
        profile = FormatCapabilityProfile()

        meta = getattr(document, "metadata", {}) or {}
        profile.format_type = meta.get("file_type", "unknown")

        # Hierarchy detection (INI-aware source structure)
        structure = self._source_structure(document)
        if structure is not None:
            depth = self._max_depth(structure)
            profile.has_hierarchy = depth >= 2
            profile.hierarchy_depth = depth

        # Interpolation detection
        # Contract references take priority (based on raw values, unaffected by masking)
        if facts.refs:
            profile.has_interpolation = True
        else:
            for unit in (fine_units or base_units):
                if any(
                    p.search(unit.text)
                    for p in self._INTERPOLATION_PATTERNS
                ):
                    profile.has_interpolation = True
                    break

        # Namespace collision detection (from entry facts)
        leaf_names: Dict[str, int] = {}
        for e in facts.entries:
            leaf = e.path_str.split(".")[-1].split("[")[0]
            leaf_names[leaf] = leaf_names.get(leaf, 0) + 1
        profile.has_namespace_collision = any(
            count >= 2 for count in leaf_names.values()
        )

        # Representation flattened
        # Every entry carries a full path and entry count > 1
        profile.representation_flattened = (
            len(facts.entries) > 1
            and all(e.path_str for e in facts.entries)
        )

        # Sibling count (entry facts grouped by parent)
        profile.sibling_count = self._count_sibling_groups(facts)

        return profile

    @staticmethod
    def _max_depth(obj, current=1):
        """Calculate maximum nesting depth of a dict/list structure."""
        if isinstance(obj, dict):
            if not obj:
                return current
            return max(
                ConfigQualityFactsMixin._max_depth(v, current + 1)
                for v in obj.values()
            )
        elif isinstance(obj, list):
            if not obj:
                return current
            return max(
                ConfigQualityFactsMixin._max_depth(v, current + 1)
                for v in obj
            )
        return current

    # ==================================
    # Phase 1: Applicability Check
    # ==================================

    def _check_applicability(
        self, profile: FormatCapabilityProfile, facts: _FactView,
        source_verifiable: bool = True,
    ) -> Dict[str, Dict]:
        """Check applicability for each dimension.

        Basis = format capabilities + fact availability (no evidence to judge -> N/A).
        """
        has_contract = facts.source == cc.CONFIG_ENTRY_CONTRACT_VERSION
        return {
            "hierarchy_continuity": {
                "applicable": profile.has_hierarchy
                              and profile.hierarchy_depth >= 2,
                "reason": (
                    ""
                    if profile.has_hierarchy
                    and profile.hierarchy_depth >= 2
                    else "no hierarchy or depth < 2"
                ),
            },
            "path_integrity": {
                "applicable": True,
                "reason": "",
            },
            "locality_preservation": {
                "applicable": profile.sibling_count >= 1,
                "reason": (
                    ""
                    if profile.sibling_count >= 1
                    else "insufficient sibling groups"
                ),
            },
            "value_fidelity": {
                # Value fidelity must come from structured entry facts (contract)
                # and a verifiable source structure; no contract = no evidence;
                # source degraded to a parse-error marker = unverifiable source
                # (parse failure is exposed by an issue, not double-penalized in the score) — both record N/A rather than degrading the reverse parse.
                "applicable": has_contract and source_verifiable,
                "reason": (
                    ""
                    if has_contract and source_verifiable
                    else (
                        "no structured entry facts "
                        "(config_entry contract missing)"
                        if not has_contract
                        else "source structure unverifiable "
                             "(parse error)"
                    )
                ),
            },
            "dependency_integrity": {
                "applicable": profile.has_interpolation,
                "reason": (
                    ""
                    if profile.has_interpolation
                    else "no interpolation detected"
                ),
            },
            "source_anomaly_handling": {
                "applicable": True,
                "reason": "",
            },
        }
