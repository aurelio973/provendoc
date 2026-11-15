"""Table Chunk Quality Index (TQI) — spreadsheet semantics evaluation.

Domain: spreadsheet
Formats: CSV, TSV, PSV, Excel (xlsx, xls), Parquet (future)

Core question:
    "Is the table's semantics complete?"

Unlike NarrativeQuality, this evaluator does NOT care about:
    - lexical cohesion (cells are not prose)
    - paragraph boundaries (tables have no paragraphs)
    - semantic continuity (rows are independent records)

Instead it asks three orthogonal questions (table_quality_v2.0,
dimension_taxonomy_refactor_prereg_v1 2026-10-26 — a consolidated-question
perspective: dimensions are orthogonal questions, not duplicate declarations of parsing facts;
subcheck computation logic untouched line by line):
    - unit_coherence       — units unbroken: region data rows are fully covered by base
                             units and row-level units map one-to-one to source rows
                             (subchecks
                             table_integrity 0.5 +
                             row_integrity 0.5）
    - relation_continuity  — relations unbroken: formulas preserved in rendered text
                             and cross-sheet relation targets addressable (subcheck
                             formula_relation_integrity, within-dimension
                             0.7/0.3 mixing coefficients unchanged)
    - provenance_fidelity  — content neither lost nor hallucinated: headers bind to contract,
                             cell_ids traceable to canvas (subchecks
                             header_binding 0.5 +
                             cell_source_fidelity 0.5）

Evidence-layer/deterministic scaffolding assertions stay out of scoring (config path_integrity
v1.3 same-criterion precedent): region_locality (locality_scope match +
unique sheet attribution, measured constant 1.0 across the 916-file corpus) is demoted to
observations — values retained for the invariant track's profile→observations
fallback score reads, issues kept for the gate (zero drift in gate inputs). All six
subchecks' raw scores are exposed under their original names in observations.

"Good chunk" for spreadsheet = structure-preserving row groups
whose every cell is traceable back to the canvas facts.

Fact sources (two-layer, mirrors ConfigQuality):
    Layer 1 — spreadsheet_v1 contract (sc.get_contract(document)):
        canvas rows / regions / cells / workbook.relations provide
        the ground truth (data rows, headers, expected cell ids,
        formula cells, cross-sheet relation targets).
    Layer 2 — MultiGranularityOutput units (fine row / base row
        group / coarse sheet): carry region_id, region_type,
        row_index / row_start / row_end, header_binding, cell_ids,
        locality_scope, identity.
    Without a contract the evaluator degrades to structure-only
    checks (uniqueness / modal agreement) and dims that need
    ground truth are reported N/A — never guessed.

Canonical weight policy (same contract as config_quality_v1):
    Weights are CANONICAL and FIXED. When a dimension is not
    applicable (N/A), its weight is NOT redistributed:

        overall = Σ(active_dim_score × weight) / Σ(all weights)

    A CSV with no formulas scores formula_relation_integrity as
    N/A and its overall is already discounted — this forbids
    "easy" documents from inflating their overall score.

Confidence decomposition:
    confidence = dimension_coverage × evaluation_depth
                 × format_reliability
    (fallback fact source discounts format_reliability by 0.85,
    mirroring ConfigQuality; document quality warnings penalize it).

Skeleton mode is RETIRED: dimension logic is real, so the
invariant track no longer caps confidence at 0.5
(see table_invariants.SKELETON_MODE = False).
"""

from provendoc.intelligence.rse.quality_base import BaseQuality

# ==================================
# formula_relation_integrity within-dimension mixing coefficients (E3 named-constant registry,
# zero change in values)
#
# Reconciliation lock: tests/test_scoring_sensitivity.py contract B —
# score = _FORMULA_PRESERVATION_WEIGHT × formula_score
#       + _RELATION_RESOLVABILITY_WEIGHT × relation_score
# (E1 example: 0.7×0.75 + 0.3×1.0 = 0.825). Changing the values turns tests red,
# requiring a prereg version bump (dead_parameter_cleanup_prereg_v1 §2.2).
# ==================================

