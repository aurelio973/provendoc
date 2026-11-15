# -*- coding: utf-8 -*-
"""Context Self-Containment — candidate-dimension invariant of RE migration step 3.

Contract source: re_migration_plan.md §5 step 3 / §4 constraint 1.

Semantic question:
    "Taken out alone (detached from the original document and from neighboring chunks), can it be understood?"

    In retrieval each chunk is embedded and recalled independently — the reader receives
    the chunk itself, not the whole document. Heading context, table headers, or code
    explanations left in other chunks make the chunk unreadable once retrieved.

Testable proxies (v1, structural-evidence signals only):
    A. heading_context_coverage   whether the chunk carries usable heading context
                                  (metadata heading_path / in-text heading line /
                                  injected context prefix line) — porting the
                                  failure_modes.SectionContextLossDetector
                                  detection semantics into a coverage rate
    B. context_mode_execution     when the strategy declares context injection (mode != none),
                                  is the promise materialized onto chunks (execution consistency)
    C. structural_self_containment whether table chunks carry their own header rows (orphan row
                                  proxy), and whether isolated heading chunks carry the
                                  parent path (orphan heading proxy)

Explicitly excluded (kept in Diagnostics, not scored):
    - ambiguous_reference (dangling "如下"/"上述"/"参见") — without coreference
      resolution it is a noise source; counted and reported only, revisit after measuring the false-positive rate (migration doc §5 step 3);
    - cross-chunk breaks such as code-explanation binding or list splitting — owned by failure_modes
      / the Gate layer, not double-scored here (§1 boundary statement: no double scoring).

Standalone-invariant positioning (important):
    This class is **NOT registered** into create_narrative_registry() — registration
    would change invariant_composite, violating the freeze constraint that "legacy
    scores for steps 0-3 must not move by a single digit" (golden snapshot scope).
    It is not made a QualityBackedInvariant either (no legacy dimension to back
    it; implementation note in migration doc §5 step 3). It currently reports
    via re.candidate_profile as a candidate dimension, self-reporting confidence;
    step 4 profile reassembly decides, by admission-test results, whether it joins the Quality Profile or retreats to Diagnostics.

Robustness contract:
    - Chunk fields are accessed only via getattr soft access: real RepresentationUnits (with
      heading_path metadata) and bare .text objects (archived-corpus retests) are both evaluable;
      missing metadata automatically degrades to pure-text signals with lower confidence;
    - Evaluation failures are absorbed by the caller (try/except); this module must not
      become a new runtime single point of failure (same discipline as the narrative_quality invariant track).
"""

import re
from typing import Dict, List, Optional

from provendoc.intelligence.semantic.invariant import (
    InvariantResult,
    SemanticInvariant,
)

CONTEXT_SELF_CONTAINMENT_VERSION = "context_self_containment_v1"

# ---- Text-signal primitives (structural evidence, not heuristic guesses) ----

_HEADING_LINE = re.compile(r"^\s{0,3}(#{1,6})\s+\S")
"""markdown heading 行（与 failure_modes.SemanticDriftDetector 同刻度）。"""

_CONTEXT_PREFIX = re.compile(
    r"^\s*\[(?:context|上下文|语境)\s*[:：][^\]]{0,120}\]",
    re.IGNORECASE,
)
"""注入的上下文前缀行，如 D_section_context 的 "[Context: 父 > 标题]"。"""

_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:\-|]*-{2,}[\s:\-|]*\|?\s*$")
"""markdown 表格分隔行（|---|---| 形态）。"""

# ---- Diagnostics-only: dangling-reference markers (counted only, not scored) ----

_AMBIGUOUS_REF_MARKERS = (
    "如下", "上述", "如上", "参见", "详见", "上文", "前文",
    "as follows", "see above", "the above", "aforementioned",
)


def _nonempty_lines(text: str) -> List[str]:
    return [ln for ln in (text or "").split("\n") if ln.strip()]


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 and len(stripped) > 3


def _metadata_heading_path(chunk) -> List[str]:
    """Metadata heading path (a real RepresentationUnit field; bare objects → [])."""
    path = getattr(chunk, "heading_path", None)
    if path:
        return list(path)
    primitives = getattr(chunk, "semantic_primitives", None)
    if primitives is not None:
        hierarchy = getattr(primitives, "hierarchy_path", None)
        if hierarchy:
            return list(hierarchy)
    return []


def _context_evidence(chunk) -> Dict[str, bool]:
    """A chunk's three kinds of context evidence (any one suffices to understand its section standalone)."""
    text = getattr(chunk, "text", "") or ""
    lines = _nonempty_lines(text)

    heading_line = bool(lines) and bool(_HEADING_LINE.match(lines[0]))
    context_prefix = any(
        _CONTEXT_PREFIX.match(ln) for ln in lines[:2]
    )
    metadata_path = bool(_metadata_heading_path(chunk))
    heading_meta = bool(getattr(chunk, "heading", ""))

    return {
        "heading_line": heading_line,
        "context_prefix": context_prefix,
        "metadata_path": metadata_path,
        "heading_meta": heading_meta,
    }


