r"""Tech-standard scenario → DQR payload projection (v1.1).

v1.1 (polish r1, 2026-11-10 — real-corpus audit over the 49-file RFC
bucket): two DERIVED census facts added on top of the v1 locators —

* ``clause_hierarchy_gaps`` — clauses whose parent number is absent
  from the flush-left ledger. This quantifies the two REAL indented-
  heading families the corpus audit confirmed: centered TOP-LEVEL
  headings (the rfc791/rfc793/rfc1305 family — parents centered,
  children flush-left; e.g. rfc791 L249 "                           1.
  INTRODUCTION") and indented MID-LEVEL headings (rfc2616's 13.1);
* ``clause_sequence_anomalies`` — clause numbers breaking the
  strictly-increasing order of the document's numbering. These are
  MARKERS, not verdicts: the flagged line may be an intruding
  non-heading line (prose/equation/reference lines starting with a
  number — the rfc1305 ragged-nroff layout) OR the first REAL heading
  after an intruder (the rfc1035 "3.5" victim after the "25" port-
  table intruder).

The audit also tested and REJECTED a single-space TOC-page rule:
rfc1305's TOC mixes wide-space and single-space page numbers
("3.4.5.   Clock-Update Procedure 27"), and the single-space entries
fall into the clause ledger — but relaxing the regex would
misclassify real headings ending in bare numbers (corpus evidence:
"1.1 Changes to RFC 1541", "19.6.3 Changes from RFC 2068", "4.2.4.
Reply Code 502", "5.3.1.  Background on Priority in RFC 7540").
The verbatim ledger keeps them; the boundary is declared, and the
hierarchy-gap census records the fallout.

The tech_standard scenario chain is a LEDGER, not a repair —
like the email chain, a standards document's extraction pain is
structure location, so v1 records line-level coordinates for the
structural conventions of RFC-style standards documents and
stamps them on ``document.metadata["tech_standard_scenario"]``:

* T1 clause-numbering hierarchy — every flush-left numbered
  heading (``1.`` / ``2.1.`` / ``3.2.4.`` …, up to four segments)
  with its line number, verbatim number token, depth and bounded
  title, plus per-depth census counts and the uncapped total;
  ``Appendix X.`` headings are recorded as their own census;
  table-of-contents entries (heading shape followed by a trailing
  page number after whitespace or a dot leader) are counted as
  ``toc_entries`` and NEVER enter the clause ledger;
* T2 RFC 2119 keyword census — the ten requirement levels of
  RFC 2119 (MUST / MUST NOT / SHALL / SHALL NOT / SHOULD /
  SHOULD NOT / MAY / REQUIRED / RECOMMENDED / OPTIONAL) counted
  as UPPERCASE word-boundary occurrences (the RFC 2119
  convention — lowercase prose forms never vote), with bounded
  line samples and the count of literal ``RFC 2119`` citations.

T0 — independence from the identification layer: the RFC header
markers in ``domain_signals`` (rfc_header, category_line, …) are
CALIBRATION-FROZEN assets whose shapes are vote-optimized; this
ledger implements its own locator regexes with full ledger
semantics (the email/academic precedent — the two layers own
their own correctness, and the marker family stays
calibration-locked regardless of this chain).

Contract discipline (mirrors invoice/academic/email chains):

* verbatim projection — every located line number and bounded
  text is present; nothing summarized away;
* line numbers refer to ``original_text`` lines (1-based) — no
  salvage, no line_map, no second coordinate space;
* DQR surfaces the payload VERBATIM and never folds it into the
  four dimension scores;
* attach-only — document text fields are never touched.

Honest boundaries:

* the clause ledger is SHAPE-BASED: a flush-left numbered line
  (e.g. a numbered list item ``3. Boil water``) is recorded as a
  clause — heading-ness is a convention judgement the ledger
  does not make (declared limitation, recorded here);
* heading shapes beyond numbered sections and ``Appendix X.``
  (unnumbered ``REFERENCES``, ``ACKNOWLEDGEMENTS``, author
  blocks) are honest misses, never guesses;
* TOC styles WITHOUT a trailing page number (the rfc9000-family
  TOC in this corpus — bare indented entries) are honest misses
  for ``toc_entries``: no page number, no TOC fact (they stay out
  of the clause ledger either way — they are indented);
* TOC entries with a SINGLE-SPACE page number (rfc1305's mixed
  layout) stay in the clause ledger — the single-space rule was
  corpus-tested and rejected (real headings ending in bare
  numbers, e.g. "Reply Code 502", would be misclassified); their
  fallout is visible in the hierarchy-gap census;
* centered TOP-LEVEL headings (the rfc791/rfc793 family — parents
  centered, children flush-left) and indented MID-LEVEL headings
  (rfc2616 13.1, rfc959 subsections) are honest misses for the
  clause ledger, QUANTIFIED by ``clause_hierarchy_gaps``;
* ``clause_sequence_anomalies`` are markers, not verdicts — the
  flagged line may be the intruding non-heading line or the first
  real heading after an intruder (see rfc1035's "3.5" victim);
* indented SUBSECTION headings (the rfc959 style
  ``   2.1.  HISTORY``) are honest misses for the clause ledger:
  the flush-left rule stands, because relaxing it would flood the
  ledger with the indented TOC entries of no-page-number TOCs —
  the two styles are indistinguishable line by line;
* compound RFC 2119 levels split across a line wrap
  (``MUST\``new line``NOT``) are honest misses — the two-word
  forms are matched within a single line;
* ``SHALL NOT`` etc. count exactly as written; the census is a
  vocabulary count, NOT a semantic interpretation of
  requirement strength;
* no clause-aware chunking in v1 — the ledger locates clauses
  only; chunk integration is a v2 candidate
  (representation_profile stays the declared
  ``clause_aware_v1`` label);
* the resolver's label decision is upstream (frameworks registry
  dispatch); this module is the pure projection primitive.

Dispatch (frameworks registry): the chain is registered under
``tech_standard_scenario`` in ``core/scenario/frameworks.py`` and
invoked by ``attach_scenario_chain`` after the L2 resolver labels
the text ``tech_standard``.
"""

