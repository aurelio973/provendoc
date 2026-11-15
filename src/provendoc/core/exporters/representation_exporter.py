"""Representation-layer exporter — the official contract
implementation for the final structured output.

Contract (docs/*/internal/artifact_contract.md §2.3):
    representation = the full cleaned text (fact source of the
                   units)
                   + a cleaning trace summary
                   + coarse/base/fine three-granularity
                   RepresentationUnit views
                   + policy_snapshot + statistics

    - All three granularities are official representation views,
      not intermediate states.
    - Granularities that were not generated keep the field with
      status="not_generated".
    - Contains no evaluation conclusions (evaluation belongs to
      the evaluation layer, DQR to the raw layer).
"""

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from provendoc.base.exporter import BaseExporter
from provendoc.base.models.representation_unit import RepresentationUnit
from provendoc.core.exporters.atomic_write import atomic_write_json
from provendoc.core.foundation.serialization import safe_serialize


def _unit_view(unit: RepresentationUnit) -> Dict[str, Any]:
    """Serialized view of one RepresentationUnit (fills in
    unit_id, keeps all fields)."""
    data: Dict[str, Any]
    if hasattr(unit, "to_dict"):
        data = unit.to_dict()
    else:
        # dataclass without to_dict: fall back to the instance
        # field dict.
        data = dict(vars(unit))
    primitives = data.get("semantic_primitives") or {}
    data.setdefault(
        "unit_id",
        primitives.get("identity")
        or f"{data.get('granularity', 'base')}_{data.get('sequence_index', 0):04d}",
    )
    return data


def _granularity_view(
    units: Optional[List[RepresentationUnit]],
    generated: bool,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Single-granularity view: available / empty / not_generated,
    with a fixed schema."""
    if units is None:
        units = []
    serialized_units = [_unit_view(u) for u in units]
    if not generated:
        status = "not_generated"
    elif not serialized_units:
        # the granularity is enabled but no unit met the split
        # condition, e.g. an emoji text with no analyzable sentence
        status = "empty"
    else:
        status = "available"
    view: Dict[str, Any] = {
        "status": status,
        "units": serialized_units,
    }
    if status == "not_generated":
        view["reason"] = reason or "granularity_disabled"
    elif status == "empty":
        view["reason"] = reason or "no_eligible_units"
    view["unit_count"] = len(serialized_units)
    view["character_count"] = sum(
        len(u.get("text", "")) for u in serialized_units
    )
    return view


class RepresentationExporter(BaseExporter):
    """L3 representation-layer exporter.

    Inputs (provided by the runtime):
        document      — Document (text is the cleaned working
                        text; metadata["cleaning"] carries the
                        cleaning trace)
        repr_result   — RepresentationResult whose content is
                        either MultiGranularityOutput
                        (coarse/base/fine) or a plain
                        List[RepresentationUnit] (treated as base)
        policy        — RepresentationPolicy (for the
                        policy_snapshot)
        selected_granularity — the default consumption granularity
                        for this run (default "base")
    """

    def export(
            self,
            document,
            repr_result,
            path,
            policy=None,
            selected_granularity: str = "base",
    ):
        """Export the L3 representation-layer JSON (three-granularity
        views + cleaning trace).

        Args:
            document: Document (text is the cleaned working text).
            repr_result: RepresentationResult; content is either
                MultiGranularityOutput or a list of base units.
            path: output file path (atomic write).
            policy: optional RepresentationPolicy, written into
                policy_snapshot.
            selected_granularity: default consumption granularity
                (default "base").
        """
        metadata = getattr(repr_result, "metadata", None) or {}
        content = getattr(repr_result, "content", None)

        # ── cleaning trace (conclusion-level; before/after diffs
        # belong to the debug layer) ──
        cleaning_meta = (document.metadata or {}).get("cleaning", {})
        if cleaning_meta:
            cleaning = {
                "applied": True,
                "summary": cleaning_meta,
            }
        else:
            cleaning = {
                "applied": False,
                "summary": {},
            }

        # ── three-granularity views ──
        coarse_units = getattr(content, "coarse", None)
        base_units = getattr(content, "base", None)
        fine_units = getattr(content, "fine", None)
        fine_enabled = getattr(content, "fine_enabled", False)

        if coarse_units is not None or base_units is not None:
            # MultiGranularityOutput container
            chunks: Dict[str, Any] = {
                "selected_granularity": selected_granularity,
                "coarse": _granularity_view(
                    coarse_units, generated=coarse_units is not None,
                    reason="coarse_granularity_disabled",
                ),
                "base": _granularity_view(
                    base_units, generated=base_units is not None,
                    reason="base_granularity_disabled",
                ),
                "fine": _granularity_view(
                    fine_units, generated=fine_enabled,
                    reason=(
                        "no_over_budget_paragraphs"
                        if fine_enabled and not fine_units
                        else "fine_granularity_disabled"
                    ),
                ),
            }
        else:
            # plain List[RepresentationUnit] (legacy /
            # non-multi-granularity representation)
            units = content if isinstance(content, list) else []
            chunks = {
                "selected_granularity": selected_granularity,
                "coarse": _granularity_view(
                    None, generated=False,
                    reason="multi_granularity_not_enabled",
                ),
                "base": _granularity_view(units, generated=True),
                "fine": _granularity_view(
                    None, generated=False,
                    reason="multi_granularity_not_enabled",
                ),
            }

        # ── policy_snapshot: the policy configuration used to
        # generate the units ──
        policy_snapshot: Dict[str, Any] = {}
        if policy is not None:
            if hasattr(policy, "to_dict"):
                policy_snapshot["representation_policy"] = policy.to_dict()
            elif hasattr(policy, "__dataclass_fields__"):
                policy_snapshot["representation_policy"] = safe_serialize(
                    asdict(policy)
                )
            else:
                policy_snapshot["representation_policy"] = safe_serialize(
                    policy
                )
        if "granularity_stats" in metadata:
            policy_snapshot["granularity_stats"] = metadata[
                "granularity_stats"
            ]

        # ── statistics ──
        statistics = {
            g: {
                "unit_count": chunks[g]["unit_count"],
                "character_count": chunks[g]["character_count"],
            }
            for g in ("coarse", "base", "fine")
        }

        projected = {
            "layer": "representation",
            "representation_type": getattr(
                repr_result, "representation_type", "semantic"
            ),
            "document_id": (document.metadata or {}).get("file_name"),
            "cleaning": cleaning,
            # the cleaned full text: fact source of the units
            # (landing point of source spans)
            "cleaned_text": document.text,
            "chunks": chunks,
            "policy_snapshot": policy_snapshot,
            "statistics": statistics,
            "metadata": {
                k: v for k, v in metadata.items()
                if k != "granularity_stats"
            },
        }

        # ── object-level unresolved registry (marker_contract.md
        # §1/§5.3) ──
        # The ``*_unresolved`` key family in document.metadata
        # (object-level honest-absence registries written by
        # parsers, e.g. pptx_unresolved / docx_unresolved /
        # pdf_unresolved) is passed through into the representation
        # envelope: the structured output must keep the full
        # registry — machine consumers use this field instead of
        # reverse-parsing markers out of markdown. Additive:
        # written only when non-empty; documents with no unresolved
        # objects get no field (same §1.1 key convention and
        # honest-absence discipline).
        unresolved_registry = {
            k: v
            for k, v in (getattr(document, "metadata", None) or {}).items()
            if k.endswith("_unresolved")
        }
        if unresolved_registry:
            projected["unresolved_registry"] = unresolved_registry

        serialized = safe_serialize(projected)

        atomic_write_json(path, serialized, indent=2)
