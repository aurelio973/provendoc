# ============================================================
# Narrative Domain
# RTF (.rtf) Parser — pure-stdlib state machine
# ============================================================
# Contract source: docx_rtf_prereg_v1.md (v1 frozen);
# v2 upgrade contract: rtf_family_prereg_v1.md (D1-D13
# all A, decided 2026-10-05). v1 → v2 breaking change registered: extractor name
# bumped to rtf_structure_v2; all v1 evidence keys retained without reduction.
#
# v2 added evidence surfaces (rtf_family_prereg_v1 §2b/§3):
#   - D10 destination three-way classification: _STRUCTURE_DESTINATIONS (fonttbl/
#     colortbl/info — collected and parsed in-group) / _RESOURCE_DESTINATIONS (pict —
#     hex-decoded into byte assets) / the rest keep whole-group skip with name counting;
#   - D7 font_table / color_table / document_info / generator registration;
#   - D9 \upr/\ud dual-version: Unicode version preferred, \*\ud collected as fallback,
#     isolated surrogates (UNICODE_PRIMARY_CORRUPTED) backfilled with ud text;
#   - D5 table content tier: \trowd/\cellxN/\cell/\row row-cell-boundary registration
#     (\cell renders as tab, table text is not lost);
#   - D6 rtf_runs: character-format range registration (runs_head sample + total count,
#     body stays clean without mixed markup);
#   - D4 image assets: \pict hex → bytes → asset_id (content hash,
#     same schema as docx_image_assets); damage registers a reason instead of silent drop;
#   - unknown \*\xxx destinations count into unknown_destinations
#     (UNKNOWN_RTF_DESTINATION — not dropped, not treated as body);
#   - tri-state: resolution_status complete/partial + reason_codes.
#
# P0b closure keys (rtf_family_prereg_v1 D11/D12/D3, decided all A):
#   - coverage six keys (text/paragraphs/character_format/tables/images/
#     embedded_objects) each an independent tri-state, no single aggregate coverage; criteria come
#     only from in-source provable facts (truncation/damage/registration caps); when the file declares
#     no content of a class, the key is complete (vacuous complete, noted);
#   - page_break_declared: only the in-source provable \page count (no page-number
#     inference; page_number_inferred/page_assignment_rendered are not fabricated);
#   - source_hint（metadata/generator/metadata_or_generator）+
#     conversion_artifact: inferred from \*\generator text plus the \info group;
#     the source is a lead only and does not decide the parse result;
#   - evidence_type: native_rtf; vocabulary reserves rendered_conversion
#     (D12 A: no external conversion dependency introduced);
#   - RtxParser (.rtx content-sniffing routing, D3 A): a {\rtf header hit goes
#     through the RTF channel; a miss is handled as text and registers format_mismatch.
#
# P1/P2 closing keys (rtf_family_prereg_v2 D14-D22, 2026-10-06
# decided all A):
#   - D14 stylesheet parsing: style entries (id/name/type/basedon/next/
#     outline_level) + heading determination (style name heading N or an
#     \outlinelev0-8 definition); heading_candidates generated from paragraph style references or
#     paragraph attribute \outlinelevN (HeadingCandidate, provenance=
#     declared, cap 50 with truncation registered);
#   - D15 listtable/listoverridetable parse registration (levelstartat/
#     levelfollow/leveltext raw; \listtext still stays out of the body);
#   - D16 header/footer family → header_footer_view collected by kind;
#     footnote → footnotes registration list (not merged into main; \chftn anchor
#     counted); multi-section \sect/\sectd counted in sections_total;
#   - D17 fields samples (fldinst instruction head truncated to 80 × 20 +
#     is_hyperlink); \bkmkstart/\bkmkend collected into bookmark_names;
#     \*\annotation → annotation_view; revisions {\insrs..}/{\delrs..}:
#     ins text kept in main + a revision_view.inserted copy; del text
#     stays out of main (deleted is a fact) and only lands in revision_view.deleted;
#     r25 (2): the standard forms {\revised..}/{\deleted..} take the same route (deleted
#     text had previously leaked into main — fixed as such); r25 (4): \*\revtbl author names
#     and \*\rsidtbl session identifiers are now collected (verbatim census);
#   - D18 views six keys (main_view summary/header_footer_view/
#     annotation_view/revision_view/hidden_text_view/metadata_view
#     summary; hidden text is still kept in main — conservative, no loss);
#   - D19 RTFD directory package (RtfdParser: TXT.rtf main document + attachment
#     sha256↔asset_id reconciliation + orphan-resource registration);
#   - D20 \object upgraded to collect: objclass + objdata hex decoding (the hex
#     channel is an ASCII text channel, lossless) → embedded_objects table; when objdata
#     magic hits a {\rtf header → recursive _extract_rtf registers the nested body;
#     coverage.embedded_objects criterion unsupported → partial (P0b
#     boundary uplift, deviation registered);
#   - D21 group-level recovery evidence made explicit (group_recovery: unclosed_at_eof/
#     early_closes + GROUP_RECOVERY_APPLIED); \bin out-of-range
#     BIN_TRUNCATED; \bin byte-asset recovery is not done — the text channel (utf-8/
#     replace decode chain) has already destroyed the binary bytes; slicing characters into bytes =
#     fabricating values without evidence (safe_decode implementation audited 2026-10-06);
#   - D22 WMF/EMF magic already covered by P0a; render comparison keeps D12 A — no external
#     conversion dependency introduced.
#
# Fact boundary (Word-family content format, same as DOCX — not source-evidence):
#   - Document.text = extracted rendered body (control words/destination content stay out of
#     the body — spec §6 "downstream must not see rtf_control_word fragments");
#   - source_text = decoded raw text (evidence preservation channel, existing contract);
#   - heading_candidates = [] (the style table is file-local; v1/v2 do not parse styles
#     or guess heading text; \outlinelevN is only counted);
#   - failure degrades silently: non-RTF files get parse_failed honest failure;
#   - truncation/bracket imbalance: unbalanced_groups registered as-is; the already-extracted body
#     is best-effort preserved (no silent truncation loss).
#
# Dependency: zero third-party dependencies (parser_registry precedent; no striprtf introduced).


# ------------------------------------------------------------
# 2026-11-10 split note: this file was originally a single 2630-line implementation, mechanically split into
# the rtf/ three-layer package (facts = evidence registration / scanner = state-machine core / adapters = boundary
# adaptation, zero per-line changes). This module remains a facade: the parser_registry module
# path string (".rtf" -> "...narrative.rtf_parser") and existing test imports
# (_EVIDENCE_TYPE_VOCAB / _to_codepage_name / the three Parser classes) stay drift-free.
# ------------------------------------------------------------

from provendoc.core.parsers.narrative.rtf.adapters import (
    RtfdParser,
    RtfParser,
    RtxParser,
)
from provendoc.core.parsers.narrative.rtf.facts import (
    _EVIDENCE_TYPE_VOCAB,
)
from provendoc.core.parsers.narrative.rtf.scanner import (
    _to_codepage_name,
)

__all__ = [
    "RtfdParser",
    "RtfParser",
    "RtxParser",
    "_EVIDENCE_TYPE_VOCAB",
    "_to_codepage_name",
]