from __future__ import annotations

import re
from typing import Any

SCENARIO_METADATA_KEY = "tech_standard_scenario"

_MAX_CLAUSES = 400
_MAX_APPENDIX = 50
_MAX_RFC2119_SAMPLES = 100
_MAX_TEXT_CHARS = 80

# T1 — clause-numbering locator. A heading line is ``N(.N)*.? title``
# with the number flush at column 0 (real RFC body headings are
# flush-left at every depth — corpus evidence rfc1035 L380
# "2.3.1. Preferred name syntax"); up to four number segments.
_CLAUSE_RE = re.compile(
    r"^(?P<num>\d{1,3}(?:\.\d{1,3}){0,3})\.?[ \t]+(?P<title>\S.*)$"
)
# TOC entry: the same heading shape (leading whitespace allowed —
# TOC entries are indented by depth) whose title portion ends with
# a trailing page number after a dot leader or wide whitespace
# (corpus evidence rfc1035 L33 "1. STATUS OF THIS MEMO     1",
# rfc8446 L209 "Appendix A. State Machine .......120", rfc2818 L36
# "1. Introduction  . . . . . . . . . . . . . . . 2" — the three
# leader conventions: wide spaces, consecutive dots, and
# single-space-separated dots).
_TOC_PAGE_RE = re.compile(
    r"(?:\.{2,}[ \t]*|(?:\.[ \t]+){2,}|[ \t]{2,})\d{1,4}[ \t]*$"
)
# indented heading shape for TOC detection (leading whitespace)
_INDENTED_CLAUSE_RE = re.compile(
    r"^[ \t]+(?P<num>\d{1,3}(?:\.\d{1,3}){0,3})\.?[ \t]+(?P<title>\S.*)$"
)
# Appendix heading: ``Appendix X. Title`` flush-left.
_APPENDIX_RE = re.compile(
    r"^Appendix[ \t]+(?P<label>[A-Z])\.?[ \t]+(?P<title>\S.*)$"
)

# T2 — RFC 2119 requirement levels. Compounds first (matched
# within a line); singles are then counted excluding the compound
# occurrences. UPPERCASE only — the RFC 2119 convention.
_RFC2119_COMPOUNDS = (
    ("MUST NOT", re.compile(r"\bMUST[ \t]+NOT\b")),
    ("SHALL NOT", re.compile(r"\bSHALL[ \t]+NOT\b")),
    ("SHOULD NOT", re.compile(r"\bSHOULD[ \t]+NOT\b")),
)
_RFC2119_SINGLES = (
    ("MUST", re.compile(r"\bMUST\b")),
    ("SHALL", re.compile(r"\bSHALL\b")),
    ("REQUIRED", re.compile(r"\bREQUIRED\b")),
    ("SHOULD", re.compile(r"\bSHOULD\b")),
    ("RECOMMENDED", re.compile(r"\bRECOMMENDED\b")),
    ("MAY", re.compile(r"\bMAY\b")),
    ("OPTIONAL", re.compile(r"\bOPTIONAL\b")),
)
_RFC2119_CITATION_RE = re.compile(r"\bRFC[ \t]+2119\b")

