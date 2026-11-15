"""Structural-integrity dimension (PRIMARY): physical integrity of code fences/tables/orphan headings."""

from provendoc.intelligence.rse import narrative_checks


# ==================================
# dimension: structural integrity (PRIMARY)
# ==================================
#
# Academic basis:
#   Lewis et al. (2020) RAG — chunk integrity affects
#   retrieval precision. A chunk with a broken code
#   fence or fragmented table is structurally invalid.
#
# Checks:
#   - code fence completeness (paired open/close)
#   - table row fragmentation across chunks
#   - orphan headings (heading without content)
#
# This dimension checks PHYSICAL structure integrity.
# It does NOT check semantic completeness — that is
# the job of semantic_cohesion.
# ==================================
def structural_integrity(chunks, doc_headings=None,
                          doc_text="", structure_provenance=None):
    """Check physical structure integrity of chunks.

    v3 calibration fix:
        - Use document-level heading metadata (doc_headings)
          to detect structural organization, not just chunk text.
          The chunker may strip headings from chunk text, so
          checking only chunk text would miss good documents'
          heading structure.
        - For long documents without ANY structural markers
          (headings, code fences, tables) in both document text
          and chunk text, apply progressive penalty:
          * >500 chars, no structure → ×0.5 (was 0.7)
          * >1000 chars, no structure → ×0.3
        - For pathological documents (fragmented text, many
          single-word lines), apply content density penalty.

    w1-c structure-evidence tiers (a three-step gradient):
        Structural evidence is not all equal — author-declared structure (markdown ``#`` /
        DOCX styles / PDF bookmarks) and parser-guessed structure (numbering heuristics /
        all-caps conventions / font inference) must be scored in tiers:

        1. declared structure / explicit markers (fence/table): full-score path, no penalty;
        2. guessed structure (headings exist but all inferred, with no in-text explicit
           marker corroboration such as fence/table): mid-tier penalty — long documents
           (>1000 → ×0.85, >500 → ×0.92);
        3. no structure: the original progressive penalty (prose ×0.7/×0.8, purely unstructured
           ×0.3/×0.5).

        Mixed evidence (declared ≥ 1) goes to tier 1 — the presence of declared structure
        is taken as the author having organized the document, conservatively no penalty; the guessed share is left to
        the confidence structure_evidence factor to reflect separately.
    """
    if not chunks:
        return 0.0

    score = 1.0
    violations = 0.0
    has_any_structure = False
    # In-text explicit structural markers directly verifiable (code fences/table pipes):
    # independent of heading evidence — a fence is a real marker the author left behind; even if headings
    # are all guessed, the document is not pushed into the mid tier.
    has_explicit_markers = False

    # check document-level structural signals
    if doc_headings and len(doc_headings) > 0:
        has_any_structure = True
    # Third source of heading evidence: the structure_provenance summary (txt's
    # heading_candidates does not enter the 'headings' compatibility key — see
    # the priority chain of summarize_structure_provenance).
    if structure_provenance and structure_provenance.get(
        "heading_total", 0
    ) > 0:
        has_any_structure = True
    if doc_text:
        if any(f in doc_text for f in narrative_checks.FENCE_MARKS):
            has_any_structure = True
            has_explicit_markers = True
        # check for table markers in document text
        if doc_text.count("|") >= 4:
            has_any_structure = True
            has_explicit_markers = True

    for i, chunk in enumerate(chunks):
        text = chunk.text
        block_type = chunk.unit_type.value

        # code fence completeness
        if block_type == "code":
            fence_count = 0
            for fence in narrative_checks.FENCE_MARKS:
                fence_count += text.count(fence)
            if fence_count % 2 != 0:
                violations += 1.0

        # table fragmentation (text chunks)
        if block_type == "text":
            lines = text.split("\n")
            table_lines = [
                line for line in lines
                if "|" in line.strip()
                and len(line.strip()) > 3
            ]

            if i > 0 and table_lines:
                first_tl = table_lines[0].strip()
                if (not first_tl.startswith("|")
                        and first_tl.count("|") >= 2):
                    violations += 0.3

            if i < len(chunks) - 1 and table_lines:
                last_tl = table_lines[-1].strip()
                if (last_tl.count("|") >= 2
                        and not last_tl.endswith("|")):
                    violations += 0.3

        # orphan heading
        if i < len(chunks) - 1:
            stripped = text.strip()
            if len(stripped) < 50:
                is_heading = any(
                    p.match(stripped)
                    for p in narrative_checks.HEADING_PATTERNS
                )
                if is_heading:
                    violations += 0.2

        # also check chunk-level structural elements
        # (for docs where headings ARE in chunk text)
        if any(p.match(text.strip())
               for p in narrative_checks.HEADING_PATTERNS):
            has_any_structure = True
        if any(f in text for f in narrative_checks.FENCE_MARKS):
            has_any_structure = True
            has_explicit_markers = True
        if "|" in text and text.strip().count("|") >= 2:
            has_any_structure = True
            has_explicit_markers = True

    score -= violations / max(len(chunks), 1)

    # structure absence penalty: progressive based on document size
    # v3: check if document has meaningful paragraph structure
    # (paragraphs with avg length >50 chars = prose document
    # without markup, which is still structured)
    # v4: single-paragraph clean prose (short news: 1-3 non-blank
    # lines, avg line length >= 50) also counts as prose structure
    # (see inline v4 calibration note below)
    total_text = sum(len(c.text) for c in chunks)
    if not has_any_structure:
        # check if document has prose paragraph structure
        has_prose_structure = False
        if doc_text:
            raw_paras = [
                p.strip() for p in doc_text.split("\n\n")
                if p.strip()
            ]
            if len(raw_paras) >= 3:
                avg_para_len = sum(
                    len(p) for p in raw_paras
                ) / len(raw_paras)
                if avg_para_len >= 50:
                    has_prose_structure = True
            # v4 calibration (rse_audit_25): clean prose with one/few paragraphs is not
            # pathological fragmentation — short news typically is one or two long paragraphs over the whole text, no blank-line
            # separation (thucnews: a single 1793-character prose line fell into the ×0.3 heavy-penalty tier). Continuous prose
            # with ≤3 non-empty lines and average line length ≥50 goes to the prose light-penalty tier;
            # the ×0.3 heavy-penalty tier is reserved for genuinely fragmented text with many short lines (indented tables/stacked short sentences).
            if not has_prose_structure:
                nonblank = [
                    ln for ln in doc_text.splitlines() if ln.strip()
                ]
                if (
                    0 < len(nonblank) <= 3
                    and len(doc_text.strip()) / len(nonblank) >= 50
                ):
                    has_prose_structure = True

        if has_prose_structure:
            # prose without markup — mild penalty only
            if total_text > 1000:
                score *= 0.7
            elif total_text > 500:
                score *= 0.8
        else:
            # truly unstructured — heavy penalty
            if total_text > 1000:
                score *= 0.3
            elif total_text > 500:
                score *= 0.5
    elif (
        structure_provenance
        and structure_provenance.get("heading_total", 0) > 0
        and not structure_provenance.get("declared")
        and not has_explicit_markers
    ):
        # w1-c mid tier (guessed structure): has_any_structure is True only because
        # of parser-guessed headings — no author-declared structure at all, nor in-text explicit
        # markers such as fence/table. Between declared structure (no penalty) and no structure
        # (×0.3-×0.8); the penalty coefficient is tunable on the calibration corpus.
        if total_text > 1000:
            score *= 0.85
        elif total_text > 500:
            score *= 0.92

    return round(max(score, 0.0), 3)