class ContextSelfContainment(SemanticInvariant):
    """Whether a chunk is independently understandable when detached from the original document (candidate dimension, standalone invariant).

    See the module docstring for signal design and boundary statements. Sub-signals
    only explain the parent score (diagnostics); they do not vote independently or double-score with the parent.
    """

    #: A / B / C sub-signal weights (B is dropped and weights re-normalized when mode is none / no strategy)
    _W_HEADING = 0.4
    _W_MODE = 0.3
    _W_STRUCTURAL = 0.3

    @property
    def name(self) -> str:
        """Invariant name."""
        return "context_self_containment"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Each chunk can be understood standalone: it carries "
            "heading context (metadata, heading line, or injected "
            "context prefix), bound table headers, and no orphan "
            "structural fragments."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Lewis et al. (2020) RAG — chunks are retrieved "
            "independently; context lost at chunk time is context "
            "lost at answer time. Chunk-level self-containment is a "
            "functional requirement of retrieval, not an aesthetic "
            "preference."
        )

    @property
    def weight(self) -> float:
        """Nominal mounting weight (the standalone track does not join the composite yet)."""
        # Nominal weight: the mounting weight once step-4 admission passes and it enters the Quality Profile;
        # the standalone track currently joins no composite.
        return 0.2

    # ==================================
    # applicability
    # ==================================

    def applicable(
        self, chunks, document=None, strategy=None, **kwargs
    ) -> tuple:
        """Decide whether the invariant applies (returns the reason if not)."""
        if not chunks:
            return False, "no_chunks"

        # Dimension premise: the document has heading structure (or the strategy injects context).
        # Flat prose without hierarchy → nothing to lose → not applicable (N/A, not a zero score).
        has_structure = any(
            any(_context_evidence(c).values()) for c in chunks
        )
        if not has_structure:
            doc_text = getattr(document, "text", "") if document else ""
            if doc_text and any(
                _HEADING_LINE.match(ln)
                for ln in _nonempty_lines(doc_text)
            ):
                # The document has headings but no chunk carries any — exactly the
                # pathological shape where all context is lost; it must be evaluated (no N/A escape)
                return True, ""
            mode = self._context_mode(strategy)
            if mode not in (None, "none"):
                return True, ""
            return False, "no_heading_structure"

        return True, ""

    # ==================================
    # evaluate
    # ==================================

    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Evaluate the invariant and return the result."""
        n = len(chunks)
        mode = self._context_mode(strategy)

        # ---- Signal A: heading-context coverage rate ----
        evidence_flags = [_context_evidence(c) for c in chunks]
        evident = [any(e.values()) for e in evidence_flags]
        rate_a = sum(1 for e in evident if e) / n

        # ---- Signal B: context-mode execution consistency ----
        # mode none (strategy explicitly injects nothing) → drop this signal and re-normalize;
        # no strategy object (archived retests etc.) → likewise dropped, honest down-weighting.
        if mode in (None, "none"):
            rate_b = None
        else:
            rate_b = rate_a  # 同一证据口径：承诺的上下文是否物化

        # ---- Signal C: structural self-containment (orphan row / orphan heading proxies) ----
        table_fragments: List[int] = []
        orphan_headings: List[int] = []
        structural_pass = []
        for i, chunk in enumerate(chunks):
            ok, reason = self._structural_check(
                chunk, evidence_flags[i],
            )
            structural_pass.append(ok)
            if reason == "table_fragment":
                table_fragments.append(i)
            elif reason == "orphan_heading":
                orphan_headings.append(i)
        rate_c = sum(1 for p in structural_pass if p) / n

        # ---- Composition (re-normalized after dropping B) ----
        parts = [(rate_a, self._W_HEADING), (rate_c, self._W_STRUCTURAL)]
        if rate_b is not None:
            parts.append((rate_b, self._W_MODE))
        total_w = sum(w for _, w in parts)
        score = round(
            sum(r * w for r, w in parts) / max(total_w, 1e-9), 3
        )

        # ---- diagnostics-only: dangling-reference markers (count without penalty) ----
        ambiguous = self._count_ambiguous_references(chunks)

        metadata_available = any(e["metadata_path"] for e in evidence_flags)

        diagnostics = {
            "version": CONTEXT_SELF_CONTAINMENT_VERSION,
            "chunk_count": n,
            "heading_context_coverage": round(rate_a, 3),
            "context_evident_chunks": sum(1 for e in evident if e),
            "context_mode": mode,
            "context_mode_execution": (
                round(rate_b, 3) if rate_b is not None else None
            ),
            "structural_self_containment": round(rate_c, 3),
            "table_fragment_chunks": table_fragments,
            "orphan_heading_chunks": orphan_headings,
            "metadata_heading_path_available": metadata_available,
            # Diagnostics-only signal (not scored):
            "ambiguous_reference_markers": ambiguous["count"],
            "ambiguous_reference_note": (
                "counted only, not scored — noise rate unmeasured, "
                "kept in Diagnostics per migration plan §5 step 3"
            ),
        }

        issues = []
        if rate_a < 0.5:
            issues.append(
                f"only {round(rate_a * 100)}% of chunks carry "
                "heading context — most chunks are unreadable "
                "standalone"
            )
        if rate_b is not None and rate_b < 0.5:
            issues.append(
                f"context mode '{mode}' declared but materialized on "
                f"only {round(rate_b * 100)}% of chunks"
            )
        if table_fragments:
            issues.append(
                f"{len(table_fragments)} table chunk(s) lack in-chunk "
                "header rows (orphan table rows)"
            )
        if orphan_headings:
            issues.append(
                f"{len(orphan_headings)} orphan heading chunk(s) "
                "without parent context"
            )

        # ---- confidence: self-reported evidence density ----
        confidence = self._confidence(
            n=n, metadata_available=metadata_available,
            evidence_flags=evidence_flags,
        )

        return InvariantResult(
            name=self.name,
            score=score,
            diagnostics=diagnostics,
            issues=issues,
            confidence=confidence,
        )

    # ==================================
    # Internals: sub-signal implementations
    # ==================================

    @staticmethod
    def _context_mode(strategy) -> Optional[str]:
        if strategy is None:
            return None
        cp = getattr(strategy, "context_propagation", None)
        if cp is None:
            # Compatible with strategy objects that carry mode directly (tests / archived rebuilds)
            return getattr(strategy, "context_mode", None)
        return getattr(cp, "mode", None)

    def _structural_check(self, chunk, evidence) -> "tuple[bool, Optional[str]]":
        """Structural self-containment verdict for a single chunk.

        Returns:
            (ok, reason): reason is None when ok=True;
            reason ∈ {"table_fragment", "orphan_heading"}.
        """
        text = getattr(chunk, "text", "") or ""
        lines = _nonempty_lines(text)

        # Table chunk: has table rows but no separator row (the header lives in another chunk)
        table_rows = [ln for ln in lines if _is_table_row(ln)]
        if table_rows:
            has_separator = any(
                _TABLE_SEPARATOR.match(ln) and "-" in ln for ln in lines
            )
            if not has_separator:
                return False, "table_fragment"
            return True, None

        # Isolated heading chunk: a single heading line, path without a parent (contains only itself),
        # no injected context → cannot disambiguate "what this is" at retrieval time
        if len(lines) == 1 and _HEADING_LINE.match(lines[0]):
            if (
                len(_metadata_heading_path(chunk)) < 2
                and not evidence["context_prefix"]
            ):
                return False, "orphan_heading"
            return True, None

        return True, None

    @staticmethod
    def _count_ambiguous_references(chunks) -> Dict[str, int]:
        """Count dangling-reference markers (Diagnostics-only, not scored)."""
        count = 0
        for chunk in chunks:
            text = getattr(chunk, "text", "") or ""
            head = text[:120]
            count += sum(
                1 for m in _AMBIGUOUS_REF_MARKERS if m in head
            )
        return {"count": count}

    @staticmethod
    def _confidence(n, metadata_available, evidence_flags) -> float:
        if metadata_available and n >= 3:
            return 0.9
        if n < 3:
            return 0.6
        # Pure-text signals (archived retests / missing metadata): lower tier
        prefix_only = any(
            e["context_prefix"]
            and not (e["heading_line"] or e["metadata_path"])
            for e in evidence_flags
        )
        return 0.7 if prefix_only else 0.75


# ==================================
# Convenience entry point (for provendoc_run wiring / archived-corpus retests)
# ==================================

def evaluate_context_self_containment(
    representation,
    document=None,
    strategy=None,
) -> InvariantResult:
    """Extract chunks from the representation and evaluate context self-containment.

    Compatible with MultiGranularityOutput and flat RepresentationUnit lists
    (same extraction basis as rse_report._extract_chunks). The caller owns the try/except
    safety net — this function does not swallow exceptions; evaluation failures must stay visible (honesty over silence).
    """
    if representation is None:
        raise ValueError("representation is None")

    content = getattr(representation, "content", None)
    if content is None:
        raise ValueError("representation has no content")

    if hasattr(content, "base"):
        chunks = content.base
    elif hasattr(content, "all_units"):
        chunks = content.all_units()
    elif isinstance(content, list):
        chunks = content
    else:
        raise ValueError(
            f"unsupported representation content type: "
            f"{type(content).__name__}"
        )

    if not chunks:
        raise ValueError("representation content is empty")

    invariant = ContextSelfContainment()
    applicable, reason = invariant.applicable(chunks, document, strategy)
    if not applicable:
        return invariant._not_applicable_result(reason)
    return invariant.evaluate(chunks, document, strategy)
