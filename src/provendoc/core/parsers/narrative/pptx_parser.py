# ============================================================
# Narrative Domain
# PowerPoint (.pptx) Parser
# ============================================================
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts,
    detect_security_carriers,
)
from provendoc.core.foundation.metadata import _build_metadata

# ZIP container guard (zip-bomb protection) before decompression.
from provendoc.core.parsers.container_guard import (
    check_zip_package_quietly as _check_zip_package_safe,
)
from provendoc.core.parsers.container_guard import (
    zip_guard_error,
)
from provendoc.core.parsers.narrative.omml import (
    OMATH,
    OMATH_PARA,
    omml_linear_text,
    omml_structure_counts,
    omml_to_mathml,
)
from provendoc.core.parsers.narrative.pptx_evidence import (
    classify_pptx_container,
    extract_pptx_evidence,
    iter_preferred,
    notes_slide_part,
    presentation_modify_verifier,
    presentation_slide_order,
    slide_rels,
)
from provendoc.core.parsers.narrative.pptx_limits import (
    PptxRuntimeLimits,
)
from provendoc.core.parsers.narrative.pptx_presentation_nodes import (
    derive_presentation_nodes,
)
from provendoc.core.parsers.narrative.shared import (
    localname as _localname,
)
from provendoc.core.parsers.narrative.shared import (
    markdown_table_lines as _markdown_table_lines,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.heading_patterns import HeadingCandidate
from provendoc.core.text.normalization import sanitize_invisible_chars

_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_TITLE_PH_TYPES = {"title", "ctrTitle"}

# PowerPoint-family identity by extension: OpenXML suffixes only;
# legacy CFB (.ppt/.pps/.pot) fails honestly with no profile.
_FAMILY_PROFILES = {
    ".pptx": ("presentation", "normal", False),
    ".pptm": ("presentation", "normal", False),
    ".ppsx": ("presentation", "slide_show", False),
    ".ppsm": ("presentation", "slide_show", False),
    ".potx": ("template", "normal", True),
    ".potm": ("template", "normal", True),
}


def _family_profile(file_path):
    """Extension-derived family identity (evidence-only)."""
    suffix = Path(file_path).suffix.lower()
    entry = _FAMILY_PROFILES.get(suffix)
    if entry is None:
        return None
    document_type, mode, is_template = entry
    return {
        "extension": suffix,
        "document_type": document_type,
        "presentation_mode": mode,
        "is_template": is_template,
        "note": (
            "extension-derived fact, not content judgment; "
            "template sample text inclusion is a downstream decision"
        ),
    }


# ------------------------------------------------------------
# CFB stream-level evidence (evidence-only)
# ------------------------------------------------------------
# Container facts reachable via olefile: no text extraction,
# no structure repair. olefile is an optional dependency; when
# missing the caller falls back to the 8-byte classification.
def _cfb_stream_facts(file_path):
    """Stream-level facts after opening with olefile.

    status: unavailable / open_failed / ppt_stream /
    no_ppt_stream. structure is present only when the file
    opened. Stream-name comparison is case-insensitive.
    """
    try:
        import olefile  # 条件依赖 (D5): doc extra
    except ImportError:
        return {"status": "unavailable"}
    try:
        ole = olefile.OleFileIO(str(file_path))
    except Exception:
        # corrupt/truncated CFB: stop here, no guessing
        return {"status": "open_failed"}
    with ole:
        entries = ole.listdir()
    stream_names = sorted({e[-1] for e in entries})
    lowered = [e[-1].lower() for e in entries]
    has_ppt_doc = "powerpoint document" in lowered
    has_current_user = "current user" in lowered
    structure = {
        "extractor": "ppt_cfb_structure_v1",
        "streams": stream_names,
        "powerpoint_document_stream": has_ppt_doc,
        "current_user_stream": has_current_user,
        "note": (
            "opened as OLE/CFB; PowerPoint Document stream present; "
            "text extraction frozen pending corpus anchor "
            "(evidence-only batch)"
            if has_ppt_doc
            else "opened as OLE/CFB; no PowerPoint Document stream "
            "(not a legacy PowerPoint binary; container facts only)"
        ),
    }
    return {
        "status": "ppt_stream" if has_ppt_doc else "no_ppt_stream",
        "structure": structure,
    }


class _EvidenceBudget:
    """Shared budget for the three evidence channels.

    Counts extraction actions (chart part reads / smartart
    data part reads / oMath conversions). ``take()`` gates each
    action; at the cap a channel stops early and one truncation
    fact is registered. ``max_slides`` is reused by declared
    order: slides beyond the cap are skipped entirely.
    """

    def __init__(self, limits: Optional[PptxRuntimeLimits]) -> None:
        self.cap = (
            limits.max_evidence_parts if limits is not None else None
        )
        self.slide_cap = (
            limits.max_slides if limits is not None else None
        )
        self.used = 0
        self.channel_used: Dict[str, int] = {}
        self.truncated_channels: List[str] = []
        self.facts: List[Dict[str, Any]] = []
        self.records: List[Dict[str, Any]] = []
        self.reason: Optional[str] = None

    def slide_allowed(self, position: int) -> bool:
        """Whether the declared position is within max_slides."""
        if self.slide_cap is None:
            return True
        return position <= self.slide_cap

    def take(self, channel: str, part: str) -> bool:
        """Budget check: True = allowed and counted."""
        if self.cap is None:
            return True
        if self.used >= self.cap:
            self._register_truncation(channel, part)
            return False
        self.used += 1
        self.channel_used[channel] = (
            self.channel_used.get(channel, 0) + 1
        )
        return True

    def _register_truncation(self, channel: str, part: str) -> None:
        """Register each truncated channel at most once."""
        if channel in self.truncated_channels:
            return
        self.truncated_channels.append(channel)
        extracted = self.channel_used.get(channel, 0)
        self.facts.append({
            "kind": "evidence_parts_truncated",
            "channel": channel,
            "parts_extracted": extracted,
            "max_evidence_parts": self.cap,
            "part": part,
        })
        self.records.append({
            "object_type": "evidence_channel",
            "object_id": f"{channel}-parts-after-{extracted}",
            "resolution": "partial",
            "reason_code": "EVIDENCE_PARTS_NOT_EXTRACTED",
            "source": part,
            "placeholder_text": "",
        })
        self.reason = (
            f"evidence parts beyond {self.cap} not extracted "
            f"({'/'.join(self.truncated_channels)} capped, "
            f"max_evidence_parts={self.cap})"
        )


class PptxParser(BaseParser):
    """PPTX parser: conservative zipfile + ElementTree extraction.

    Facts:
        - Slide order follows sldIdLst declaration order via
          presentation rels; with missing/broken rels it falls
          back to filename order, recorded in
          metadata["slide_order_method"]
        - mc:AlternateContent: only the preferred (Choice) branch
          is entered, so no object is extracted twice
        - Notes are matched via slide rels; filename numbering is
          a fallback only
        - Each slide renders as "## Slide N: {title}" plus text
          paragraphs (a:p / a:t); title placeholders
          (p:ph type=title/ctrTitle) are recognized separately;
          N is the declared position, not the file number
        - Tables (a:tbl) render as pipe rows; images (p:pic) are
          recorded as placeholders
        - Charts resolve via slide rels to ppt/charts/chartN.xml:
          "[Chart: {title}]" placeholder in the narrative,
          structured series/category/value facts in
          metadata["pptx_charts"]
        - SmartArt resolves via slide rels to
          ppt/diagrams/dataN.xml: visible text as list items in
          the narrative, modelId + parent-child tree in
          metadata["pptx_smartart"]
        - Speaker notes are appended as "Notes:" paragraphs
        - Media (a:videoFile/a:audioFile) and OLE objects
          (p:oleObj) are not extracted: placeholder line + count
          (UNSUPPORTED-BUT-RECORDED); their p:pic/graphicFrame do
          not count toward image_count
        - OMML formulas go to metadata["pptx_formulas"] (same fact
          shape as docx_formulas); m:t text stays in the linear
          narrative text
        - Coverage facts (pptx_coverage): referenced-vs-resolved
          pairs (charts/smartart/notes)
        - Layouts/themes/masters do not enter the narrative; a
          package with zero slide parts fails honestly

    pptx is a binary container format: source_text is None (see
    the Document model contract).
    """

    def __init__(
        self, encoding="utf-8", tolerant=True, include_notes=True,
        enable_image_assets=True,
        limits: PptxRuntimeLimits | None = None,
    ):
        """Initialize with encoding, tolerance, notes, image assets
        and runtime limits.

        ``limits`` guards rather than filters (default None =
        historic behavior): over-cap documents are not dropped;
        expensive paths are skipped and recorded.
        """
        self.encoding = encoding
        self.tolerant = tolerant
        self.include_notes = include_notes
        # Same contract as docx/pdf: no images is a natural no-op;
        # when enabled this additionally yields
        # metadata["image_asset_payloads"] (bytes, in memory) and
        # pptx_image_assets facts. False = explicitly off.
        self.enable_image_assets = enable_image_assets
        self.limits = limits

    def parse(self, file_path):
        """Parse a PowerPoint-family OpenXML package into a
        Document.

        .pptx/.pptm/.ppsx/.ppsm/.potx/.potm share one chain;
        encrypted / legacy .ppt (CFB) packages fail honestly.
        """
        metadata = _build_metadata(file_path)
        # file_type from the suffix; variants carry identity.
        metadata["file_type"] = Path(file_path).suffix.lower().lstrip(".")
        # Extension-derived identity facts, not a content judgment.
        _profile = _family_profile(file_path)
        if _profile is not None:
            metadata["pptx_profile"] = _profile

        # ZIP container guard (zip-bomb protection): reads only the
        # central directory, decides before decompression; a
        # violation is an honest failure.
        _zip_guard = _check_zip_package_safe(file_path)
        if _zip_guard is not None:
            metadata["zip_package_guard"] = _zip_guard
            if _zip_guard.get("verdict") == "violation":
                metadata["parse_failed"] = True
                metadata["parse_error"] = zip_guard_error(_zip_guard)
                return Document(text="", metadata=metadata)

        start_time = time.perf_counter()

        try:
            # Container pre-flight: an encrypted OOXML package (MS-OFFCRYPTO)
            # or a legacy binary .ppt renamed .pptx can never open as a
            # zip.  The classification is recorded as evidence before the
            # honest failure (same contract as the docx pre-flight).
            with open(file_path, "rb") as f:
                raw_head = f.read(8)
            container_facts = classify_pptx_container(raw_head)
            if container_facts["ole_container"]:
                metadata["pptx_security"] = {
                    "encryption": container_facts,
                }
                # CFB stream-level evidence: text extraction for
                # legacy PowerPoint binaries is not implemented;
                # text stays "" in every branch.
                cfb_facts = _cfb_stream_facts(file_path)
                _status = cfb_facts["status"]
                if _status in ("no_ppt_stream", "ppt_stream"):
                    metadata["ppt_cfb_structure"] = cfb_facts["structure"]
                if _status == "open_failed":
                    raise ValueError(
                        "not an Open XML zip package: "
                        + str(container_facts["note"])
                        + "; olefile open failed "
                        "(corrupt/truncated CFB honest failure)"
                    )
                if _status == "ppt_stream":
                    raise ValueError(
                        "not an Open XML zip package: "
                        + str(container_facts["note"])
                        + "; text_extraction_frozen: legacy PowerPoint "
                        "binary text extraction not implemented "
                        "(frozen pending corpus anchor, prereg "
                        "pptx_cfb_evidence_v1 §4)"
                    )
                if _status == "no_ppt_stream":
                    raise ValueError(
                        "not an Open XML zip package: "
                        + str(container_facts["note"])
                        + "; no PowerPoint Document stream "
                        "(not a legacy PowerPoint binary; "
                        "container facts only)"
                    )
                raise ValueError(
                    "not an Open XML zip package: "
                    + str(container_facts["note"])
                )

            (
                text, headings, stats, slides_meta, order_facts, coverage,
                unresolved_records, object_records,
            ) = self._extract(
                file_path, include_notes=self.include_notes,
                limits=self.limits,
            )
            # Guard facts travel back via the stats dict (return
            # signature unchanged) and are handled below.
            guard_reasons = stats.pop("guard_reasons", [])
            guard_facts = stats.pop("guard_facts", [])
            # p:modifyVerifier write-protection declaration (recorded
            # only, never enforced).
            modify_verifier = stats.pop("modify_verifier", None)
            # Shared evidence budget for the three channels below.
            evidence_budget = _EvidenceBudget(self.limits)
            charts_facts = self._extract_charts(
                file_path, budget=evidence_budget,
            )
            smartart_trees = self._extract_smartart(
                file_path, budget=evidence_budget,
            )
            formula_facts = self._extract_formulas(
                file_path, budget=evidence_budget,
            )
            # Evidence truncation merges into existing channels.
            if evidence_budget.records:
                unresolved_records.extend(evidence_budget.records)
            if evidence_budget.facts:
                guard_facts.extend(evidence_budget.facts)
            if evidence_budget.reason is not None:
                guard_reasons.append(evidence_budget.reason)

            # Honest failure: the package opened but contains no slide
            # parts (layout/master-only fixtures, renamed foreign
            # packages). Slides are the narrative document; a successful
            # parse yielding no text would force the representation layer
            # to emit an empty document -- the contract instead requires
            # an explicit parse_failed (mirrors docx empty-narrative rule).
            if stats["slides"] == 0:
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "package contains no slide parts"
                )
                metadata["parse_success"] = False
                return Document(text="", metadata=metadata)

            metadata["heading_candidates"] = headings
            # Per-slide [start, end) spans in the narrative text plus
            # titles (read-only facts).
            metadata["pptx_slides"] = slides_meta
            # Slide order method and orphan slides (fallbacks recorded).
            metadata["slide_order_method"] = order_facts["method"]
            if order_facts["orphan_numbers"]:
                metadata["orphan_slide_numbers"] = order_facts[
                    "orphan_numbers"
                ]
            # Hidden slides (p:sld@show="0"/"false"): text still enters
            # the narrative; hiddenness is recorded separately.
            hidden_slide_numbers = [
                entry["slide_number"] for entry in slides_meta
                if entry.get("show") in ("0", "false")
            ]
            if hidden_slide_numbers:
                metadata["hidden_slide_numbers"] = hidden_slide_numbers
            # Auto-advance slides (p:transition/@advTm present).
            auto_advance_slide_numbers = [
                entry["slide_number"] for entry in slides_meta
                if entry.get("transition", {}).get("advTm") is not None
            ]
            if auto_advance_slide_numbers:
                metadata["auto_advance_slide_numbers"] = (
                    auto_advance_slide_numbers
                )
            # Write-protection facts (p:modifyVerifier), written only
            # when present.
            if modify_verifier is not None:
                metadata["pptx_modify_verifier"] = modify_verifier
            # Coverage facts: referenced vs resolved pairs, auditable.
            metadata["pptx_coverage"] = coverage
            # Object-level unresolved records (machine-readable,
            # locatable; narrative placeholder lines unchanged).
            if unresolved_records:
                metadata["pptx_unresolved"] = {
                    "extractor": "pptx_unresolved_v1",
                    "records": unresolved_records,
                }
            # Resource guards: written only when triggered; reasons
            # aggregate one line per guard kind, per-page detail goes
            # into the structured facts.
            if guard_reasons:
                resource_guard = {
                    "scheme": "resource_limits_v1",
                    "limits": self.limits.as_dict(),
                    "reasons": list(guard_reasons),
                }
                if guard_facts:
                    resource_guard["facts"] = [
                        dict(fact) for fact in guard_facts
                    ]
                metadata["resource_guard"] = resource_guard
            # Structured chart/SmartArt facts, written only when
            # non-empty.
            if charts_facts:
                metadata["pptx_charts"] = {"charts": charts_facts}
            if smartart_trees:
                metadata["pptx_smartart"] = {"diagrams": smartart_trees}
            # OMML formula facts (same shape as docx_formulas), with
            # slide origin.
            if formula_facts:
                metadata["pptx_formulas"] = {"formulas": formula_facts}
            # Media/OLE counts (UNSUPPORTED-BUT-RECORDED).
            metadata["video_count"] = stats["videos"]
            metadata["audio_count"] = stats["audios"]
            metadata["ole_object_count"] = stats["ole_objects"]
            # Domain signals (same detector as other parsers).
            metadata["domain_signals"] = detect_domain_signals(text)

            # Security carrier census over the extracted text (pptx
            # does not use the base read-and-diagnose path); warning
            # codes into warnings, counts into
            # metadata["security_carriers"] only when non-zero.
            carrier_census = detect_security_carriers(text)
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata.setdefault("warnings", []).extend(
                    build_carrier_facts(carrier_census)
                )

            metadata["parse_success"] = True
            metadata["slide_count"] = stats["slides"]
            metadata["title_count"] = stats["titles"]
            metadata["table_count"] = stats["tables"]
            metadata["image_count"] = stats["images"]
            metadata["chart_count"] = stats["charts"]
            metadata["smartart_count"] = stats["smartart"]
            metadata["notes_slide_count"] = stats["notes"]

            # Evidence layer (B7/B8 follow-up): package / security /
            # text-character / watermark namespaces.  Degrades honestly,
            # never fatal -- the narrative extraction above already
            # succeeded.
            metadata.update(extract_pptx_evidence(file_path))

            # Unified presentation node tree derived from existing
            # evidence keys (no new IO), written only when non-empty;
            # per-slide object probing records likewise.
            if object_records:
                metadata["pptx_slide_objects"] = object_records
            _nodes = derive_presentation_nodes(metadata)
            if _nodes:
                metadata["presentation_nodes"] = _nodes

            # Image assets (opt-in, same contract as docx/pdf):
            # bit-exact bytes + per-slide placement; failures degrade
            # to an honest error record.
            if self.enable_image_assets:
                try:
                    from provendoc.core.image_assets.schema import (
                        image_assets_metadata,
                    )
                    from provendoc.core.parsers.narrative.pptx_image_assets import (
                        extract_pptx_image_assets,
                    )

                    records = extract_pptx_image_assets(file_path)[
                        "records"
                    ]
                    asset_entry, asset_payloads = image_assets_metadata(
                        records, "pptx_image_assets_v1"
                    )
                    metadata["pptx_image_assets"] = asset_entry
                    if asset_payloads:
                        metadata["image_asset_payloads"] = asset_payloads
                except Exception as asset_error:
                    metadata["pptx_image_assets"] = {
                        "extractor": "pptx_image_assets_v1",
                        "error": f"{type(asset_error).__name__}: "
                                 f"{asset_error}",
                    }

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )

            # Sanitize invisible characters in Document.text only;
            # disclosed via metadata["sanitization"] + warning.
            sanitized_text, sanitization = sanitize_invisible_chars(text)
            if sanitization:
                metadata["sanitization"] = sanitization
                normalized = sum(
                    sanitization.get(key, 0)
                    for key in (
                        "zero_width_space", "bidi_resolved",
                        "bidi_unresolved", "zero_width", "mid_file_bom",
                        "ansi_escape", "control_char", "form_feed",
                    )
                )
                if normalized:
                    metadata.setdefault("warnings", []).append(
                        f"{normalized} invisible characters normalized "
                        "(see metadata.sanitization)"
                    )

            return Document(
                text=sanitized_text,
                metadata=metadata,
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            metadata["parse_success"] = False
            return Document(text="", metadata=metadata)

    # ========================================================
    # slide XML extraction
    # ========================================================

    @classmethod
    def _extract(cls, file_path, include_notes=True, limits=None):
        with zipfile.ZipFile(file_path) as zf:
            names = zf.namelist()

            # Declaration order; falls back to filename order.
            ordered_slides, order_method, orphans = (
                presentation_slide_order(zf, names)
            )

            stats = {
                "slides": len(ordered_slides), "titles": 0,
                "tables": 0, "images": 0,
                "charts": 0, "smartart": 0, "notes": 0,
                "videos": 0, "audios": 0, "ole_objects": 0,
                "charts_resolved": 0, "smartart_resolved": 0,
                "notes_related": 0, "notes_fallback": 0,
            # Guard channel: popped by parse(), not passed through.
                "guard_reasons": [], "guard_facts": [],
            }

            # p:modifyVerifier: travels back via stats, registered in
            # parse().
            modify_verifier = presentation_modify_verifier(zf, names)
            if modify_verifier is not None:
                stats["modify_verifier"] = modify_verifier

            lines = []      # [(text, is_heading_render)]
            headings = []
            # Object-level unresolved details, merged if non-empty.
            unresolved_records = []
            # Per-slide object probing records (not in narrative/stats).
            object_records = []
            # Per-slide start offsets in the joined text (same
            # convention as _candidate) for span facts.
            slide_starts = []  # [(position, file_number, part, title, start)]
            # p:sld@show raw values (absent = visible).
            show_attributes = {}
            # p:transition raw attributes (advTm = auto-advance ms,
            # spd = speed).
            transition_attributes = {}

            # Resource guard: slides beyond max_slides get no XML
            # parse / notes fetch; each is registered as a
            # SLIDES_NOT_EXTRACTED record + fact. stats["slides"]
            # stays the declared total (not faked), so the
            # empty-package check is unaffected; incompleteness is
            # expressed via resource_guard instead.
            max_slides = (
                limits.max_slides if limits is not None else None
            )
            max_shapes = (
                limits.max_shapes_per_slide
                if limits is not None else None
            )

            for position, (number, name) in enumerate(
                ordered_slides, start=1,
            ):
                if max_slides is not None and position > max_slides:
                    unresolved_records.append({
                        "object_type": "slide",
                        "object_id": f"slide-{position}",
                        "resolution": "partial",
                        "reason_code": "SLIDES_NOT_EXTRACTED",
                        "source": name,
                        "placeholder_text": "",
                    })
                    stats["guard_facts"].append({
                        "kind": "slides_not_extracted",
                        "slide_position": position,
                        "slide_number": number,
                        "part": name,
                    })
                    continue
                root = ElementTree.fromstring(zf.read(name))
                # p:sld@show raw value (absent = visible).
                show_attr = root.get("show")
                if show_attr is not None:
                    show_attributes[number] = show_attr
                # p:transition raw attributes, recorded verbatim.
                transition_el = root.find(
                    f"{{{_NS['p']}}}transition"
                )
                if transition_el is not None:
                    transition_attributes[number] = dict(
                        transition_el.attrib
                    )
                rels = slide_rels(zf, names, number)
                (
                    title, body_lines, slide_stats, slide_unresolved,
                ) = cls._slide_content(
                    root, zf, rels, position, name,
                    max_shapes=max_shapes, slide_number=number,
                    guard_facts=stats["guard_facts"],
                )
                unresolved_records.extend(slide_unresolved)
                # Object probing reuses the same root/rels.
                object_records.extend(cls._slide_object_facts(
                    root, zf, names, rels, position, name,
                    slide_number=number, max_shapes=max_shapes,
                ))
                stats["titles"] += 1 if title else 0
                stats["tables"] += slide_stats["tables"]
                stats["images"] += slide_stats["images"]
                stats["charts"] += slide_stats["charts"]
                stats["smartart"] += slide_stats["smartart"]
                stats["videos"] += slide_stats["videos"]
                stats["audios"] += slide_stats["audios"]
                stats["ole_objects"] += slide_stats["ole_objects"]
                stats["charts_resolved"] += slide_stats["charts_resolved"]
                stats["smartart_resolved"] += slide_stats[
                    "smartart_resolved"
                ]

                rendered_title = title or "(untitled)"
                # Rendered number = declared position.
                heading_line = f"## Slide {position}: {rendered_title}"
                slide_starts.append((
                    position, number, name, rendered_title,
                    sum(len(t) + 1 for t, _ in lines),
                ))
                headings.append(cls._candidate(
                    heading_line, lines, rendered_title,
                ))
                lines.append((heading_line, True))
                for line in body_lines:
                    lines.append((line, False))
                lines.append(("", False))

                if include_notes:
                    notes, notes_source = cls._notes_text(zf, names, number)
                    if notes_source == "relationship":
                        stats["notes_related"] += 1
                    elif notes_source == "filename_fallback":
                        stats["notes_fallback"] += 1
                    if notes:
                        stats["notes"] += 1
                        lines.append((f"Notes: {notes}", False))
                        lines.append(("", False))

        parts = [t for t, _ in lines]
        text = "\n".join(parts)

        # Slide i ends where slide i+1 starts; the last slide ends at
        # the text end.
        slides_meta = []
        for i, (position, number, name, rendered_title, start) in enumerate(
            slide_starts,
        ):
            end = (
                slide_starts[i + 1][4]
                if i + 1 < len(slide_starts)
                else len(text)
            )
            entry = {
                "slide_index": position,
                "slide_number": number,
                "part": name,
                "title": rendered_title,
                "start": start,
                "end": end,
            }
            # Explicit show attribute, recorded verbatim when present.
            show_attr = show_attributes.get(number)
            if show_attr is not None:
                entry["show"] = show_attr
            # p:transition attributes, recorded when present.
            slide_transition = transition_attributes.get(number)
            if slide_transition is not None:
                entry["transition"] = slide_transition
            slides_meta.append(entry)
        order_facts = {
            "method": order_method,
            "orphan_numbers": orphans,
        }
        # Coverage facts: referenced vs resolved pairs. Slides need no
        # pairing (every slide part renders); chart/smartart/notes rels
        # resolution is what can diverge.
        coverage = {
            "charts": {
                "referenced": stats["charts"],
                "resolved": stats["charts_resolved"],
            },
            "smartart": {
                "referenced": stats["smartart"],
                "resolved": stats["smartart_resolved"],
            },
            "notes": {
                "related": stats["notes_related"],
                "filename_fallback": stats["notes_fallback"],
                "extracted": stats["notes"],
            },
            "ole_objects": stats["ole_objects"],
            "video_refs": stats["videos"],
            "audio_refs": stats["audios"],
            "note": (
                "referenced-vs-resolved pairs; a resolved object had its "
                "part located and parsed; unsupported object kinds "
                "(OLE/media) are registered as placeholders, never "
                "silently dropped"
            ),
        }
        # Reasons aggregate one line per guard kind, in fixed order
        # (slides -> shapes).
        guard_reasons = []
        slides_skipped = [
            fact for fact in stats["guard_facts"]
            if fact["kind"] == "slides_not_extracted"
        ]
        if slides_skipped:
            guard_reasons.append(
                f"slides beyond {max_slides} not extracted "
                f"({len(slides_skipped)} skipped, total "
                f"{len(ordered_slides)}, max_slides={max_slides})"
            )
        shapes_capped = [
            fact for fact in stats["guard_facts"]
            if fact["kind"] == "shapes_not_extracted"
        ]
        if shapes_capped:
            guard_reasons.append(
                f"shapes beyond {max_shapes} per slide not extracted "
                f"({len(shapes_capped)} slides capped, "
                f"max_shapes_per_slide={max_shapes})"
            )
        stats["guard_reasons"] = guard_reasons

        return (
            text, headings, stats, slides_meta, order_facts, coverage,
            unresolved_records, object_records,
        )

    @staticmethod
    def _candidate(heading_line, lines, raw_title):
        offset = sum(len(t) + 1 for t, _ in lines)
        return HeadingCandidate(
            raw=raw_title,
            pattern="markdown",
            depth_hint=2,
            start=offset,
            end=offset + len(heading_line),
        )

    @classmethod
    def _slide_content(
        cls, root, zf, rels, position, part_name,
        max_shapes=None, slide_number=None, guard_facts=None,
    ):
        """One slide: title + body paragraphs + counts + unresolved
        object records.

        ``max_shapes`` caps per-slide shape objects: once the cap is
        hit the remaining objects on that slide are skipped and one
        partial record is registered. ``guard_facts`` is the facts
        list owned by ``_extract``; a shapes_not_extracted fact is
        appended when triggered.
        """
        title = None
        body_lines = []
        unresolved = []  # marker_contract.md §1.2 记录 (正文占位行不动)
        obj_seq = 0      # slide 内对象序 → object_id 稳定定位符

        shapes_processed = 0
        shapes_capped = False

        def _shape_slot():
            # Count at loop entries: None = no cap; 0 = cap everything.
            nonlocal shapes_processed, shapes_capped
            if max_shapes is None:
                return True
            if shapes_capped:
                return False
            if shapes_processed >= max_shapes:
                shapes_capped = True
                return False
            shapes_processed += 1
            return True

        def _record(object_type, resolution, reason_code,
                    placeholder_text, rel_id=None):
            nonlocal obj_seq
            obj_seq += 1
            entry = {
                "object_type": object_type,
                "object_id": f"slide-{position}-object-{obj_seq}",
                "resolution": resolution,
                "reason_code": reason_code,
                "source": part_name,
                "placeholder_text": placeholder_text,
            }
            if rel_id:
                entry["rel_id"] = rel_id
            unresolved.append(entry)

        stats = {
            "tables": 0, "images": 0,
            "charts": 0, "smartart": 0,
            "videos": 0, "audios": 0, "ole_objects": 0,
            "charts_resolved": 0, "smartart_resolved": 0,
        }

        for sp in iter_preferred(root, f"{{{_NS['p']}}}sp"):
            if not _shape_slot():
                continue
            ph_type = None
            nv = sp.find(f".//{{{_NS['p']}}}nvSpPr/{{{_NS['p']}}}nvPr/{{{_NS['p']}}}ph")
            if nv is not None:
                ph_type = nv.get("type")
            paragraphs = cls._sp_paragraphs(sp)
            if not paragraphs:
                continue
            if ph_type in _TITLE_PH_TYPES and title is None:
                title = " ".join(
                    t for t in paragraphs if t.strip()
                ).strip() or None
                continue
            body_lines.extend(paragraphs)
            body_lines.append("")

        for tbl in iter_preferred(root, f"{{{_NS['a']}}}tbl"):
            if not _shape_slot():
                continue
            stats["tables"] += 1
            body_lines.extend(cls._table_lines(tbl))
            body_lines.append("")

        for pic in iter_preferred(root, f"{{{_NS['p']}}}pic"):
            if not _shape_slot():
                continue
            # Media p:pic is registered separately, not in image_count.
            is_media = (
                pic.find(f"{{{_NS['a']}}}videoFile") is not None
                or pic.find(f"{{{_NS['a']}}}audioFile") is not None
            )
            if not is_media:
                stats["images"] += 1

        # Media/OLE (UNSUPPORTED-BUT-RECORDED): content not extracted,
        # placeholder line + count; media labels come from rels targets.
        for video_file in iter_preferred(root, f"{{{_NS['a']}}}videoFile"):
            if not _shape_slot():
                continue
            stats["videos"] += 1
            rid = video_file.get(f"{{{_NS['r']}}}link") or video_file.get(
                f"{{{_NS['r']}}}embed"
            )
            target = rels.get(rid) if rid else None
            label = (target or "").rsplit("/", 1)[-1]
            placeholder = f"[Video: {label}]" if label else "[Video]"
            body_lines.append(placeholder)
            body_lines.append("")
            _record(
                "video", "unsupported",
                "MEDIA_REFERENCED_NOT_EXTRACTED", placeholder,
                rel_id=rid,
            )
        for audio_file in iter_preferred(root, f"{{{_NS['a']}}}audioFile"):
            if not _shape_slot():
                continue
            stats["audios"] += 1
            rid = audio_file.get(f"{{{_NS['r']}}}link") or audio_file.get(
                f"{{{_NS['r']}}}embed"
            )
            target = rels.get(rid) if rid else None
            label = (target or "").rsplit("/", 1)[-1]
            placeholder = f"[Audio: {label}]" if label else "[Audio]"
            body_lines.append(placeholder)
            body_lines.append("")
            _record(
                "audio", "unsupported",
                "MEDIA_REFERENCED_NOT_EXTRACTED", placeholder,
                rel_id=rid,
            )
        for ole_obj in iter_preferred(root, f"{{{_NS['p']}}}oleObj"):
            if not _shape_slot():
                continue
            stats["ole_objects"] += 1
            prog_id = ole_obj.get("progId")
            placeholder = (
                f"[OLE object: {prog_id}]" if prog_id else "[OLE object]"
            )
            body_lines.append(placeholder)
            body_lines.append("")
            _record(
                "ole", "unsupported", "OLE_OBJECT_UNSUPPORTED", placeholder,
            )

        # Charts: a:graphicFrame -> c:chart -> slide rels -> chart xml.
        chart_lines, chart_count, charts_resolved = cls._chart_lines(
            root, zf, rels, record=_record, shape_slot=_shape_slot,
        )
        stats["charts"] += chart_count
        stats["charts_resolved"] += charts_resolved
        body_lines.extend(chart_lines)

        # SmartArt: a:graphicFrame -> dgm:relIds -> diagrams/data xml.
        smartart_lines, smartart_count, smartart_resolved = (
            cls._smartart_lines(
                root, zf, rels, record=_record, shape_slot=_shape_slot,
            )
        )
        stats["smartart"] += smartart_count
        stats["smartart_resolved"] += smartart_resolved
        body_lines.extend(smartart_lines)

        if shapes_capped:
            # One partial record per capped slide.
            unresolved.append({
                "object_type": "slide_shapes",
                "object_id": (
                    f"slide-{position}-shapes-after-{shapes_processed}"
                ),
                "resolution": "partial",
                "reason_code": "SHAPES_NOT_EXTRACTED",
                "source": part_name,
                "placeholder_text": "",
            })
            if guard_facts is not None:
                guard_facts.append({
                    "kind": "shapes_not_extracted",
                    "slide_position": position,
                    "slide_number": slide_number,
                    "part": part_name,
                    "shapes_processed": shapes_processed,
                    "max_shapes_per_slide": max_shapes,
                })

        # Collapse consecutive blank lines.
        cleaned = []
        for line in body_lines:
            if not line.strip() and (not cleaned or not cleaned[-1].strip()):
                continue
            cleaned.append(line)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()

        return title, cleaned, stats, unresolved

    @staticmethod
    def _slide_object_facts(
        root, zf, names, rels, position, part_name,
        slide_number=None, max_shapes=None,
    ):
        """Per-slide object probing: flat records (kind + slide origin
        + object fields) for metadata["pptx_slide_objects"].

        Read-only on the same ``root``; narrative and stats are
        untouched. A shape without placeholder but with text is a
        text_box, without text a shape; title paragraphs are not
        probed. ``max_shapes`` has the same cap semantics as the
        narrative path. Notes existence is judged from rels only.
        """
        records = []
        obj_seq = 0
        shapes_processed = 0
        shapes_capped = False

        def _shape_slot():
            nonlocal shapes_processed, shapes_capped
            if max_shapes is None:
                return True
            if shapes_capped:
                return False
            if shapes_processed >= max_shapes:
                shapes_capped = True
                return False
            shapes_processed += 1
            return True

        def _record(kind, **fields):
            nonlocal obj_seq
            obj_seq += 1
            entry = {
                "kind": kind,
                "slide_index": position,
                "slide_number": slide_number,
                "part": part_name,
                "object_seq": obj_seq,
            }
            entry.update(fields)
            records.append(entry)

        # Raw external targets: slide_rels normalizes all targets to
        # in-zip part paths (external URLs would be distorted), so
        # TargetMode=External entries are read separately here.
        external_targets = {}
        rels_name = f"ppt/slides/_rels/slide{slide_number}.xml.rels"
        if rels_name in names:
            try:
                rels_root = ElementTree.fromstring(zf.read(rels_name))
                for rel in rels_root:
                    if (
                        rel.get("TargetMode") == "External"
                        and rel.get("Id") and rel.get("Target")
                    ):
                        external_targets[rel.get("Id")] = (
                            rel.get("Target")
                        )
            except (KeyError, ElementTree.ParseError):
                pass

        def _target_for(rid):
            # External targets verbatim; internal parts via slide_rels.
            if rid in external_targets:
                return external_targets[rid]
            return rels.get(rid)

        def _hyperlinks(container):
            for hl in container.iter(f"{{{_NS['a']}}}hlinkClick"):
                rid = hl.get(f"{{{_NS['r']}}}id")
                fields = {}
                if rid:
                    fields["rel_id"] = rid
                    target = _target_for(rid)
                    if target:
                        fields["target"] = target
                _record("hyperlink", **fields)

        for sp in iter_preferred(root, f"{{{_NS['p']}}}sp"):
            if not _shape_slot():
                continue
            ph = sp.find(
                f".//{{{_NS['p']}}}nvSpPr/{{{_NS['p']}}}nvPr/"
                f"{{{_NS['p']}}}ph"
            )
            ph_type = ph.get("type") if ph is not None else None
            is_title = ph_type in _TITLE_PH_TYPES
            para_count = 0
            if not is_title:
                for para in iter_preferred(sp, f"{{{_NS['a']}}}p"):
                    runs = []
                    for node in para.iter():
                        name = _localname(node.tag)
                        if name == "t" and node.text:
                            runs.append(node.text)
                        elif name == "br":
                            runs.append(" ")
                    text = "".join(runs).strip()
                    if not text:
                        continue
                    fields = {}
                    marker = None
                    marker_kind = None
                    pPr = para.find(f"{{{_NS['a']}}}pPr")
                    if pPr is not None:
                        bu = pPr.find(f"{{{_NS['a']}}}buChar")
                        if bu is not None:
                            marker = bu.get("char")
                            marker_kind = "buChar"
                        else:
                            bu = pPr.find(f"{{{_NS['a']}}}buAutoNum")
                            if bu is not None:
                                marker = bu.get("type")
                                marker_kind = "buAutoNum"
                    if marker is not None:
                        fields["list_marker"] = marker
                    if ph_type:
                        fields["placeholder_type"] = ph_type
                    _record("paragraph", text=text, **fields)
                    para_count += 1
                    if marker_kind is not None:
                        _record(
                            "list_item", text=text, marker=marker,
                            marker_kind=marker_kind,
                        )
                if ph_type is None:
                    if para_count:
                        _record(
                            "text_box", paragraph_count=para_count,
                        )
                    else:
                        _record("shape")
            # Hyperlinks are probed in title subtrees too.
            _hyperlinks(sp)

        for tbl in iter_preferred(root, f"{{{_NS['a']}}}tbl"):
            if not _shape_slot():
                continue
            rows = list(iter_preferred(tbl, f"{{{_NS['a']}}}tr"))
            row_entries = []
            cells_total = 0
            for row_index, tr in enumerate(rows, start=1):
                tcs = list(iter_preferred(tr, f"{{{_NS['a']}}}tc"))
                nonempty = 0
                cell_entries = []
                for cell_index, tc in enumerate(tcs, start=1):
                    texts = [
                        t.text for t in iter_preferred(
                            tc, f"{{{_NS['a']}}}t"
                        ) if t.text
                    ]
                    value = " ".join(texts).strip()
                    if value:
                        nonempty += 1
                    cell_entries.append((cell_index, value))
                row_entries.append((row_index, nonempty, cell_entries))
                cells_total += len(tcs)
            _record(
                "table", row_count=len(rows), cell_count=cells_total,
            )
            for row_index, nonempty, cell_entries in row_entries:
                _record(
                    "table_row", row_index=row_index,
                    nonempty_cell_count=nonempty,
                )
                for cell_index, value in cell_entries:
                    _record(
                        "table_cell", row_index=row_index,
                        cell_index=cell_index, text=value,
                    )

        for pic in iter_preferred(root, f"{{{_NS['p']}}}pic"):
            if not _shape_slot():
                continue
            is_media = (
                pic.find(f"{{{_NS['a']}}}videoFile") is not None
                or pic.find(f"{{{_NS['a']}}}audioFile") is not None
            )
            if not is_media:
                blip = pic.find(f".//{{{_NS['a']}}}blip")
                rid = (
                    blip.get(f"{{{_NS['r']}}}embed")
                    if blip is not None else None
                )
                fields = {}
                if rid:
                    fields["rel_id"] = rid
                    target = _target_for(rid)
                    if target:
                        fields["target"] = target
                _record("image", **fields)
            _hyperlinks(pic)

        # Notes existence from rels, same channel as _notes_text.
        if rels_name in names:
            notes_name = notes_slide_part(zf, names, slide_number)
            channel = "relationship"
        else:
            notes_name = (
                f"ppt/notesSlides/notesSlide{slide_number}.xml"
            )
            channel = "filename_fallback"
        if notes_name and notes_name in names:
            _record("notes", channel=channel, notes_part=notes_name)

        return records

    @staticmethod
    def _sp_paragraphs(sp):
        """One line per a:p in a shape (a:t joined, a:br folded)."""
        paragraphs = []
        for p in iter_preferred(sp, f"{{{_NS['a']}}}p"):
            runs = []
            for node in p.iter():
                name = _localname(node.tag)
                if name == "t" and node.text:
                    runs.append(node.text)
                elif name == "br":
                    runs.append(" ")
            text = "".join(runs).strip()
            if text:
                paragraphs.append(text)
        return paragraphs

    @staticmethod
    def _table_lines(tbl):
        rows = []
        for tr in iter_preferred(tbl, f"{{{_NS['a']}}}tr"):
            cells = []
            for tc in iter_preferred(tr, f"{{{_NS['a']}}}tc"):
                cell_text = []
                for t in iter_preferred(tc, f"{{{_NS['a']}}}t"):
                    if t.text:
                        cell_text.append(t.text)
                value = " ".join(cell_text).strip()
                cells.append(value.replace("|", "\\|"))
            if any(c for c in cells):
                rows.append(cells)
        # Table rendering (padding + separator) is shared in
        # narrative/shared.py.
        return _markdown_table_lines(rows)

    # ========================================================
    # slide rels / charts / SmartArt
    # ========================================================

    # slide rels parsing lives in pptx_evidence.slide_rels (shared).

    @classmethod
    def _chart_lines(cls, root, zf, rels, record=None, shape_slot=None):
        """Chart facts: c:chart references -> title placeholder lines.

        The chart title lives in c:tx -> c:rich; without c:tx an
        untitled placeholder is used (no guessing). Returns
        (lines, referenced, resolved): resolved = chart part
        located and parsed. Unresolved objects go through the
        ``record`` callback; ``shape_slot`` gates the per-slide
        shape cap.
        """
        lines = []
        referenced = 0
        resolved = 0
        chart_tag = f"{{{_NS['c']}}}chart"
        for chart_ref in iter_preferred(root, chart_tag):
            if shape_slot is not None and not shape_slot():
                continue
            rid = chart_ref.get(f"{{{_NS['r']}}}id")
            referenced += 1
            part = rels.get(rid) if rid else None
            title, ok = cls._chart_title(zf, part)
            if ok:
                resolved += 1
            elif record is not None:
                reason = (
                    "CHART_PART_MISSING"
                    if part is None or part not in zf.namelist()
                    else "CHART_DATA_UNREADABLE"
                )
                record(
                    "chart", "unresolved", reason, "[Chart]", rel_id=rid,
                )
            lines.append(f"[Chart: {title}]" if title else "[Chart]")
            lines.append("")
        return lines, referenced, resolved

    @staticmethod
    def _chart_title(zf, part):
        """chart xml -> (explicit title, resolved).

        resolved = part located and XML parsed; having no title is
        a content fact, separate from parse success. Without c:tx
        the title is None (no guessing).
        """
        if not part:
            return None, False
        try:
            root = ElementTree.fromstring(zf.read(part))
        except (KeyError, ElementTree.ParseError):
            return None, False
        tx = root.find(
            f".//{{{_NS['c']}}}tx/{{{_NS['c']}}}rich",
        )
        if tx is None:
            return None, True
        parts = [
            t.text for t in iter_preferred(tx, f"{{{_NS['a']}}}t") if t.text
        ]
        return " ".join(parts).strip() or None, True

    @classmethod
    def _smartart_lines(cls, root, zf, rels, record=None, shape_slot=None):
        """SmartArt facts: dgm:relIds (r:dm) -> diagrams/data xml text.

        Visible text (dgm:pt -> dgm:t -> a:t) renders as "- item"
        list lines; missing/broken data parts become placeholders.
        Returns (lines, referenced, resolved); unresolved objects
        go through the ``record`` callback and ``shape_slot`` gates
        the per-slide shape cap.
        """
        lines = []
        referenced = 0
        resolved = 0
        rel_ids_tag = f"{{{_NS['dgm']}}}relIds"
        for rel_ids in iter_preferred(root, rel_ids_tag):
            if shape_slot is not None and not shape_slot():
                continue
            referenced += 1
            dm = rel_ids.get(f"{{{_NS['r']}}}dm")
            data_part = rels.get(dm) if dm else None
            items, ok = cls._smartart_items(zf, data_part)
            if ok:
                resolved += 1
            elif record is not None:
                reason = (
                    "SMARTART_PART_MISSING"
                    if data_part is None or data_part not in zf.namelist()
                    else "SMARTART_DATA_UNREADABLE"
                )
                record(
                    "smartart", "unresolved", reason,
                    "[SmartArt diagram]", rel_id=dm,
                )
            if items:
                lines.append("[SmartArt diagram]")
                lines.extend(f"- {item}" for item in items)
            else:
                lines.append("[SmartArt diagram]")
            lines.append("")
        return lines, referenced, resolved

    @staticmethod
    def _smartart_items(zf, part):
        """diagrams/data xml -> (visible text items, resolved)."""
        if not part:
            return [], False
        try:
            root = ElementTree.fromstring(zf.read(part))
        except (KeyError, ElementTree.ParseError):
            return [], False
        items = []
        # Semantic points only (dgm:pt with modelId); text sits in
        # child dgm:t a:t elements.
        for pt in iter_preferred(root, f"{{{_NS['dgm']}}}pt"):
            texts = [
                t.text
                for t in iter_preferred(pt, f"{{{_NS['a']}}}t")
                if t.text
            ]
            value = " ".join(texts).strip()
            if value:
                items.append(value)
        return items, True

    # ========================================================
    # chart / SmartArt structured fact channels
    # ========================================================

    @staticmethod
    def _slide_part_positions(file_path):
        """Declaration order [(position, file_number, part)]."""
        with zipfile.ZipFile(file_path) as zf:
            ordered, _, _ = presentation_slide_order(zf, zf.namelist())
        return list(enumerate(ordered, start=1))

    @classmethod
    def _extract_charts(cls, file_path, budget=None):
        """Chart parts referenced by slides -> structured
        series/category/value facts.

        Each fact: {slide_index, slide_number, part, title,
        series}; series = [{name, categories, values}] with raw c:v
        strings (no normalization, no type guessing).

        max_slides is reused; each chart part read checks the
        shared budget and stops early at the cap.
        """
        charts = []
        with zipfile.ZipFile(file_path) as zf:
            names = zf.namelist()
            for position, (number, name) in cls._slide_part_positions(
                file_path,
            ):
                if budget is not None and not budget.slide_allowed(
                    position,
                ):
                    continue
                try:
                    root = ElementTree.fromstring(zf.read(name))
                except (KeyError, ElementTree.ParseError):
                    continue
                rels = slide_rels(zf, names, number)
                for chart_ref in iter_preferred(
                    root, f"{{{_NS['c']}}}chart",
                ):
                    rid = chart_ref.get(f"{{{_NS['r']}}}id")
                    part = rels.get(rid) if rid else None
                    if not part:
                        continue
                    if budget is not None and not budget.take(
                        "charts", name,
                    ):
                        break
                    title, series, extras = cls._chart_data(zf, part)
                    if title is None and not series and not extras:
                        continue
                    charts.append({
                        "slide_index": position,
                        "slide_number": number,
                        "part": part,
                        "title": title,
                        "series": series,
                        **extras,
                    })
        return charts

    @staticmethod
    def _chart_part_rels(zf, part):
        """A chart part's own rels (.../_rels/chartN.xml.rels) -> rId
        -> relationship facts.

        c:externalData workbook links live here; unreadable or
        missing rels yield an empty mapping.
        """
        directory, _, base = part.rpartition("/")
        rels_name = f"{directory}/_rels/{base}.rels"
        mapping = {}
        try:
            if rels_name in zf.namelist():
                root = ElementTree.fromstring(zf.read(rels_name))
                for rel in root:
                    mapping[rel.get("Id") or ""] = {
                        "target": rel.get("Target") or "",
                        "target_mode": rel.get("TargetMode") or "",
                    }
        except (KeyError, ElementTree.ParseError):
            pass
        return mapping

    @staticmethod
    def _chart_data(zf, part):
        """chart xml -> (title, series list, extra facts).

        Series (c:ser): name (c:tx literal or strCache),
        categories (c:cat cache points by idx), values (c:val
        numCache points). All c:v strings are kept verbatim;
        dimensions without caches are omitted. c:f reference
        strings (including external [Book]! refs) are recorded per
        series, never evaluated.

        Extra facts: the c:externalData workbook link (resolved via
        the chart part's own rels, never followed) and the
        c:extLst extension inventory (uri + capped text excerpt).
        """
        try:
            root = ElementTree.fromstring(zf.read(part))
        except (KeyError, ElementTree.ParseError):
            return None, [], {}
        title = None
        tx = root.find(f".//{{{_NS['c']}}}tx/{{{_NS['c']}}}rich")
        if tx is not None:
            parts = [
                t.text for t in iter_preferred(tx, f"{{{_NS['a']}}}t") if t.text
            ]
            title = " ".join(parts).strip() or None
        series = []
        for ser in iter_preferred(root, f"{{{_NS['c']}}}ser"):
            name = None
            tx = ser.find(f"{{{_NS['c']}}}tx")
            if tx is not None:
                texts = [
                    node.text
                    for node in iter_preferred(tx, f"{{{_NS['c']}}}v")
                    if node.text
                ]
                if not texts:
                    texts = [
                        node.text
                        for node in iter_preferred(tx, f"{{{_NS['a']}}}t")
                        if node.text
                    ]
                name = " ".join(texts).strip() or None
            entry = {"name": name}
            if tx is not None:
                tx_formula = (tx.findtext(f".//{{{_NS['c']}}}f") or "").strip()
                if tx_formula:
                    entry["name_formula"] = tx_formula
            for formula_key, dimension_tag in (
                ("category_formula", "cat"),
                ("value_formula", "val"),
            ):
                dimension = ser.find(f"{{{_NS['c']}}}{dimension_tag}")
                if dimension is None:
                    continue
                formula = (dimension.findtext(f".//{{{_NS['c']}}}f") or "").strip()
                if formula:
                    entry[formula_key] = formula
            categories = PptxParser._cache_points(
                ser.find(f"{{{_NS['c']}}}cat"),
            )
            if categories is not None:
                entry["categories"] = categories
            values = PptxParser._cache_points(
                ser.find(f"{{{_NS['c']}}}val"),
            )
            if values is not None:
                entry["values"] = values
            if name is None and not entry.get("values"):
                continue
            series.append(entry)

        extras = {}
        external_data = root.find(f"{{{_NS['c']}}}externalData")
        if external_data is not None:
            rid = external_data.get(f"{{{_NS['r']}}}id") or ""
            rel = PptxParser._chart_part_rels(zf, part).get(rid)
            auto_update = external_data.find(f"{{{_NS['c']}}}autoUpdate")
            extras["external_data"] = {
                "rel_id": rid,
                "target": rel["target"] if rel else "",
                "target_mode": rel["target_mode"] if rel else "",
                "rel_resolved": rel is not None,
                "auto_update": (
                    auto_update.get("val") if auto_update is not None else None
                ),
            }
        ext_lst = root.find(f"{{{_NS['c']}}}extLst")
        if ext_lst is not None:
            extensions = ext_lst.findall(f"{{{_NS['c']}}}ext")
            extras["extension_list"] = {
                "extension_count": len(extensions),
                "extensions": [
                    {
                        "uri": extension.get("uri") or "",
                        "content_excerpt": "".join(
                            node.text or "" for node in extension.iter()
                        ).strip()[:200],
                    }
                    for extension in extensions[:10]
                ],
            }
        return title, series, extras

    @staticmethod
    def _cache_points(dimension):
        """c:cat/c:val -> [c:v strings] by idx; None without caches."""
        if dimension is None:
            return None
        points = []
        for pt in iter_preferred(dimension, f"{{{_NS['c']}}}pt"):
            v = pt.find(f"{{{_NS['c']}}}v")
            if v is None or v.text is None:
                continue
            try:
                idx = int(pt.get("idx"))
            except (TypeError, ValueError):
                idx = len(points)
            points.append((idx, v.text))
        if not points:
            return None
        points.sort(key=lambda item: item[0])
        return [value for _, value in points]

    @classmethod
    def _extract_smartart(cls, file_path, budget=None):
        """Diagram data parts referenced by slides -> tree facts.

        Each fact: {slide_index, slide_number, part, nodes}; node =
        {model_id, text, parent_ids}: modelId from dgm:pt, parents
        from dgm:cxnLst parOf connections (srcId=child,
        dstId=parent). Same budget as _extract_charts
        (channel="smartart").
        """
        diagrams = []
        with zipfile.ZipFile(file_path) as zf:
            names = zf.namelist()
            for position, (number, name) in cls._slide_part_positions(
                file_path,
            ):
                if budget is not None and not budget.slide_allowed(
                    position,
                ):
                    continue
                try:
                    root = ElementTree.fromstring(zf.read(name))
                except (KeyError, ElementTree.ParseError):
                    continue
                rels = slide_rels(zf, names, number)
                for rel_ids in iter_preferred(
                    root, f"{{{_NS['dgm']}}}relIds",
                ):
                    dm = rel_ids.get(f"{{{_NS['r']}}}dm")
                    part = rels.get(dm) if dm else None
                    if not part:
                        continue
                    if budget is not None and not budget.take(
                        "smartart", name,
                    ):
                        break
                    nodes = cls._smartart_nodes(zf, part)
                    if nodes:
                        diagrams.append({
                            "slide_index": position,
                            "slide_number": number,
                            "part": part,
                            "nodes": nodes,
                        })
        return diagrams

    @staticmethod
    def _smartart_nodes(zf, part):
        """diagrams/data xml -> tree nodes (modelId + parents)."""
        try:
            root = ElementTree.fromstring(zf.read(part))
        except (KeyError, ElementTree.ParseError):
            return []
        texts: dict[str, str] = {}
        for pt in iter_preferred(root, f"{{{_NS['dgm']}}}pt"):
            model_id = pt.get("modelId")
            if not model_id:
                continue
            parts = [
                t.text
                for t in iter_preferred(pt, f"{{{_NS['a']}}}t")
                if t.text
            ]
            texts[model_id] = " ".join(parts).strip()
        parents: dict[str, list[str]] = {}
        for cxn_lst in root.iter(f"{{{_NS['dgm']}}}cxnLst"):
            for cxn in cxn_lst.findall(f"{{{_NS['dgm']}}}cxn"):
                if (cxn.get("type") or "parOf") != "parOf":
                    continue
                src = cxn.get("srcId")
                dst = cxn.get("dstId")
                if not src or not dst:
                    continue
                parents.setdefault(src, []).append(dst)
        return [
            {
                "model_id": model_id,
                "text": text,
                "parent_ids": parents.get(model_id, []),
            }
            for model_id, text in texts.items()
        ]

    # ========================================================
    # OMML formula fact channel (shared with docx)
    # ========================================================

    @classmethod
    def _extract_formulas(cls, file_path, budget=None):
        """Embedded m:oMath in slides -> structured formula facts.

        Each fact: {slide_index, slide_number, part, display,
        text, structure, mathml, mathml_unsupported_elements} --
        the same fact shape as docx_formulas (linear text /
        structure counts / MathML subset conversion with a
        degradation list). Formulas sit inside a:p (usually via
        a14:m extension wrappers). stdlib ElementTree has no
        iterancestors, so display is resolved via a parent map and
        passed to omml_to_mathml explicitly.

        Same budget as _extract_charts (channel="formulas").
        """
        formulas = []
        with zipfile.ZipFile(file_path) as zf:
            for position, (number, name) in cls._slide_part_positions(
                file_path,
            ):
                if budget is not None and not budget.slide_allowed(
                    position,
                ):
                    continue
                try:
                    root = ElementTree.fromstring(zf.read(name))
                except (KeyError, ElementTree.ParseError):
                    continue
                parents = {
                    child: parent
                    for parent in root.iter()
                    for child in parent
                }
                for omath in iter_preferred(root, OMATH):
                    if budget is not None and not budget.take(
                        "formulas", name,
                    ):
                        break
                    linear = omml_linear_text(omath)
                    if not linear.strip():
                        continue
                    display = cls._in_omath_para(omath, parents)
                    mathml, unsupported = omml_to_mathml(
                        omath, display=display,
                    )
                    formulas.append({
                        "slide_index": position,
                        "slide_number": number,
                        "part": name,
                        "display": display,
                        "text": linear,
                        "structure": omml_structure_counts(omath),
                        "mathml": mathml,
                        "mathml_unsupported_elements": unsupported,
                    })
        return formulas

    @staticmethod
    def _in_omath_para(element, parents):
        """Whether the element is inside an m:oMathPara."""
        node = parents.get(element)
        while node is not None:
            if node.tag == OMATH_PARA:
                return True
            node = parents.get(node)
        return False

    @staticmethod
    def _notes_text(zf, names, slide_number):
        """Notes text -> (text, source).

        source records how the notes were located:
            "relationship"      -- via slide rels .../notesSlide
            "filename_fallback" -- rels missing, filename hit
            None                -- no notes

        A present rels file is authoritative: no notesSlide relation
        means no notes (prevents double extraction via the filename
        convention); the filename convention is used only when the
        rels file is missing.
        """
        rels_name = f"ppt/slides/_rels/slide{slide_number}.xml.rels"
        if rels_name in names:
            notes_name = notes_slide_part(zf, names, slide_number)
            if notes_name is None:
                return "", None
        else:
            notes_name = (
                f"ppt/notesSlides/notesSlide{slide_number}.xml"
            )
        if notes_name not in names:
            return "", None
        try:
            root = ElementTree.fromstring(zf.read(notes_name))
        except (KeyError, ElementTree.ParseError):
            return "", None
        parts = []
        for t in iter_preferred(root, f"{{{_NS['a']}}}t"):
            if t.text:
                parts.append(t.text)
        text = " ".join(parts).strip()
        if rels_name in names:
            return text, "relationship"
        return text, "filename_fallback"
