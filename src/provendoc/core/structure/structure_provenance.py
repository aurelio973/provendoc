"""Document-level structure provenance aggregation (w1-b/w1-c).

Structure evidence comes in two tiers that must not flow into one
assessment pipeline as equals:

- ``declared``: structure the author explicitly marked — markdown ``#``
  markers, DOCX heading styles / outline levels, PDF bookmark navigation.
  The text or package itself carries the structural marker; trustworthy.
- ``inferred``: structure the parser guessed — Chinese numbering
  heuristics (``一、`` / ``（一）`` / ``第X章``), dotted numbers, all-caps
  convention, setext underline convention, PDF font-size heuristics, MinerU
  layout semantic labels. Heading identity is an inference result and can
  be wrong.

This module aggregates per-heading evidence scattered across parser
metadata into a document-level summary (counts + methods), consumed by:

- the RE envelope (``rse_report.build_re_report``) to tell consumers the
  structure-evidence makeup of this batch of chunks;
- the invariants track (``narrative_invariants``) to adjust structure-
  completeness weights and assessment confidence over a three-step
  gradient (declared structure / inferred structure / no structure).
"""

from typing import Any, Dict, Optional

from provendoc.core.structure.heading_patterns import (
    classify_heading_provenance,
)

# Values of the DOCX heading-block "source" field that count as author-
# declared (the first two tiers of docx_parser._resolve_heading_level's
# three-tier priority chain).
_DECLARED_DOCX_SOURCES = {
    "docx_outline_level",
    "docx_style",
    "docx_title_style",
}

# Values of the PDF heading "heading_level_source" field that count as
# author-declared: the bookmark tree (navigation.py) is author navigation
# embedded in the PDF package. The rest (layout_title /
# layout_title_unleveled / font-size heuristics) is machine inference —
# MinerU layout labels and font sizes are not structural markers left by
# the author.
_DECLARED_PDF_LEVEL_SOURCES = {
    "bookmark",
    "bookmark_promotion",
}


def _count_method(methods: Dict[str, int], label: str) -> None:
    methods[label] = methods.get(label, 0) + 1


def _candidate_provenance(candidate: Any) -> Optional[str]:
    """HeadingCandidate object / dict candidate → declared/inferred/None."""
    prov = getattr(candidate, "provenance", None)
    if prov is None and isinstance(candidate, dict):
        prov = candidate.get("provenance")
    if prov in ("declared", "inferred"):
        return prov
    # Legacy serialized candidates lack a provenance field — classify
    # conservatively by pattern family.
    family = getattr(candidate, "pattern", None)
    if family is None and isinstance(candidate, dict):
        family = candidate.get("pattern")
    if family:
        return classify_heading_provenance(family)
    return None


def _heading_entry_provenance(entry: Dict[str, Any]) -> Optional[str]:
    """headings-list entry (PDF heading_info / markdown-compatible fields) →
    declared/inferred/None."""
    if not isinstance(entry, dict):
        return None
    level_source = entry.get("heading_level_source")
    if level_source:
        return (
            "declared"
            if level_source in _DECLARED_PDF_LEVEL_SOURCES
            else "inferred"
        )
    source = entry.get("source")
    if source:
        return "declared" if source in _DECLARED_DOCX_SOURCES else "inferred"
    # A bare {level, title} dict with no source annotation comes from the
    # parser's explicit structure extraction (markdown ``#`` / HTML h1-h6
    # compatible fields) → declared.
    if "level" in entry or "title" in entry:
        return "declared"
    return None


# Default label for a PDF heading block whose heading_level_source is
# missing: judged by HeadingReconstructor's font-size heuristic without
# setting a source field.
_PDF_FONT_HEURISTIC_LABEL = "font_size_heuristic"


def summarize_navigation_evidence(document: Any) -> Optional[Dict[str, Any]]:
    """Bookmark-navigation coverage of heading structure (w2; evidence
    facts, not scored).

    Consumer: the RE envelope (the navigation_evidence key of
    ``rse_report.build_re_report``) tells consumers how much of this batch's
    PDF heading structure is backed by author bookmarks. Bookmarks are
    author-declared navigation embedded in the PDF package — high
    coverage makes structure_provenance.declared more trustworthy.

    Args:
        document: a Document carrying ``pdf_reconstruction.navigation`` /
            ``pdf_blocks`` metadata (non-PDF documents lack these keys →
            return None; the RE envelope then omits the section, an honest
            gap).

    Returns:
        {enabled, bookmark_count, heading_total, bookmark_backed_headings,
         heading_bookmark_coverage, matched_bookmark_count,
         bookmark_match_ratio, promoted_text_block_count,
         level_adjusted_count} or None.
    """
    metadata = getattr(document, "metadata", None) or {}
    # NavigationReconstructor.apply's info (matched_bookmark_ids /
    # enabled / promoted counts) hangs under pdf_reconstruction.navigation;
    # pdf_navigation is the raw bookmark tree (NavigationEvidence.to_dict)
    # without match statistics.
    nav = (metadata.get("pdf_reconstruction") or {}).get("navigation")
    if not isinstance(nav, dict):
        return None

    heading_blocks = [
        block
        for block in (metadata.get("pdf_blocks") or [])
        if isinstance(block, dict) and block.get("type") == "heading"
    ]
    bookmark_backed = sum(
        1
        for block in heading_blocks
        if block.get("heading_level_source") in _DECLARED_PDF_LEVEL_SOURCES
    )
    bookmark_count = int(nav.get("bookmark_count") or 0)
    matched_count = len(nav.get("matched_bookmark_ids") or [])
    heading_total = len(heading_blocks)

    return {
        "enabled": bool(nav.get("enabled")),
        "bookmark_count": bookmark_count,
        "heading_total": heading_total,
        "bookmark_backed_headings": bookmark_backed,
        "heading_bookmark_coverage": (
            round(bookmark_backed / heading_total, 3) if heading_total else None
        ),
        "matched_bookmark_count": matched_count,
        "bookmark_match_ratio": (
            round(matched_count / bookmark_count, 3) if bookmark_count else None
        ),
        "promoted_text_block_count": int(
            nav.get("promoted_text_block_count") or 0
        ),
        "level_adjusted_count": int(nav.get("level_adjusted_count") or 0),
    }


