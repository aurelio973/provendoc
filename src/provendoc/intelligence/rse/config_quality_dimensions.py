"""Six-norm dimension checks of ConfigQuality (mixin).

Contains the six Phase 2 dimension implementations (split from config_quality.py in 2026-09,
moved verbatim):
- _hierarchy_continuity       bidirectional tree connectivity + empty-container retention + depth preservation
- _path_integrity             path integrity + namespace disambiguation
- _locality_preservation      sibling adjacency
- _value_fidelity             path→value bijection (coverage/correctness)
- _dependency_integrity       three-level reference assessment (detection/traceability/resolvability)
- _source_anomaly_handling    evidence quality of source-anomaly handling

The methods are composed via ConfigQuality (see provendoc.intelligence.rse.config_quality)
as a mixin; fact-layer methods and class constants are provided by the facade side. The move
only fixed three stale comments inconsistent with frozen weights and one mypy type debt
(the source_scalars annotation), with no runtime behavior change.
"""

import re
from typing import TYPE_CHECKING, Any, Dict, List

from provendoc.core.parsers.config import contracts as cc
from provendoc.intelligence.rse.config_quality_model import _EntryFact

if TYPE_CHECKING:
    from typing import Callable

# ==================================
# dependency_integrity within-dimension three-level mixing coefficients (E3 named-constant
# registration, zero numeric change)
#
# Reconciliation lock: contract B in tests/test_scoring_sensitivity.py —
# score = _REF_DETECTION_BASE + _REF_TRACE_WEIGHT × (traced/detected)
#       + _REF_RESOLUTION_WEIGHT × (resolved/detected)
# (E2 example: 0.3 + 0.3×2/3 + 0.4×2/3 = 0.767). Changing a value turns the test red;
# a prereg version bump is required (dead_parameter_cleanup_prereg_v1 §2.2).
# Note: since v1.2 this dimension is excluded from overall (included_in_overall=False,
# numbers kept in observations), but the within-dimension formula is still a reconciliation target.
# ==================================

#: Detection baseline of the three-level reference assessment (scored once detected)
_REF_DETECTION_BASE = 0.3

#: Within-dimension weight for traceability (bound to an entry path)
_REF_TRACE_WEIGHT = 0.3

#: Within-dimension weight for resolvability (five-state == resolved)
_REF_RESOLUTION_WEIGHT = 0.4


