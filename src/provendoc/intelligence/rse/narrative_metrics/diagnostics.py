"""Shared evaluation diagnostics: per-chunk validity and execution-trace evidence.

Split out of the old narrative_cqi package (rse_naming_spec_v1 batch ③):
the six-dimension-specific confidence / generate_issues / six-dimension empty_result
were retired along with the old six-dimension pipeline; this module keeps only the
diagnostic functions still consumed by the invariant track and the optimizer.
"""

import re

# ==================================
# single-chunk suitability (legitimacy check)
# ==================================

def single_chunk_suitability(
    chunks, doc_headings, doc_text,
    total_text_length, target_size, strategy=None,
    policy_execution=None,
):
    """Judge whether producing a single chunk is a legitimate outcome.

    Markdown heading count is descriptive evidence, not a verdict. A
    compact changelog or quick-start can contain several headings while
    remaining below packaging capacity and still be one cohesive retrieval
    unit. Conversely, capacity violations and independently addressable
    structural blocks provide concrete evidence that a one-chunk outcome
    loses necessary granularity.
    """
    reasons = []

    hard_limit = target_size
    soft_limit = max(int(hard_limit * 0.4), 1)
    if (strategy is not None
            and hasattr(strategy, "get_min_chunk_size")):
        try:
            soft_limit = max(strategy.get_min_chunk_size(), 1)
        # fall back to default capacity when strategy provides no valid min_chunk_size.
        except Exception:
            pass

    # Capacity is a hard legitimacy constraint. We do not require a
    # compact document to fit the soft preference: soft capacity guides
    # normal packaging, while hard capacity defines whether a single unit
    # remains deployable at all.
    if total_text_length > hard_limit:
        reasons.append(
            f"document length {total_text_length} exceeds hard "
            f"limit {hard_limit}"
        )

    top_level_headings = sum(
        1 for line in (doc_text or "").splitlines()
        if re.match(r"#{1,2}\s", line.lstrip())
    )
    # The soft limit becomes supporting evidence only for a materially
    # oversized, multi-section document; headings alone never fail it.
    if (top_level_headings >= 2
            and total_text_length > soft_limit
            and total_text_length > int(hard_limit * 0.75)):
        reasons.append(
            f"{top_level_headings} top-level headings remain in one "
            "chunk despite near-capacity multi-section content"
        )

    has_independent_code = bool(re.search(
        r"(?m)^\s*(```|~~~)[\s\S]*?^\s*\1\s*$",
        doc_text or "",
    ))
    has_independent_table = bool(re.search(
        r"(?m)^\s*\|.+\|\s*$\n^\s*\|\s*:?-{3,}",
        doc_text or "",
    ))

    # Structural blocks are not an automatic single-chunk failure when
    # compact. They become decisive only with capacity pressure, where
    # their independent addressability leaves a real alternative split.
    structural_capacity_pressure = total_text_length > soft_limit
    if has_independent_code and structural_capacity_pressure:
        reasons.append(
            "capacity-pressured document contains an independently "
            "delimited code block"
        )
    if has_independent_table and structural_capacity_pressure:
        reasons.append(
            "capacity-pressured document contains an independently "
            "delimited table"
        )

    trace_final_count = None
    if isinstance(policy_execution, dict):
        trace_final_count = policy_execution.get("final_chunk_count")

    return {
        "suitable": not reasons,
        "reasons": reasons,
        "total_length": total_text_length,
        "soft_limit": soft_limit,
        "hard_limit": hard_limit,
        "top_level_headings": top_level_headings,
        "has_independent_code": has_independent_code,
        "has_independent_table": has_independent_table,
        "policy_trace_final_chunk_count": trace_final_count,
    }

def execution_diagnostics(
    policy_execution, representation_execution,
):
    """Expose trace-backed explanations without creating a score dimension."""
    if not isinstance(policy_execution, dict):
        return []

    diagnostics = []
    merge_trace = policy_execution.get("short_sibling_merge", {})
    merge_count = merge_trace.get("merge_count", 0)
    if merge_count:
        diagnostics.append({
            "dimension": "execution_evidence",
            "classification": "diagnostic",
            "message": (
                f"policy execution consolidated {merge_count} eligible "
                "short sibling-section pair(s) under the configured "
                "capacity and structural safeguards"
            ),
            "evidence": {
                "merge_count": merge_count,
                "short_section_limit": merge_trace.get(
                    "short_section_limit"
                ),
            },
        })

    rejection_counts = {}
    for iteration in merge_trace.get("iterations", []):
        for pair in iteration.get("candidate_pairs", []):
            for reason in pair.get("rejection_reasons", []):
                rejection_counts[reason] = (
                    rejection_counts.get(reason, 0) + 1
                )
    structural_rejections = {
        reason: count
        for reason, count in rejection_counts.items()
        if "independent_structural_block" in reason
    }
    if structural_rejections:
        diagnostics.append({
            "dimension": "execution_evidence",
            "classification": "diagnostic",
            "message": (
                "policy execution retained candidate section boundaries "
                "because independent structural blocks were detected"
            ),
            "evidence": {
                "rejections": structural_rejections,
            },
        })

    if isinstance(representation_execution, dict):
        policy_count = representation_execution.get(
            "policy_base_chunk_count"
        )
        final_count = representation_execution.get("final_base_chunk_count")
        if (policy_count is not None and final_count is not None
                and policy_count != final_count):
            diagnostics.append({
                "dimension": "execution_evidence",
                "classification": "diagnostic",
                "message": (
                    "representation-stage orphan/context processing changed "
                    "the persisted base chunk count after policy execution"
                ),
                "evidence": {
                    "policy_base_chunk_count": policy_count,
                    "final_base_chunk_count": final_count,
                },
            })

    return diagnostics


# ==================================
# empty result contract (invariant track)
# ==================================

def empty_result():
    """Empty-result contract when no chunks are evaluable (invariant-track definition)."""
    return {
        "invariant_version": "narrative_invariant_v1",
        "invariants": None,
        "invariant_composite": None,
        "issues": [
            {
                "dimension": "all",
                "message": "no chunks to evaluate",
            }
        ],
        "metadata": {
            "chunk_count": 0,
            "avg_chunk_length": 0,
            "orphan_chunks": 0,
        },
    }