def summarize_structure_provenance(document: Any) -> Dict[str, Any]:
    """Aggregate per-heading evidence sources from document metadata.

    Priority: ``pdf_blocks`` (PDF-reconstructed heading blocks — chunk
    heading_paths are built from exactly these, each carrying
    heading_level_source) > ``heading_candidates`` (the real txt/markdown/
    epub pipeline) > ``headings`` (parser-compatible fields) >
    ``docx_blocks`` (DOCX heading blocks). When several match, only the
    highest-priority source is counted; no double counting.

    Returns:
        {"declared": int, "inferred": int, "heading_total": int,
         "inferred_ratio": Optional[float], "methods": {label: count},
         "evidence": str} — with no heading evidence at all, heading_total=0
        and inferred_ratio=None.
    """
    metadata = getattr(document, "metadata", None) or {}
    declared = 0
    inferred = 0
    methods: Dict[str, int] = {}
    evidence = "none"

    # 0) pdf_blocks — PDF-reconstructed heading blocks (w2): each carries
    # heading_level_source. Bookmarks (bookmark/bookmark_promotion) are
    # author-declared → declared; the rest (layout_title / bracket_convention
    # / font-size heuristics) is machine inference → inferred.
    # Must take priority over heading_candidates: those are re-scans of
    # sanitized text that cannot see bookmark/layout evidence, while chunk
    # heading_paths are actually built from pdf_blocks — the summary should
    # describe the real structure evidence.
    pdf_heading_blocks = [
        block
        for block in (metadata.get("pdf_blocks") or [])
        if isinstance(block, dict) and block.get("type") == "heading"
    ]
    if pdf_heading_blocks:
        evidence = "pdf_blocks"
        for block in pdf_heading_blocks:
            source = block.get("heading_level_source")
            prov: Optional[str] = (
                "declared"
                if source in _DECLARED_PDF_LEVEL_SOURCES
                else "inferred"
            )
            label = f"pdf:{source or _PDF_FONT_HEURISTIC_LABEL}"
            if prov == "declared":
                declared += 1
            else:
                inferred += 1
            _count_method(methods, label)

    # 1) heading_candidates — real pipeline output of the core.structure
    # shared extractor
    candidates = metadata.get("heading_candidates") or []
    if not pdf_heading_blocks and candidates:
        evidence = "heading_candidates"
        for cand in candidates:
            prov = _candidate_provenance(cand)
            family = getattr(cand, "pattern", None)
            if family is None and isinstance(cand, dict):
                family = cand.get("pattern") or "dict"
            label = f"pattern:{family or 'unknown'}"
            if prov == "declared":
                declared += 1
            elif prov == "inferred":
                inferred += 1
            _count_method(methods, label)

    # 2) headings — PDF heading_info / markdown-compatible fields
    if not pdf_heading_blocks and not candidates:
        headings = metadata.get("headings") or []
        if headings:
            evidence = "headings"
            for entry in headings:
                prov = _heading_entry_provenance(entry)
                label = (
                    f"pdf:{entry.get('heading_level_source')}"
                    if isinstance(entry, dict)
                    and entry.get("heading_level_source")
                    else "headings_list"
                )
                if prov == "declared":
                    declared += 1
                elif prov == "inferred":
                    inferred += 1
                _count_method(methods, label)

    # 3) docx_blocks — DOCX heading blocks (source-field priority chain)
    if (
        not pdf_heading_blocks
        and not candidates
        and not (metadata.get("headings") or [])
    ):
        docx_blocks = metadata.get("docx_blocks") or []
        heading_blocks = [
            b for b in docx_blocks
            if isinstance(b, dict) and b.get("type") == "heading"
        ]
        if heading_blocks:
            evidence = "docx_blocks"
            for block in heading_blocks:
                source = block.get("source") or "unspecified"
                prov = (
                    "declared"
                    if source in _DECLARED_DOCX_SOURCES
                    else "inferred"
                )
                if prov == "declared":
                    declared += 1
                else:
                    inferred += 1
                _count_method(methods, f"docx:{source}")

    total = declared + inferred
    return {
        "declared": declared,
        "inferred": inferred,
        "heading_total": total,
        "inferred_ratio": round(inferred / total, 3) if total else None,
        "methods": methods,
        "evidence": evidence,
    }