class ConfigQualityDimensionsMixin:
    """Collection of six-norm dimension-check methods of ConfigQuality (no fields, methods only).

    Fact-layer methods (_representation_paths / _source_structure /
    _source_verifiable / _max_depth / _unit_path) and the class constant
    (_INTERPOLATION_PATTERNS) and _is_severe_loss are provided by the facade class
    ConfigQuality and its FactsMixin; the annotations here only provide
    a contract for static checking.
    """

    if TYPE_CHECKING:
        _INTERPOLATION_PATTERNS: List[re.Pattern[str]]
        _representation_paths: Callable[..., Any]
        _source_structure: Callable[..., Any]
        _source_verifiable: Callable[..., Any]
        _max_depth: Callable[..., Any]
        _unit_path: Callable[..., Any]
        _is_severe_loss: Callable[[str], bool]

    # ==================================
    # Dimension 1: hierarchy_continuity (PRIMARY, 0.30)
    # merged hierarchy_preservation + nesting_integrity
    # ==================================

    def _hierarchy_continuity(
        self, document, facts, fine_units, base_units,
        profile, breakdowns,
    ) -> float:
        """Bidirectional tree connectivity + empty-container retention + depth preservation.

        Sub-checks (breakdown preserves diagnostic granularity):
            connectivity        child→parent: every ancestor level of an entry is in the container set
            container_retention parent→child: empty/unenumerated containers retained in the representation
            depth_preservation  entry path depth <= source structure depth
        """
        sub: Dict[str, Any] = {}

        # Child→parent: broken-path check (tightened to a full per-level scan)
        total = 0
        consistent = 0
        for e in facts.entries:
            total += 1
            segs = e.path_str.split(".")
            if all(
                ".".join(segs[:d]) in facts.container_paths
                for d in range(1, len(segs))
            ):
                consistent += 1
        sub["connectivity"] = round(consistent / max(total, 1), 3)

        # Parent→child: silent empty-container loss check (contract path only)
        if facts.containers:
            repr_paths = self._representation_paths(
                fine_units, base_units
            )
            # Empty / unenumerated containers must have an explicit marker presence;
            # enumerated containers are implicitly retained by child entries.
            targets = [
                c for c in facts.containers
                if (c.get("path_str") or "")
                and c.get("child_count") in (0, None)
            ]
            if targets:
                retained = sum(
                    1 for c in targets
                    if c["path_str"] in repr_paths
                )
                sub["container_retention"] = round(
                    retained / len(targets), 3
                )

        # Depth preservation (only meaningful when the source structure is verifiable; the error
        # marker degraded from a parse failure is not a real structure, comparing depth is meaningless)
        structure = self._source_structure(document)
        if structure is not None and self._source_verifiable(document):
            source_depth = self._max_depth(structure)
            correct = sum(
                1 for e in facts.entries
                if e.path_str.count(".") + 1 <= source_depth
            )
            sub["depth_preservation"] = round(
                correct / max(len(facts.entries), 1), 3
            )

        breakdowns["hierarchy_continuity"] = sub
        if not sub:
            return 1.0
        return round(sum(sub.values()) / len(sub), 3)

    # ==================================
    # Dimension 2: path_integrity (PRIMARY, 0.17)
    # merged semantic_path_completeness + namespace_disambiguation
    # ==================================

    def _path_integrity(
        self, facts, fine_units, base_units,
        profile, breakdowns,
    ) -> float:
        """Path integrity + namespace disambiguation (sub-check enabled only on collision)."""
        sub: Dict[str, Any] = {}

        units = fine_units or base_units
        if not units:
            breakdowns["path_integrity"] = sub
            return 0.0

        # Integrity: every unit has a non-empty semantic path
        def _has_semantic_path(u):
            if u.semantic_primitives.hierarchy_path:
                return True
            sp = u.metadata.get("semantic_paths")
            if isinstance(sp, list) and sp:
                return True
            return bool(u.metadata.get("semantic_path"))

        complete = sum(1 for u in units if _has_semantic_path(u))
        sub["completeness"] = round(complete / len(units), 3)

        # Disambiguation: colliding leaves must carry a namespace prefix (>= 2 segments)
        if profile.has_namespace_collision:
            leaf_names: Dict[str, int] = {}
            for e in facts.entries:
                leaf = e.path_str.split(".")[-1].split("[")[0]
                leaf_names[leaf] = leaf_names.get(leaf, 0) + 1
            colliding = {
                leaf for leaf, n in leaf_names.items() if n >= 2
            }
            if colliding:
                total = 0
                disambiguated = 0
                for e in facts.entries:
                    leaf = e.path_str.split(".")[-1].split("[")[0]
                    if leaf in colliding:
                        total += 1
                        if e.path_str.count(".") >= 1:
                            disambiguated += 1
                sub["disambiguation"] = round(
                    disambiguated / max(total, 1), 3
                )

        breakdowns["path_integrity"] = sub
        if not sub:
            return 1.0
        return round(sum(sub.values()) / len(sub), 3)

    # ==================================
    # Dimension 3: locality_preservation (PRIMARY, 0.13)
    # ==================================

    def _locality_preservation(
        self, facts, fine_units, profile
    ) -> float:
        """Measure sibling key adjacency in the atomic unit sequence.

        For each sibling group (keys sharing the same parent path):
            expected_span = len(siblings) - 1  (ideal: all adjacent)
            actual_span = max(index) - min(index)
            locality_score = expected_span / actual_span

        The sequence is the fine-grained unit order when available
        (entry atoms in document order); otherwise entry facts.
        """
        if profile.sibling_count == 0:
            return 1.0

        if fine_units:
            seq_paths = [self._unit_path(u) for u in fine_units]
        else:
            seq_paths = [e.path_str for e in facts.entries]

        sibling_groups: Dict[str, List[int]] = {}
        for i, path in enumerate(seq_paths):
            if not path:
                continue
            segments = path.split(".")
            if len(segments) <= 1:
                continue
            parent = ".".join(segments[:-1])
            sibling_groups.setdefault(parent, []).append(i)

        scores = []
        for members in sibling_groups.values():
            if len(members) < 2:
                continue
            actual_span = max(members) - min(members)
            expected_span = len(members) - 1
            if actual_span == 0:
                scores.append(1.0)
            else:
                scores.append(expected_span / actual_span)

        if not scores:
            return 1.0
        return round(sum(scores) / len(scores), 3)

    # ==================================
    # Dimension 4: value_fidelity (PRIMARY, 0.20)
    # merged flattening_stability + scalar_extraction_correctness
    # ==================================

    def _value_fidelity(
        self, document, facts, profile, breakdowns
    ) -> float:
        """Both directions of the path→value bijection (contract facts, never reverse-parsing text).

        coverage    recall: source scalar → contract entry (masked exemption)
        correctness precision: contract entry → source scalar (masked exemption)
        """
        sub: Dict[str, Any] = {}

        structure = self._source_structure(document)
        if structure is None:
            breakdowns["value_fidelity"] = {
                "note": "no source structure"
            }
            return 1.0

        source_scalars: Dict[str, Any] = {}
        self._collect_scalars(structure, "", source_scalars)
        # The parse-failure marker key is not a config value and does not participate in comparison
        source_scalars.pop("_error", None)
        if not source_scalars:
            breakdowns["value_fidelity"] = {
                "note": "no source scalars"
            }
            return 1.0

        # Masked exemption: sensitive keys are removed from both sides
        source_scalars = {
            p: v for p, v in source_scalars.items()
            if not cc.is_sensitive_key(p.split(".")[-1])
        }
        entries = [e for e in facts.entries if not e.masked]

        # None documents (empty/comment-only) are removed from both sides: they carry no config information.
        # Their entry path_str is empty, so candidate keys degrade to bare doc indices.
        if isinstance(structure, list):
            none_idx = {
                str(i) for i, x in enumerate(structure) if x is None
            }
            if none_idx:
                source_scalars = {
                    p: v for p, v in source_scalars.items()
                    if p not in none_idx
                }
                entries = [
                    e for e in entries
                    if not (not e.path_str and e.value is None
                            and str(e.doc_index) in none_idx)
                ]

        def _entry_keys(e):
            """entry → source-scalar candidate keys (bare key + doc-prefixed key)."""
            if e.path_str:
                return (e.path_str, f"{e.doc_index}.{e.path_str}")
            return (str(e.doc_index),)

        # Dual-key matching: for multi-document files (YAML ---), structured_data is
        # a list of documents, scalar paths carry an "N." prefix, while contract path_str is
        # the in-document path (doc_index is carried separately); array-root files (JSON [..])
        # also mount a list but with doc_index=0 and path_str already indexed. Both shapes use
        # dual-candidate matching (bare key + doc-prefixed key) without cross-misjudging each other.
        if source_scalars:
            entry_by_key: Dict[str, _EntryFact] = {}
            for e in entries:
                for key in _entry_keys(e):
                    entry_by_key.setdefault(key, e)
            matched = sum(
                1 for p, v in source_scalars.items()
                if p in entry_by_key
                and str(entry_by_key[p].value) == str(v)
            )
            sub["coverage"] = round(
                matched / len(source_scalars), 3
            )

        if entries:
            # v1.1 double-counting guard: container-valued entries (TOML inline container parents and
            # derived intermediate containers) are excluded from correctness — their value fidelity is
            # carried by scalar children; otherwise the parent would be misjudged as incorrect for
            # "no corresponding source scalar" and double-count the same fact with its children.
            scalar_entries = [
                e for e in entries
                if not isinstance(e.value, (dict, list))
            ]
            correct = 0
            for e in scalar_entries:
                for key in _entry_keys(e):
                    if key in source_scalars and (
                        str(source_scalars[key]) == str(e.value)
                    ):
                        correct += 1
                        break
            if scalar_entries:
                sub["correctness"] = round(
                    correct / len(scalar_entries), 3
                )

        breakdowns["value_fidelity"] = sub
        if not sub:
            # All masked / nothing verifiable → quality-neutral
            return 1.0
        return round(sum(sub.values()) / len(sub), 3)

    @staticmethod
    def _collect_scalars(obj, parent_path, result):
        """Collect scalar path→value pairs (dot-path, aligned with contract path_str:
        list index segments are decimal strings joined by dots)."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                path = (
                    f"{parent_path}.{key}" if parent_path else str(key)
                )
                if isinstance(value, (dict, list)):
                    ConfigQualityDimensionsMixin._collect_scalars(
                        value, path, result
                    )
                else:
                    result[path] = value
        elif isinstance(obj, list):
            for idx, value in enumerate(obj):
                path = (
                    f"{parent_path}.{idx}" if parent_path else str(idx)
                )
                if isinstance(value, (dict, list)):
                    ConfigQualityDimensionsMixin._collect_scalars(
                        value, path, result
                    )
                else:
                    result[path] = value

    # ==================================
    # Dimension 5: dependency_integrity (SECONDARY, 0.15)
    # ==================================

    def _dependency_integrity(
        self, facts, fine_units, base_units, profile
    ) -> float:
        """Three-level assessment of cross-references.

        Level 1 — detection: references present in facts
        Level 2 — traceability: refs bound to an entry path
        Level 3 — resolvability: five-state status == resolved

        Contract path: consume contract references directly (five-state).
        Fallback path: text regex detection + partial path matching (detection level only).
        """
        if not profile.has_interpolation:
            return 1.0

        # ---- contract path ----
        if facts.refs:
            refs = facts.refs
            detected = len(refs)
            traced = sum(1 for r in refs if r.get("path_str"))
            resolved = sum(
                1 for r in refs
                if r.get("resolution_status") == cc.REF_RESOLVED
            )
            score = (
                _REF_DETECTION_BASE
                + _REF_TRACE_WEIGHT * (traced / detected)
                + _REF_RESOLUTION_WEIGHT * (resolved / detected)
            )
            return round(score, 3)

        # ---- fallback: text regex over units ----
        refs_detected = 0
        refs_traced = 0
        refs_resolved = 0

        all_paths = set()
        for e in facts.entries:
            all_paths.add(e.path_str)
            segments = e.path_str.split(".")
            for i in range(1, len(segments)):
                all_paths.add(".".join(segments[:i]))

        for unit in (fine_units or base_units):
            for pattern in self._INTERPOLATION_PATTERNS:
                for match in pattern.findall(unit.text):
                    refs_detected += 1
                    if unit.metadata.get("references") or \
                            getattr(unit, "references", None):
                        refs_traced += 1
                    ref_path = (
                        match if isinstance(match, str) else match[0]
                    )
                    if ref_path in all_paths:
                        refs_resolved += 1
                    else:
                        for p in all_paths:
                            if ref_path in p or p in ref_path:
                                refs_resolved += 1
                                break

        if refs_detected == 0:
            return 1.0

        score = (
            _REF_DETECTION_BASE
            + _REF_TRACE_WEIGHT * (refs_traced / refs_detected)
            + _REF_RESOLUTION_WEIGHT * (refs_resolved / refs_detected)
        )
        return round(score, 3)

    # ==================================
    # Dimension 6: source_anomaly_handling (SECONDARY, 0.05)
    # ==================================

    def _source_anomaly_handling(
        self, facts, breakdowns
    ) -> float:
        """Evidence quality of source-anomaly handling (not anomaly existence).

        Score semantics: no anomalies → 1.0; all anomalies carrying evidence
        (override_chain / no severe loss reason) → 1.0;
        missing evidence is deducted proportionally. Anomaly existence itself is exposed by issues
        (the score measures overall quality, issues track risk).
        """
        duplicates = [e for e in facts.entries if e.duplicate]
        handled = [e for e in duplicates if e.override_chain]
        severe_losses = [
            r for r in facts.loss_reasons
            if self._is_severe_loss(r)
        ]

        breakdowns["source_anomaly_handling"] = {
            "duplicate_count": len(duplicates),
            "duplicate_evidence": (
                round(len(handled) / len(duplicates), 3)
                if duplicates else 1.0
            ),
            "severe_loss_reasons": severe_losses,
        }

        total_events = len(duplicates) + len(severe_losses)
        if total_events == 0:
            return 1.0
        # severe loss reasons are treated as unhandled risk events
        handled_events = len(handled)
        return round(handled_events / total_events, 3)