_RFC2119_LEVELS = (
    "MUST", "MUST NOT", "SHALL", "SHALL NOT", "REQUIRED",
    "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", "OPTIONAL",
)


def _is_toc_entry(title: str) -> bool:
    """True when the title portion ends with a trailing page number."""
    return _TOC_PAGE_RE.search(title) is not None


def build_tech_standard_payload(original_text: str) -> dict[str, Any]:
    """Deterministic structure-fact ledger (v1).

    Pure function; no I/O; never raises on empty text (an empty
    ledger with zero stats is a legal, honest outcome).
    """
    lines = original_text.split("\n")
    # CRLF tolerance: matching happens on \r-stripped lines, while
    # line numbers (the ledger's coordinates) are unaffected
    stripped = [
        line[:-1] if line.endswith("\r") else line for line in lines
    ]
    n = len(lines)

    # --- T1: clause numbering hierarchy ----------------------------
    clauses: list[dict[str, Any]] = []
    appendix: list[dict[str, Any]] = []
    toc_count = 0
    toc_first_line: int | None = None
    toc_last_line: int | None = None
    for idx, line in enumerate(stripped):
        m = _CLAUSE_RE.match(line)
        if m is not None:
            if _is_toc_entry(m.group("title")):
                # flush-left + trailing page number — a TOC entry
                # that happens to start at column 0
                toc_count += 1
                if toc_first_line is None:
                    toc_first_line = idx + 1
                toc_last_line = idx + 1
                continue
            number = m.group("num")
            clauses.append({
                "line": idx + 1,
                "number": number,
                "depth": number.count(".") + 1,
                "title": m.group("title")[:_MAX_TEXT_CHARS],
            })
            continue
        m = _APPENDIX_RE.match(line)
        if m is not None:
            if _is_toc_entry(m.group("title")):
                toc_count += 1
                if toc_first_line is None:
                    toc_first_line = idx + 1
                toc_last_line = idx + 1
                continue
            appendix.append({
                "line": idx + 1,
                "label": m.group("label"),
                "title": m.group("title")[:_MAX_TEXT_CHARS],
            })
            continue
        # indented heading shape — TOC entry only with a trailing
        # page number; an indented numbered line without one is
        # neither a body clause nor a TOC fact (honest miss)
        m = _INDENTED_CLAUSE_RE.match(line)
        if m is not None and _is_toc_entry(m.group("title")):
            toc_count += 1
            if toc_first_line is None:
                toc_first_line = idx + 1
            toc_last_line = idx + 1

    depth_counts: dict[str, int] = {}
    max_depth = 0
    for clause in clauses:
        key = str(clause["depth"])
        depth_counts[key] = depth_counts.get(key, 0) + 1
        max_depth = max(max_depth, clause["depth"])

    # --- v1.1 derived censuses (polish r1) ------------------------
    # hierarchy gaps: parents absent from the flush-left ledger
    # (centered top-level headings, indented mid-level headings —
    # quantified misses, never guesses)
    clause_numbers = {c["number"] for c in clauses}
    hierarchy_gaps: list[dict[str, Any]] = []
    for clause in clauses:
        parts = clause["number"].split(".")
        if len(parts) >= 2:
            parent = ".".join(parts[:-1])
            if parent not in clause_numbers:
                hierarchy_gaps.append({
                    "line": clause["line"],
                    "number": clause["number"],
                    "missing_parent": parent,
                })
    # sequence anomalies: numbers breaking strictly-increasing
    # order (numeric-tuple lexicographic). Markers, not verdicts —
    # the flagged line may be the intruder (prose/equation/
    # reference line starting with a number) or the first real
    # heading after an intruder (the victim).
    sequence_anomalies: list[dict[str, Any]] = []
    prev_key: tuple[int, ...] | None = None
    for clause in clauses:
        num_key = tuple(int(x) for x in clause["number"].split("."))
        if prev_key is not None and num_key <= prev_key:
            sequence_anomalies.append({
                "line": clause["line"],
                "number": clause["number"],
            })
        prev_key = num_key

    # --- T2: RFC 2119 keyword census --------------------------------
    counts = {level: 0 for level in _RFC2119_LEVELS}
    samples: list[dict[str, Any]] = []
    citations = 0
    for idx, line in enumerate(stripped):
        citations += len(_RFC2119_CITATION_RE.findall(line))
        compounds = 0
        for level, rx in _RFC2119_COMPOUNDS:
            found = len(rx.findall(line))
            counts[level] += found
            compounds += found
            for _ in range(found):
                if len(samples) < _MAX_RFC2119_SAMPLES:
                    samples.append({
                        "line": idx + 1,
                        "keyword": level,
                        "text": line.strip()[:_MAX_TEXT_CHARS],
                    })
        # singles, excluding occurrences inside compound forms:
        # strip the compound spans, then count what remains
        remainder = line
        for _, rx in _RFC2119_COMPOUNDS:
            remainder = rx.sub(" ", remainder)
        for level, rx in _RFC2119_SINGLES:
            found = len(rx.findall(remainder))
            if found:
                counts[level] += found
                for _ in range(found):
                    if len(samples) < _MAX_RFC2119_SAMPLES:
                        samples.append({
                            "line": idx + 1,
                            "keyword": level,
                            "text": line.strip()[:_MAX_TEXT_CHARS],
                        })

    return {
        "extractor": "tech_standard_scenario_v1.1",
        "total_lines": n,
        "clauses": clauses[:_MAX_CLAUSES],
        "clause_count": len(clauses),
        "clause_max_depth": max_depth,
        "clause_depth_counts": depth_counts,
        "appendix_headings": appendix[:_MAX_APPENDIX],
        "appendix_heading_count": len(appendix),
        "toc_entries": toc_count,
        "toc_first_line": toc_first_line,
        "toc_last_line": toc_last_line,
        "clause_hierarchy_gaps": hierarchy_gaps,
        "clause_hierarchy_gap_count": len(hierarchy_gaps),
        "clause_sequence_anomalies": sequence_anomalies,
        "clause_sequence_anomaly_count": len(sequence_anomalies),
        "rfc2119_counts": counts,
        "rfc2119_total": sum(counts.values()),
        "rfc2119_citations": citations,
        "rfc2119_samples": samples,
        "stats": {
            "clauses_capped": len(clauses) > _MAX_CLAUSES,
            "appendix_capped": len(appendix) > _MAX_APPENDIX,
            "rfc2119_samples_capped": (
                len(samples) >= _MAX_RFC2119_SAMPLES
            ),
        },
        # honest boundary, machine-readable
        "note": (
            "structure-fact ledger: clauses are flush-left numbered "
            "headings (1-4 segments, verbatim number token, bounded "
            "title) — the ledger is SHAPE-BASED, so a flush-left "
            "numbered list line is recorded as a clause (declared "
            "limitation); Appendix X. headings are their own census; "
            "TOC entries (heading shape with a trailing page number "
            "after a dot leader or whitespace) are counted, never "
            "entering the clause ledger; TOC styles without page "
            "numbers and indented subsection headings (rfc959) are "
            "honest misses; RFC 2119 levels are counted "
            "as UPPERCASE word occurrences with compounds (MUST NOT "
            "/ SHALL NOT / SHOULD NOT) matched within a single line "
            "(line-wrapped compounds are honest misses) — a "
            "vocabulary count, never a semantic reading of "
            "requirement strength; clause_hierarchy_gaps quantify "
            "centered-top-level (rfc791/rfc793) and indented-mid-level "
            "heading misses; clause_sequence_anomalies are markers, not "
            "verdicts (intruder or first-real-heading-after-intruder); "
            "single-space TOC page numbers stay in the clause ledger "
            "(rule tested and rejected on real headings ending in bare "
            "numbers); no clause-aware chunking in v1; unknown heading "
            "shapes are honest misses, never guesses"
        ),
    }


def attach_tech_standard_scenario(document: Any) -> None:
    """Stamp the ledger on ``metadata[SCENARIO_METADATA_KEY]``.

    Attach-only: mutates ``document.metadata`` in place, mirroring
    the runtime stamping pattern; document text fields are never
    touched.
    """
    original_text = getattr(document, "original_text", "") or ""
    document.metadata[SCENARIO_METADATA_KEY] = (
        build_tech_standard_payload(original_text)
    )


__all__ = [
    "SCENARIO_METADATA_KEY",
    "attach_tech_standard_scenario",
    "build_tech_standard_payload",
]
