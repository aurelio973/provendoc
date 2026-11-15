"""Code Chunk Quality Index — structural evaluation for code units.

CodeQuality evaluates whether AST-derived representation units preserve parse-stage
source structure.  It is deliberately not a compiler or a static analyser:
syntax recovery is recorded as evidence and dynamic name resolution is never
invented.  Its facts come from the code parser contract and the generated
``CodeRepresentation`` units.

code_quality_v3 (dimension_taxonomy_refactor_prereg_v1, 2026-10-26):
scoring dimensions are two-question consolidated dims — dimensions are orthogonal questions, not duplicate
declarations of parsing facts (full-corpus measurement: the function/class/import three dims
are purely binary with pairwise r=+1.000; five dims = five declarations of one latent variable):

    - unit_coherence      — units unbroken: structural units such as function/class bodies
                            land in exactly one chunk (subchecks
                            function_completeness 0.5 +
                            class_boundary_correctness 0.5）
    - relation_continuity — relations unbroken: references resolvable, imports
                            preserved (subchecks symbol_continuity 0.5 +
                            import_dependency_preservation 0.5）

provenance_fidelity (content neither lost nor hallucinated) is structurally satisfied: chunks =
verbatim source spans — no separate dim; recorded as a declaration (prereg §2.3).

Evidence layer stays out of scoring: ast_integrity (parse-status ladder, r=+0.85 drags
all chunk dims) is demoted to observations — degraded→error issues stay
in the gate, the confidence parse_reliability decomposition is unchanged, the invariant
track keeps fallback reads via profile→observations (zero drift). All five
subchecks' raw scores are exposed under original names in observations.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# ==================================
# ast_integrity within-dimension ladder parameters (E3 named-constant registry, zero change in values)
#
# Reconciliation lock: tests/test_scoring_sensitivity.py contract B —
# recovered-ladder score = _AST_STATUS_BASELINE["recovered"]
# - min(_AST_RECOVERY_PENALTY_CAP,
#       _AST_RECOVERY_PENALTY_PER_DIAGNOSTIC * n)
# changing the values turns tests red, requiring a prereg version bump
# (dead_parameter_cleanup_prereg_v1 §2.2).
# History: the original implementation had a max(0.35, …) floor; E2 contract C algebraically proved it
# unreachable (ladder minimum 0.95-0.45=0.50 > 0.35), removed in E3.
# ==================================

_AST_STATUS_BASELINE: Dict[str, float] = {
    "ok": 1.0,
    "recovered": 0.95,
    "degraded": 0.15,
    "empty": 0.0,
}

#: deterministic per-syntax-diagnostic reduction (recovered status)
_AST_RECOVERY_PENALTY_PER_DIAGNOSTIC = 0.05

#: reduction cap (avoid zeroing a single damaged file)
_AST_RECOVERY_PENALTY_CAP = 0.45

# ==================================
# two-question consolidated dimension weights (v3 taxonomy refactor, decision date 2026-10-26)
#
# user challenged "count functions as one, classes as another, each telling its own story" → full-corpus measurement
# (four_domain_quality_v1, 427 files): function_completeness /
# class_boundary_correctness / import_dependency_preservation
# three dims purely binary with pairwise r=+1.000, symbol_continuity r=+0.990 —
# five dims are five declarations of one latent variable (block-structure survival). The scoring layer converges
# to two-question consolidated dims; subcheck computation logic untouched, raw scores go to
# observations for invariant-track fallback reads (zero drift); ast_integrity
# demoted to observations on evidence-layer grounds.
# behavioral surface: the active_weight_normalization policy is unchanged (dim count 5→2,
# 0.5 each); uniform lock (contract E) dim count 5→2.
# prereg: dimension_taxonomy_refactor_prereg_v1.md
# ==================================


class CodeQuality:
    """Evaluate structural integrity of code-domain representation units.

    The two canonical dimensions use fixed weights.  A score describes chunk
    fidelity, not whether the source can compile or execute.  In particular,
    a recovered Tree-sitter parse remains useful but lowers AST confidence
    (reported as evidence in ``observations``, not as a scored dimension).
    """

    WEIGHTS = {
        "unit_coherence": 0.5,
        "relation_continuity": 0.5,
    }

    PRIMARY_DIMENSIONS = {
        "unit_coherence",
        "function_completeness",
        "class_boundary_correctness",
    }

    SECONDARY_DIMENSIONS = {
        "relation_continuity",
        "symbol_continuity",
        "import_dependency_preservation",
    }

    # Observation-layer dims: subcheck raw scores exposed under original names,
    # included_in_overall=False (prereg §2.4).
    OBSERVATION_DIMS = frozenset({
        "ast_integrity",
        "function_completeness",
        "class_boundary_correctness",
        "symbol_continuity",
        "import_dependency_preservation",
    })

    # Demoted dim (evidence layer out of scoring, prereg §2.3): parse-status ladder
    # — values go only to observations, issues stay in the main list (gate unchanged).
    DEMOTED_DIMS = frozenset({"ast_integrity"})

    _FUNCTION_KINDS = {"function", "method", "constructor", "procedure"}
    _CONTAINER_KINDS = {
        "class", "interface", "struct", "trait", "enum", "object",
        "table", "view", "cte",
    }
    _DEPENDENCY_KINDS = {"call", "reference", "extends", "implements", "mixin"}

    def evaluate(
        self,
        document,
        semantic_representation,
        strategy=None,
        document_quality=None,
    ):
        """Return a quality profile backed by parser and representation facts.

        ``strategy`` and ``document_quality`` are accepted for the shared quality
        evaluation interface but intentionally unused: AST/component chunking does not
        use narrative chunk-size optimisation.
        """
        code = document.metadata.get("code", {})
        status = str(code.get("status", "degraded"))
        chunks = self._as_list(semantic_representation.content)
        if not chunks:
            return self._empty_result(status, code)
        diagnostics = self._as_list(code.get("diagnostics", []))
        symbols = self._as_list(code.get("symbols", []))
        structural_units = self._as_list(code.get("structural_units", []))
        chunk_spans = self._chunk_spans(chunks)

        ast_score, ast_issues = self._ast_integrity(status, diagnostics, code)
        function_score, function_issues, function_facts = self._span_coverage_score(
            symbols,
            chunk_spans,
            self._FUNCTION_KINDS,
            "function_completeness",
            "function/method/constructor",
        )
        container_score, container_issues, container_facts = self._span_coverage_score(
            symbols,
            chunk_spans,
            self._CONTAINER_KINDS,
            "class_boundary_correctness",
            "class/interface/struct/trait/enum",
        )
        continuity_score, continuity_issues, continuity_facts = self._dependency_continuity(
            structural_units,
            chunk_spans,
            symbols,
        )
        import_score, import_issues, import_facts = self._import_preservation(
            structural_units,
            chunk_spans,
        )

        # subchecks (v2 logic as-is) → two-question consolidated dims (prereg §2.3)
        subchecks = {
            "ast_integrity": (
                ast_score, True, "parser status is available", ast_issues,
            ),
            "function_completeness": (
                function_score,
                bool(function_facts["total"]),
                "function-like symbols found"
                if function_facts["total"]
                else "no function-like symbols in source",
                function_issues,
            ),
            "class_boundary_correctness": (
                container_score,
                bool(container_facts["total"]),
                "container symbols found"
                if container_facts["total"]
                else "no class-like containers in source",
                container_issues,
            ),
            "symbol_continuity": (
                continuity_score,
                bool(continuity_facts["total"]),
                "reference relationships found"
                if continuity_facts["total"]
                else "no statically observable references in source",
                continuity_issues,
            ),
            "import_dependency_preservation": (
                import_score,
                bool(import_facts["total"]),
                "import/use/include statements found"
                if import_facts["total"]
                else "no import-like statements in source",
                import_issues,
            ),
        }

        unit_score, unit_applicable, unit_issues = self._merge_subchecks(
            "unit_coherence",
            (
                ("function_completeness", 0.5),
                ("class_boundary_correctness", 0.5),
            ),
            subchecks,
        )
        relation_score, relation_applicable, relation_issues = (
            self._merge_subchecks(
                "relation_continuity",
                (
                    ("symbol_continuity", 0.5),
                    ("import_dependency_preservation", 0.5),
                ),
                subchecks,
            )
        )

        dimensions = {
            "unit_coherence": unit_score,
            "relation_continuity": relation_score,
        }
        applicability = {
            "unit_coherence": {
                "applicable": unit_applicable,
                "reason": "function/class-like symbols found"
                if unit_applicable
                else "no function-like or class-like symbols in source",
            },
            "relation_continuity": {
                "applicable": relation_applicable,
                "reason": "reference/import relationships found"
                if relation_applicable
                else "no statically observable references or "
                "import-like statements in source",
            },
        }
        # subcheck raw scores (incl. demoted dims) → observations; demoted dims'
        # issues stay in the main list (gate inputs unchanged, prereg §2.4)
        observations = {
            name: (round(score, 3) if applicable else None)
            for name, (score, applicable, _reason, _issues) in (
                subchecks.items()
            )
        }
        issues = [
            *ast_issues,
            *unit_issues,
            *relation_issues,
        ]
        # Canonical weight policy: N/A dimensions do NOT inflate the score —
        # the overall is normalized over active weights only, so a degraded
        # parse without structural facts cannot average its way to quality.
        active_weights = [
            self.WEIGHTS[name]
            for name, info in applicability.items()
            if info["applicable"]
        ]
        overall = (
            sum(
                dimensions[name] * self.WEIGHTS[name]
                for name, info in applicability.items()
                if info["applicable"]
            )
            / sum(active_weights)
            if active_weights
            else 0.0
        )
        confidence, confidence_profile = self._confidence(
            status,
            code,
            chunk_spans,
            symbols,
            structural_units,
        )
        lengths = [len(chunk.text) for chunk in chunks]

        result = {
            # original semantics preserved: N/A dims cast bare scores (code-only exception,
            # not listed in prereg §2.5 declared drift — no extra drift surface;
            # N/A judgment follows applicability)
            "profile": {name: round(score, 3) for name, score in dimensions.items()},
            "observations": observations,
            "overall": round(overall, 3),
            "confidence": round(confidence, 3),
            "confidence_profile": confidence_profile,
            "issues": issues,
            "applicability": applicability,
            "metadata": {
                "language": code.get("language", "unknown"),
                "parse_status": status,
                "chunk_count": len(chunks),
                "avg_chunk_length": round(sum(lengths) / len(lengths), 2),
                "min_chunk_length": min(lengths),
                "max_chunk_length": max(lengths),
                "diagnostic_count": int(code.get("stats", {}).get("diagnostic_count", len(diagnostics))),
                "function_facts": function_facts,
                "container_facts": container_facts,
                "symbol_continuity_facts": continuity_facts,
                "import_facts": import_facts,
                "fact_source": "code_structure_v1",
            },
        }
        return self._attach_invariant_track(result, chunks, document)

    def _attach_invariant_track(self, result, chunks, document):
        """Attach the parallel invariant track (roadmap item 9).

        Dual-track positioning: the canonical
        2-dimension profile/overall above is the PUBLIC track; the
        3 code invariants (code_invariants.py) are a PARALLEL
        additive view exported as ``invariants`` /
        ``invariant_composite``.  The domain quality result is pre-seeded into
        the invariant context so composition causes zero
        recomputation.  If invariant evaluation fails for any
        reason, we silently degrade to invariant-free output — the
        invariant track must NEVER break the canonical evaluation
        path.
        """
        try:
            from provendoc.intelligence.rse.code_invariants import (
                create_code_registry,
            )
            registry = create_code_registry()
            context = {"evaluation_result::code": result}
            invariant_results = registry.evaluate_all(
                chunks, document, None, context=context,
            )
            result["invariants"] = {
                name: r.to_dict()
                for name, r in invariant_results.items()
            }
            result["invariant_composite"] = registry.composite_score(
                invariant_results, weighted=True,
            )
            result["invariant_track"] = "code_invariants_v1"
        # degrade to no invariant track when the invariant registry is not ready (enhancement observation, not a promise).
        except Exception:
            pass
        return result

    @staticmethod
    def _merge_subchecks(canonical, spec, subchecks):
        """Consolidated dim = weighted mean of applicable subchecks (within-dimension renormalization).

        Same semantics as BaseQuality._merge_subchecks (the code-independent architecture
        carries its own copy, the established separation of
        evaluator_semantics_governance_prereg_v1 §2): subcheck issues belong to the consolidated dim name (the dimension
        field relabeled, the original subcheck name kept in the subcheck field — invariant-track
        issue propagation matches original names with zero drift), severity preserved as-is
        — zero drift in gate inputs (prereg §2.4).
        """
        num = 0.0
        den = 0.0
        issues: List[Dict[str, Any]] = []
        for name, weight in spec:
            score, applicable, _reason, sub_issues = subchecks[name]
            if not applicable:
                continue
            num += score * weight
            den += weight
            for issue in sub_issues:
                relabeled = dict(issue)
                relabeled["dimension"] = canonical
                relabeled["subcheck"] = name
                issues.append(relabeled)
        if den <= 0:
            return 0.0, False, issues
        return num / den, True, issues

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if hasattr(value, "all_units"):
            return list(value.all_units())
        return list(value)

    @staticmethod
    def _span_of(item: Any) -> Optional[Tuple[int, int]]:
        metadata = item.metadata if hasattr(item, "metadata") else item
        if not isinstance(metadata, dict):
            return None
        try:
            start = int(metadata["byte_start"])
            end = int(metadata["byte_end"])
        except (KeyError, TypeError, ValueError):
            return None
        return (start, end) if 0 <= start < end else None

    def _chunk_spans(self, chunks: Iterable[Any]) -> List[Tuple[int, int, str]]:
        spans = []
        for index, chunk in enumerate(chunks):
            span = self._span_of(chunk)
            if span is not None:
                spans.append((span[0], span[1], str(index)))
        return spans

    @staticmethod
    def _contains(container: Tuple[int, int, str], target: Tuple[int, int]) -> bool:
        return container[0] <= target[0] and target[1] <= container[1]

    def _ast_integrity(
        self,
        status: str,
        diagnostics: List[Dict[str, Any]],
        code: Dict[str, Any],
    ) -> Tuple[float, List[Dict[str, Any]]]:
        score = _AST_STATUS_BASELINE.get(status, 0.15)
        diagnostic_count = int(code.get("stats", {}).get("diagnostic_count", len(diagnostics)))
        issues: List[Dict[str, Any]] = []

        if status == "recovered":
            # Syntax recovery is usable, but each diagnostic modestly reduces
            # certainty.  The cap avoids treating one damaged file as zero value.
            # (the 0.35 floor was a dead parameter — algebraically unreachable, removed in E3,
            # see dead_parameter_cleanup_prereg_v1 §2.1)
            score = score - min(
                _AST_RECOVERY_PENALTY_CAP,
                _AST_RECOVERY_PENALTY_PER_DIAGNOSTIC * diagnostic_count,
            )
            issues.append(
                self._issue(
                    "ast_integrity",
                    "warning",
                    f"Parser recovered from {diagnostic_count} syntax diagnostic(s).",
                )
            )
        elif status == "degraded":
            issues.append(
                self._issue(
                    "ast_integrity",
                    "error",
                    "The AST backend degraded; structural extraction is incomplete.",
                )
            )
        elif status == "empty":
            issues.append(self._issue("ast_integrity", "info", "Source file is empty."))
        return score, issues

    def _span_coverage_score(
        self,
        symbols: List[Dict[str, Any]],
        chunk_spans: List[Tuple[int, int, str]],
        kinds: set[str],
        dimension: str,
        label: str,
    ) -> Tuple[float, List[Dict[str, Any]], Dict[str, int]]:
        relevant = [symbol for symbol in symbols if symbol.get("kind") in kinds]
        total = len(relevant)
        covered = 0
        split_or_missing = 0
        issues: List[Dict[str, Any]] = []

        for symbol in relevant:
            span = self._span_of(symbol)
            if span is None:
                split_or_missing += 1
                issues.append(
                    self._issue(dimension, "warning", f"{label} {symbol.get('name', '<anonymous>')} has no valid span.")
                )
                continue
            containers = [chunk for chunk in chunk_spans if self._contains(chunk, span)]
            if len(containers) == 1:
                covered += 1
            else:
                split_or_missing += 1
                issues.append(
                    self._issue(
                        dimension,
                        "error",
                        f"{label} {symbol.get('name', '<anonymous>')} is not contained by exactly one code chunk.",
                        span,
                    )
                )

        # No symbols of this category means no boundary could have been broken.
        score = 1.0 if total == 0 else covered / total
        return score, issues, {"total": total, "covered": covered, "split_or_missing": split_or_missing}

    def _dependency_continuity(
        self,
        units: List[Dict[str, Any]],
        chunk_spans: List[Tuple[int, int, str]],
        symbols: List[Dict[str, Any]],
    ) -> Tuple[float, List[Dict[str, Any]], Dict[str, int]]:
        dependencies = [unit for unit in units if unit.get("kind") in self._DEPENDENCY_KINDS]
        names = {str(symbol.get("name")) for symbol in symbols if symbol.get("name")}
        total = len(dependencies)
        preserved = 0
        unresolved = 0
        issues: List[Dict[str, Any]] = []

        for dependency in dependencies:
            span = self._span_of(dependency)
            raw_detail = dependency.get("detail")
            detail = raw_detail if isinstance(raw_detail, dict) else {}
            target = str(detail.get("target") or dependency.get("name") or "")
            represented = span is not None and any(self._contains(chunk, span) for chunk in chunk_spans)
            # Dynamic/external targets are explicitly marked by the parser and
            # remain quality-neutral; CodeQuality never guesses their resolution.
            external_or_dynamic = bool(detail.get("external") or detail.get("dynamic"))
            resolved = external_or_dynamic or not target or target in names
            if represented and resolved:
                preserved += 1
            else:
                unresolved += 1
                reason = "is not retained in a chunk" if not represented else "has no local symbol target"
                issues.append(self._issue("symbol_continuity", "warning", f"Reference {target or '<unknown>'} {reason}.", span))

        score = 1.0 if total == 0 else preserved / total
        return score, issues, {"total": total, "preserved": preserved, "unresolved": unresolved}

    def _import_preservation(
        self,
        units: List[Dict[str, Any]],
        chunk_spans: List[Tuple[int, int, str]],
    ) -> Tuple[float, List[Dict[str, Any]], Dict[str, int]]:
        imports = [unit for unit in units if unit.get("kind") == "import"]
        total = len(imports)
        preserved = 0
        missing = 0
        issues: List[Dict[str, Any]] = []
        for imported in imports:
            span = self._span_of(imported)
            if span is not None and any(self._contains(chunk, span) for chunk in chunk_spans):
                preserved += 1
            else:
                missing += 1
                issues.append(
                    self._issue(
                        "import_dependency_preservation",
                        "error",
                        f"Import {imported.get('name', '<unknown>')} is absent from structural chunks.",
                        span,
                    )
                )
        score = 1.0 if total == 0 else preserved / total
        return score, issues, {"total": total, "preserved": preserved, "missing": missing}

    @staticmethod
    def _issue(
        dimension: str,
        severity: str,
        message: str,
        span: Optional[Tuple[int, int]] = None,
    ) -> Dict[str, Any]:
        issue: Dict[str, Any] = {"dimension": dimension, "severity": severity, "message": message}
        if span is not None:
            issue.update({"byte_start": span[0], "byte_end": span[1]})
        return issue

    @staticmethod
    def _confidence(
        status: str,
        code: Dict[str, Any],
        chunk_spans: List[Tuple[int, int, str]],
        symbols: List[Dict[str, Any]],
        structural_units: List[Dict[str, Any]],
    ) -> Tuple[float, Dict[str, float]]:
        status_confidence = {"ok": 1.0, "recovered": 0.8, "degraded": 0.2, "empty": 0.0}.get(status, 0.2)
        stats = code.get("stats", {}) if isinstance(code.get("stats"), dict) else {}
        expected = int(stats.get("symbol_count", len(symbols))) + int(
            stats.get("structural_unit_count", len(structural_units))
        )
        exported = len(symbols) + len(structural_units)
        evidence_coverage = 1.0 if expected == 0 else min(1.0, exported / expected)
        span_coverage = 0.0 if not chunk_spans else 1.0
        confidence = status_confidence * (0.7 + 0.2 * evidence_coverage + 0.1 * span_coverage)
        return confidence, {
            "parse_reliability": round(status_confidence, 3),
            "exported_evidence_coverage": round(evidence_coverage, 3),
            "chunk_span_coverage": round(span_coverage, 3),
        }

    def _empty_result(self, parse_status: str = "empty", code: Any = None) -> Dict[str, Any]:
        code = code or {}
        return {
            "profile": {dimension: 0.0 for dimension in self.WEIGHTS},
            "observations": {
                dimension: None for dimension in self.OBSERVATION_DIMS
            },
            "overall": 0.0,
            "confidence": 0.0,
            "confidence_profile": {
                "parse_reliability": 0.0,
                "exported_evidence_coverage": 0.0,
                "chunk_span_coverage": 0.0,
            },
            "issues": [{"dimension": "all", "severity": "info", "message": "no code chunks"}],
            "applicability": {
                dimension: {"applicable": False, "reason": "no code chunks"}
                for dimension in self.WEIGHTS
            },
            "metadata": {
                "language": code.get("language", "unknown"),
                "parse_status": parse_status,
                "chunk_count": 0,
                "avg_chunk_length": 0,
                "fact_source": "code_structure_v1",
            },
        }