#: within-dimension weight for formula preservation in rendered text
_FORMULA_PRESERVATION_WEIGHT = 0.7

#: within-dimension weight for resolvability of cross-sheet relation targets
_RELATION_RESOLVABILITY_WEIGHT = 0.3


class TableQuality(BaseQuality):
    """Table-domain Chunk Quality Index (TQI).

    Evaluates chunk quality for spreadsheet data where the ingestion
    philosophy is:

        table is a schema-bound row collection → chunks should
        preserve row integrity and cell traceability → each chunk
        is a complete, self-describing row group

    Dimensions and weights (table_quality_v2.0, FROZEN):
        [PRIMARY]
        unit_coherence             1/3   rows intact (coverage + 1:1)
        [SECONDARY]
        relation_continuity        1/3   formulas preserved, targets resolvable
        provenance_fidelity        1/3   cells traceable, headers match

    Observations (diagnostic-only, included_in_overall=False):
        table_integrity / row_integrity / header_binding /
        cell_source_fidelity / region_locality /
        formula_relation_integrity — subcheck raw scores kept under original names,
        for invariant-track fallback score reads and audit consumption.

    Output structure (same shape as ConfigQuality):
        profile             — dimensional breakdown (N/A dims are null)
        observations        — subcheck raw scores (diagnostic layer,
                              included_in_overall=False; invariant track
                              profile→observations fallback score reads)
        overall             — advisory weighted sum (canonical weights)
        confidence          — evaluation reliability (decomposed)
        confidence_profile  — dimension_coverage × evaluation_depth
                              × format_reliability
        applicability       — per-dimension applicability status + reasons
        issues              — per-dimension problems
        metadata            — chunk statistics + fact_source + weight_policy
        invariant_track     — parallel table invariants (dual-track)
    """

    # v1.2 change (E4 uniform baseline, decision date 2026-10-25):
    #   user-adjudicated methodology (uniform-baseline vs ordinal-scale, former chosen):
    #   no prior on dimension importance; all six dimension weights are 1/6.
    #   (stage-0 measured evidence and uniform-lock notes: prereg:
    #   uniform_baseline_calibration_prereg_v1.md)
    #
    # v2.0 change (dimension taxonomy refactor, decision date 2026-10-26):
    #   user challenged "dimensions each tell their own story, not a consolidated-question view" → full-corpus
    #   measurement (four_domain_quality_v1, 916 files): among the six dims,
    #   table_integrity / row_integrity / cell_source_fidelity /
    #   region_locality are four constant-1.0 dims (std=0), formula dim
    #   N/A 90.7%. The scoring layer converges to the three-question framework dims
    #   （unit_coherence / relation_continuity /
    #   provenance_fidelity), subcheck computation logic untouched line by line,
    #   raw scores go to observations for invariant-track fallback reads (zero drift);
    #   region_locality demoted to observations on constant evidence (config
    #   path_integrity v1.3 same-criterion precedent).
    #   behavioral drift: N/A cost goes from 1/6 to 1/3 (no-formula corpus cap
    #   5/6 → 2/3, intentional change); uniform lock (contract E) dim count 6→3.
    #   prereg: dimension_taxonomy_refactor_prereg_v1.md
    PROFILE_VERSION = "table_quality_v2.0"

    WEIGHTS = {
        "unit_coherence": 1 / 3,
        "relation_continuity": 1 / 3,
        "provenance_fidelity": 1 / 3,
    }

    PRIMARY_DIMENSIONS = {
        "unit_coherence",
        # legacy subcheck names kept in the severity set: subcheck issues'
        # severity judgment uses original names (zero drift in gate inputs, prereg §2.4)
        "table_integrity",
        "row_integrity",
    }

    SECONDARY_DIMENSIONS = {
        "relation_continuity",
        "provenance_fidelity",
        "header_binding",
        "cell_source_fidelity",
        "region_locality",
        "formula_relation_integrity",
    }

    # Observation-layer dims: subcheck raw scores exposed under original names,
    # included_in_overall=False (prereg §2.4).
    OBSERVATION_DIMS = frozenset({
        "table_integrity",
        "row_integrity",
        "header_binding",
        "cell_source_fidelity",
        "region_locality",
        "formula_relation_integrity",
    })

    # Demoted dims (evidence-layer/deterministic assertions out of scoring, prereg §2.3):
    # values go only to observations, issues stay in the main list (gate unchanged).
    DEMOTED_DIMS = frozenset({"region_locality"})

    CONTRACT_FACT_SOURCES = frozenset({"spreadsheet_v1_contract"})

    def evaluate(
        self,
        document,
        semantic_representation,
        strategy=None,
        document_quality=None,
    ):
        """Evaluate table chunk quality.

        Args:
            document: Document with spreadsheet contract in metadata
            semantic_representation: RepresentationResult whose content
                is a MultiGranularityOutput (fine/base/coarse) or a
                flat unit list
            strategy: RepresentationPolicy (unused for tables, but
                kept for interface symmetry)
            document_quality: DocumentQualityReport result

        Returns:
            dict with profile, overall, confidence, applicability,
            issues, metadata, invariant_track
        """
        fine, base, coarse = self._extract_units(semantic_representation)

        if not base and not fine and not coarse:
            return self._empty_result()

        facts = self._build_facts(document)

        # ==================================
        # Phase 1: subchecks (v1.2 logic as-is) → three-question consolidated dims
        # ==================================
        subchecks = {
            "table_integrity": self._table_integrity(facts, base),
            "row_integrity": self._row_integrity(facts, fine, base),
            "header_binding": self._header_binding(facts, fine, base),
            "cell_source_fidelity": self._cell_source_fidelity(
                facts, fine, base,
            ),
            "region_locality": self._region_locality(
                facts, base, coarse,
            ),
            "formula_relation_integrity": self._formula_relation_integrity(
                facts, fine, base, coarse,
            ),
        }

        dim_results = {
            "unit_coherence": self._merge_subchecks(
                "unit_coherence",
                (
                    ("table_integrity", 0.5),
                    ("row_integrity", 0.5),
                ),
                subchecks,
            ),
            "relation_continuity": self._merge_subchecks(
                "relation_continuity",
                (("formula_relation_integrity", 1.0),),
                subchecks,
            ),
            "provenance_fidelity": self._merge_subchecks(
                "provenance_fidelity",
                (
                    ("header_binding", 0.5),
                    ("cell_source_fidelity", 0.5),
                ),
                subchecks,
            ),
        }

        profile, applicability, issues = self._aggregate_dimensions(
            dim_results,
        )

        # subcheck raw scores (incl. demoted dims) → observations; demoted dims'
        # issues stay in the main list (gate inputs unchanged, prereg §2.4)
        observations = self._observations_from(subchecks)
        for name in self.DEMOTED_DIMS:
            _score, applicable, _reason, sub_issues = subchecks[name]
            if applicable and sub_issues:
                issues.extend(sub_issues)

        # ==================================
        # Phase 2: canonical overall — N/A weights not redistributed
        # ==================================
        overall = self._canonical_overall(profile, applicability)

        # ==================================
        # Phase 3: confidence (decomposed)
        # ==================================
        confidence, confidence_profile = self._confidence(
            applicability, facts, document_quality,
        )

        lengths = [
            len(u.text)
            for u in list(fine) + list(base)
            if getattr(u, "text", None) is not None
        ]

        result = {
            "profile_version": self.PROFILE_VERSION,
            "profile": profile,
            "observations": observations,
            "applicability": applicability,
            "overall": round(overall, 3),
            "confidence": round(confidence, 3),
            "confidence_profile": confidence_profile,
            "issues": issues,
            "metadata": {
                "chunk_count": len(fine) + len(base),
                "avg_chunk_length": round(
                    sum(lengths) / max(len(lengths), 1), 2
                ),
                "min_chunk_length": min(lengths) if lengths else 0,
                "max_chunk_length": max(lengths) if lengths else 0,
                "fine_unit_count": len(fine),
                "base_unit_count": len(base),
                "coarse_unit_count": len(coarse),
                "fact_source": facts["source"],
                "weight_policy": "canonical_no_redistribution",
                "table_region_count": len(facts["table_regions"]),
                "formula_cell_count": len(facts["formula_cells"]),
                "relation_count": len(facts["relations"]),
            },
        }
        return self._attach_invariant_track(result, fine, base, coarse, document)

    # ==================================
    # unit extraction
    # ==================================

    @staticmethod
    def _extract_units(semantic_representation):
        """Extract (fine, base, coarse) units from the representation.

        Handles MultiGranularityOutput (spreadsheet pipeline) and
        flat unit lists (shims / legacy). Flat lists are treated as
        base-level units.
        """
        if semantic_representation is None:
            return [], [], []
        content = getattr(semantic_representation, "content", semantic_representation)
        if content is None:
            return [], [], []
        if hasattr(content, "fine") and hasattr(content, "base"):
            fine = list(content.fine or [])
            base = list(content.base or [])
            coarse = list(getattr(content, "coarse", None) or [])
            return fine, base, coarse
        if isinstance(content, (list, tuple)):
            return [], list(content), []
        return [], [], []

    # ==================================
    # fact view (Layer 1: contract ground truth)
    # ==================================

    def _build_facts(self, document):
        """Build the ground-truth fact view.

        Prefers the spreadsheet_v1 contract; without a contract the
        evaluator degrades to structure-only checks and dims that
        need ground truth report N/A.
        """
        contract = None
        try:
            from provendoc.core.parsers.table import contracts as sc
            contract = sc.get_contract(document)
        except Exception:
            contract = None

        if contract is not None:
            return self._facts_from_contract(contract)
        return {
            "source": "unit_metadata_fallback",
            "table_regions": [],
            "formula_cells": [],
            "relations": [],
        }

    @staticmethod
    def _facts_from_contract(contract):
        """Derive per-region ground truth from the spreadsheet_v1 contract."""
        workbook = contract.get("workbook") or {}
        table_regions = []
        formula_cells = []

        for sheet in workbook.get("sheets") or []:
            canvas = sheet.get("canvas") or {}
            cells = canvas.get("cells") or []
            rows = canvas.get("rows") or []
            cells_by_position = {
                (c.get("row"), c.get("column")): c for c in cells
            }
            rows_by_index = {r.get("row_index"): r for r in rows}

            for cell in cells:
                if cell.get("formula"):
                    formula_cells.append({
                        "cell_id": cell.get("id"),
                        "formula": cell["formula"],
                        "sheet_name": sheet.get("name"),
                    })

            for region in canvas.get("regions") or []:
                if region.get("region_type") != "table":
                    continue
                bounds = region.get("bounds") or {}
                header_row = region.get("header_row_index")
                data_rows = []
                expected_cell_ids = []
                # same definition as representation-layer _row_cell_ids (drift-proof):
                # canvas cells within the region-bounds column range (no blank filtering),
                # not all cell_ids of the physical row (the latter includes out-of-region cells)
                for index in range(
                    bounds.get("start_row", 0), bounds.get("end_row", -1) + 1
                ):
                    row = rows_by_index.get(index)
                    if row is None or row.get("is_blank"):
                        continue
                    if index == header_row:
                        continue
                    data_rows.append(index)
                    for column in range(
                        bounds.get("start_column", 1),
                        bounds.get("end_column", 0) + 1,
                    ):
                        cell = cells_by_position.get((index, column))
                        if cell is not None:
                            expected_cell_ids.append(cell.get("id"))
                # same parsing definition as representation-layer _headers (drift-proof):
                # parser-provided headers take priority, fallback to derivation
                try:
                    from provendoc.core.parsers.table import contracts as sc
                    headers = region.get("headers") or sc.region_headers(
                        region, cells_by_position,
                    )
                except Exception:
                    headers = region.get("headers") or []
                table_regions.append({
                    "id": region.get("id"),
                    "sheet_name": sheet.get("name"),
                    "data_rows": data_rows,
                    "expected_cell_ids": expected_cell_ids,
                    "headers": headers,
                })

        relations = [
            r for r in workbook.get("relations") or []
            if r.get("relation_type") == "formula_reference"
        ]
        return {
            "source": "spreadsheet_v1_contract",
            "table_regions": table_regions,
            "formula_cells": formula_cells,
            "relations": relations,
        }

    # ==================================
    # dimension helpers
    # ==================================

    @staticmethod
    def _meta(unit, key, default=None):
        try:
            return unit.metadata.get(key, default)
        except Exception:
            return default

    @staticmethod
    def _freeze_key(value):
        """Normalize dict keys to hashable (lists recursively frozen to tuples).

        Metadata values such as region_id are self-reported by upstream extractors; using a list directly as a key raises
        TypeError: unhashable type — freeze instead of drop, grouping semantics unchanged.
        """
        if isinstance(value, list):
            return tuple(TableQuality._freeze_key(v) for v in value)
        return value

    @staticmethod
    def _locality_scope(unit):
        sp = getattr(unit, "semantic_primitives", None)
        return getattr(sp, "locality_scope", None) if sp is not None else None

    @staticmethod
    def _identity(unit):
        sp = getattr(unit, "semantic_primitives", None)
        return getattr(sp, "identity", None) if sp is not None else None

    @staticmethod
    def _normalize_binding(binding):
        """Normalize a header_binding list to comparable tuples."""
        if not binding:
            return []
        normalized = []
        for item in binding:
            if isinstance(item, dict):
                column = item.get("column")
                name = item.get("name")
                if name is None:
                    name = item.get("value", "")
                normalized.append((column, str(name or "")))
            else:
                normalized.append((None, str(item)))
        return normalized

    @staticmethod
    def _units_for_region(units, region_id):
        return [
            u for u in units
            if TableQuality._meta(u, "region_id") == region_id
        ]

    # ==================================
    # dimension: table_integrity (PRIMARY)
    # ==================================
    #
    # Every table region's data rows (ground truth from the
    # contract, header row excluded) must be covered by base
    # units exactly once — no missing rows, no duplicates.
    # ==================================

    def _table_integrity(self, facts, base):
        dim = "table_integrity"
        if facts["source"] == "spreadsheet_v1_contract":
            regions = facts["table_regions"]
            if not regions:
                return 0.0, False, "no_table_regions", []
            scores = []
            issues = []
            for region in regions:
                expected = set(region["data_rows"])
                if not expected:
                    continue
                covered = []
                for unit in self._units_for_region(base, region["id"]):
                    start = self._meta(unit, "row_start")
                    end = self._meta(unit, "row_end")
                    if start is None or end is None:
                        continue
                    covered.extend(range(start, end + 1))
                hits = len(set(covered) & expected)
                dups = len(covered) - len(set(covered))
                score = max(0.0, (hits - dups) / len(expected))
                scores.append(score)
                if score < 0.999:
                    missing = len(expected) - hits
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"region {region['id']}: {missing} missing / "
                        f"{dups} duplicated data rows in base units",
                    ))
            if not scores:
                return 0.0, False, "no_table_regions_with_data_rows", []
            return sum(scores) / len(scores), True, "", issues

        # fallback: contiguity heuristic over base groups
        by_region = {}
        for unit in base:
            if self._meta(unit, "region_type") == "table":
                by_region.setdefault(
                    self._freeze_key(self._meta(unit, "region_id")), []
                ).append(unit)
        if not by_region:
            return 0.0, False, "no_table_regions", []
        scores = []
        for region_id, units in by_region.items():
            spans = [
                (self._meta(u, "row_start"), self._meta(u, "row_end"))
                for u in units
                if self._meta(u, "row_start") is not None
            ]
            if not spans:
                continue
            span = max(e for _, e in spans) - min(s for s, _ in spans) + 1
            covered = [r for s, e in spans for r in range(s, e + 1)]
            scores.append(len(set(covered)) / max(span, 1))
        if not scores:
            return 0.0, False, "no_row_spans_in_base_units", []
        return sum(scores) / len(scores), True, "", []

    # ==================================
    # dimension: row_integrity (PRIMARY)
    # ==================================
    #
    # Row-granularity (fine) units must map 1:1 to the source
    # data rows of each table region — no split rows, no
    # duplicated rows, no phantom rows.
    # ==================================

    def _row_integrity(self, facts, fine, base):
        dim = "row_integrity"
        if not fine:
            return 0.0, False, "no_row_granularity_units", []

        if facts["source"] == "spreadsheet_v1_contract":
            regions = facts["table_regions"]
            if not regions:
                return 0.0, False, "no_table_regions", []
            scores = []
            issues = []
            for region in regions:
                expected = set(region["data_rows"])
                if not expected:
                    continue
                rows = [
                    self._meta(u, "row_index")
                    for u in self._units_for_region(fine, region["id"])
                    if self._meta(u, "row_index") is not None
                ]
                hits = len(set(rows) & expected)
                dups = len(rows) - len(set(rows))
                score = max(0.0, (hits - dups) / len(expected))
                scores.append(score)
                if score < 0.999:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"region {region['id']}: {len(expected) - hits} "
                        f"unrepresented / {dups} duplicated rows in fine units",
                    ))
            if not scores:
                return 0.0, False, "no_table_regions_with_data_rows", []
            return sum(scores) / len(scores), True, "", issues

        # fallback: (region_id, row_index) uniqueness across fine units
        keys = []
        for unit in fine:
            row_index = self._meta(unit, "row_index")
            if row_index is not None:
                keys.append((self._meta(unit, "region_id"), row_index))
        if not keys:
            return 0.0, False, "no_row_indexed_fine_units", []
        return len(set(keys)) / len(keys), True, "", []

    # ==================================
    # dimension: header_binding (SECONDARY)
    # ==================================
    #
    # The header binding (column → name) attached to region
    # units must match the contract ground truth (the same
    # binding the representation layer renders "Headers:" from).
    # ==================================

    def _header_binding(self, facts, fine, base):
        dim = "header_binding"
        if facts["source"] == "spreadsheet_v1_contract":
            regions = [
                r for r in facts["table_regions"] if r["headers"]
            ]
            if not regions:
                return 0.0, False, "no_header_bound_table_regions", []
            scores = []
            issues = []
            for region in regions:
                truth = self._normalize_binding(region["headers"])
                units = self._units_for_region(fine, region["id"]) + \
                    self._units_for_region(base, region["id"])
                if not units:
                    scores.append(0.0)
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"region {region['id']}: no representation units",
                    ))
                    continue
                matching = sum(
                    1 for u in units
                    if self._normalize_binding(self._meta(u, "header_binding"))
                    == truth
                )
                score = matching / len(units)
                scores.append(score)
                if score < 0.999:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"region {region['id']}: {len(units) - matching}"
                        f"/{len(units)} units carry a divergent header_binding",
                    ))
            return sum(scores) / len(scores), True, "", issues

        # fallback: modal agreement within each region
        by_region = {}
        for unit in list(fine) + list(base):
            binding = self._normalize_binding(self._meta(unit, "header_binding"))
            if binding:
                by_region.setdefault(
                    self._freeze_key(self._meta(unit, "region_id")), []
                ).append(binding)
        if not by_region:
            return 0.0, False, "no_header_bindings_in_units", []
        scores = []
        for _, bindings in by_region.items():
            counts = {}
            for b in bindings:
                counts[b] = counts.get(b, 0) + 1
            modal = max(counts.values())
            scores.append(modal / len(bindings))
        return sum(scores) / len(scores), True, "", []

    # ==================================
    # dimension: cell_source_fidelity (SECONDARY)
    # ==================================
    #
    # Every non-blank data cell of each table region must be
    # traceable: the region's units together reference the
    # expected cell_ids exactly once — no dropped cells, no
    # duplicated cells.
    # ==================================

    def _cell_source_fidelity(self, facts, fine, base):
        dim = "cell_source_fidelity"
        if facts["source"] == "spreadsheet_v1_contract":
            regions = [
                r for r in facts["table_regions"] if r["expected_cell_ids"]
            ]
            if not regions:
                return 0.0, False, "no_expected_cells", []
            scores = []
            issues = []
            for region in regions:
                expected = set(region["expected_cell_ids"])
                fine_ids = []
                for u in self._units_for_region(fine, region["id"]):
                    fine_ids.extend(self._meta(u, "cell_ids") or [])
                base_ids = []
                for u in self._units_for_region(base, region["id"]):
                    base_ids.extend(self._meta(u, "cell_ids") or [])
                # coverage uses the union (traceable at either granularity counts as covered);
                # duplicates counted only within a granularity (fine/base dual-granularity references to the same cell are by design, not duplicates)
                actual = set(fine_ids) | set(base_ids)
                hits = len(actual & expected)
                dups = (
                    len(fine_ids) - len(set(fine_ids))
                    + len(base_ids) - len(set(base_ids))
                )
                score = max(0.0, (hits - dups) / len(expected))
                scores.append(score)
                if score < 0.999:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"region {region['id']}: {len(expected) - hits} "
                        f"untraceable / {dups} duplicated cells",
                    ))
            return sum(scores) / len(scores), True, "", issues

        # fallback: cell_id uniqueness across units carrying cell evidence
        keys = []
        for unit in list(fine) + list(base):
            for cell_id in self._meta(unit, "cell_ids") or []:
                keys.append((self._meta(unit, "region_id"), cell_id))
        if not keys:
            return 0.0, False, "no_cell_ids_in_units", []
        return len(set(keys)) / len(keys), True, "", []

    # ==================================
    # dimension: region_locality (SECONDARY)
    # ==================================
    #
    # Structure checks over the multi-granularity scaffolding:
    # base units carry the locality_scope matching their
    # region_type ("table"→table, otherwise region), and every
    # base unit belongs to exactly one coarse sheet context
    # (locality_scope="sheet") via base_unit_ids.
    # ==================================

    def _region_locality(self, facts, base, coarse):
        dim = "region_locality"
        if not base or not coarse:
            return 0.0, False, "no_multi_granularity_context", []

        passed = 0
        total = 0
        issues = []

        coarse_lists = []
        for sheet_unit in coarse:
            ids = self._meta(sheet_unit, "base_unit_ids") or []
            coarse_lists.append({
                "scope": self._locality_scope(sheet_unit),
                "base_unit_ids": set(ids),
                "heading": getattr(sheet_unit, "heading", None),
            })

        for unit in base:
            region_type = self._meta(unit, "region_type")
            expected_scope = (
                "table" if region_type == "table" else "region"
            )
            total += 1
            if self._locality_scope(unit) == expected_scope:
                passed += 1
            else:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"unit {self._identity(unit)}: locality_scope "
                    f"'{self._locality_scope(unit)}' does not match "
                    f"region_type '{region_type}'",
                ))

            identity = self._identity(unit)
            if identity is not None:
                total += 1
                owners = [
                    c for c in coarse_lists if identity in c["base_unit_ids"]
                ]
                if len(owners) == 1:
                    passed += 1
                else:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"unit {identity}: listed in {len(owners)} "
                        f"sheet contexts (expected exactly 1)",
                    ))

        for sheet_unit in coarse_lists:
            total += 1
            if sheet_unit["scope"] == "sheet":
                passed += 1
            else:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"sheet context {sheet_unit['heading']}: locality_scope "
                    f"'{sheet_unit['scope']}' is not 'sheet'",
                ))

        if total == 0:
            return 0.0, False, "no_locality_checks_possible", []
        return passed / total, True, "", issues

    # ==================================
    # dimension: formula_relation_integrity (SECONDARY)
    # ==================================
    #
    # Formula cells must survive rendering: the unit that owns
    # the cell must carry the "[formula: ...]" marker. Cross-
    # sheet relations must be resolvable — every target sheet
    # has a coarse context unit.
    # ==================================

    def _formula_relation_integrity(self, facts, fine, base, coarse):
        dim = "formula_relation_integrity"
        if facts["source"] != "spreadsheet_v1_contract":
            return 0.0, False, "no_contract_facts", []

        formula_cells = facts["formula_cells"]
        relations = facts["relations"]
        if not formula_cells and not relations:
            return 0.0, False, "no_formulas_or_relations", []

        issues = []
        units = list(fine) + list(base)

        # --- formula preservation in rendered text ---
        formula_score = None
        if formula_cells:
            preserved = 0
            for entry in formula_cells:
                marker = f"[formula: {entry['formula']}]"
                owners = [
                    u for u in units
                    if entry["cell_id"] in (self._meta(u, "cell_ids") or [])
                ]
                if any(marker in (getattr(u, "text", "") or "") for u in owners):
                    preserved += 1
                else:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"formula lost in rendering: cell "
                        f"{entry['cell_id']} ({entry['formula']})",
                    ))
            formula_score = preserved / len(formula_cells)

        # --- relation target resolvability ---
        relation_score = None
        if relations:
            sheet_names = set()
            for sheet_unit in coarse:
                name = getattr(sheet_unit, "heading", None)
                if name is None:
                    name = self._meta(sheet_unit, "sheet_id")
                if name is not None:
                    sheet_names.add(str(name))
            resolved = 0
            for relation in relations:
                targets = relation.get("targets") or []
                if targets and all(
                    str(t.get("sheet")) in sheet_names for t in targets
                ):
                    resolved += 1
                else:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"relation {relation.get('id')}: target sheet "
                        f"missing from sheet contexts",
                    ))
            relation_score = resolved / len(relations)

        if formula_score is not None and relation_score is not None:
            score = (
                _FORMULA_PRESERVATION_WEIGHT * formula_score
                + _RELATION_RESOLVABILITY_WEIGHT * relation_score
            )
        elif formula_score is not None:
            score = formula_score
        else:
            score = relation_score
        return score, True, "", issues

    # ==================================
    # invariant track (dual-track, roadmap item 9)
    # ==================================

    def _attach_invariant_track(self, result, fine, base, coarse, document):
        """Attach the parallel invariant track (roadmap item 9).

        Dual-track positioning: the canonical
        3-dimension comprehensive profile/overall above
        (table_quality_v2.0, three-question taxonomy) is the
        PUBLIC track; the 3 table invariants (table_invariants.py)
        are a PARALLEL additive view exported as ``invariants`` /
        ``invariant_composite`` — they read subcheck scores via
        the profile→observations fallback, so the invariant track
        is untouched by the v2.0 taxonomy refactor. Since
        table_quality_v1.1 the dimension logic is real (skeleton
        mode retired), so the invariant track reports un-capped
        confidence. The quality result is pre-seeded into the
        invariant context so composition causes zero
        recomputation. If invariant evaluation fails for any
        reason, we silently degrade to invariant-free output.
        """
        try:
            from provendoc.intelligence.rse.table_invariants import (
                create_table_registry,
            )
            registry = create_table_registry()
            context = {"evaluation_result::table": result}
            invariant_results = registry.evaluate_all(
                [u for u in list(fine) + list(base) if u is not None],
                document,
                None,
                context=context,
            )
            result["invariants"] = {
                name: r.to_dict()
                for name, r in invariant_results.items()
            }
            result["invariant_composite"] = registry.composite_score(
                invariant_results, weighted=True,
            )
            result["invariant_track"] = "table_invariants_v1"
        # degrade to no invariant track when the invariant registry is not ready (enhancement observation, not a promise).
        except Exception:
            pass
        return result
