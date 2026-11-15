# ============================================================
# Narrative Domain — RTF boundary adapter layer (adapters)
# ============================================================
# Mechanically split off rtf_parser.py on 2026-11-10 (line-identical).
# RtfParser / RtxParser / RtfdParser are three BaseParser adapters for
# parser_registry and the Document contract. State machine in scanner.py,
# fact tables in facts.py.

import hashlib
import time
from pathlib import Path
from typing import Any

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts as _build_carrier_facts,
)
from provendoc.core.diagnostics import (
    detect_security_carriers as _detect_security_carriers,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.parsers.narrative.rtf.facts import (
    _RTF_HEAD,
)
from provendoc.core.parsers.narrative.rtf.scanner import (
    _extract_rtf,
)
from provendoc.core.text.normalization import (
    sanitize_invisible_chars as _sanitize_invisible_chars,
)


class RtfParser(BaseParser):
    """RTF parser: pure-stdlib state machine extracting rendered text + structural evidence registration.

    Text extraction semantics: docx_rtf_prereg_v1 §2.2 (frozen in v1,
    unchanged in v2): group stack + destination skipping + \\uN/\\ucN +
    \\'hh decoded per the declared codepage + \\binN character-level
    skipping + fldrslt display text kept. v2 structural evidence
    (rtf_family_prereg_v1 D1-D13): see the module header comments.
    """

    def __init__(self, encoding: str = "utf-8", tolerant: bool = True):
        """Initialize with encoding and tolerant switches (signature same as TexParser)."""
        self.encoding = encoding
        self.tolerant = tolerant

    def parse(self, file_path) -> Document:
        """Parse .rtf into a Document (rendered-text extraction + rtf_structure_v2 registration)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "rtf"
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "rtf", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            # Head check (v1 contract): non-RTF files fail honestly —
            # no rtf_structure, empty text. .rtx content sniffing goes
            # through the same gate (D3 A: a missing {\rtf head registers
            # format_mismatch).
            if not text.startswith(_RTF_HEAD):
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "not an RTF document: source does not start "
                    "with '{\\rtf' (format mismatch honest failure)"
                )
                metadata["parse_duration_ms"] = round(
                    (time.perf_counter() - start_time) * 1000, 3,
                )
                return Document(text="", metadata=metadata)

            extracted, structure, pict_records = _extract_rtf(text)
            # Truncation/unbalance is not a parse_failed: the extracted
            # text is kept best-effort; facts go to
            # structure["unbalanced_groups"].
            metadata["rtf_structure"] = structure
            # D14: heading_candidates promoted (pre-P0 = [] — "don't
            # guess from unresolved styles"; from P1 extracted honestly
            # once styles/outlinelev declarations can prove it)
            metadata["heading_candidates"] = structure.pop(
                "heading_candidates", []
            )

            # D4 image assets (same schema as docx_image_assets: facts
            # and byte payloads separated; payloads stay in memory and are
            # stripped before serialization).
            if pict_records:
                from provendoc.core.image_assets.schema import (
                    image_assets_metadata,
                )

                asset_entry, asset_payloads = image_assets_metadata(
                    pict_records, "rtf_image_assets_v1",
                )
                metadata["rtf_image_assets"] = asset_entry
                if asset_payloads:
                    metadata["image_asset_payloads"] = asset_payloads

            # Security-carrier census (invisible_carrier_v1, prereg F2b;
            # same bypass as the epub contract): literal carriers in the
            # source view (RTF control flow) are already censused/
            # sanitized by the base channel when rtf rides it; carriers
            # materialized by RTF \\uN escapes are only visible in the
            # extracted rendered-text view — base sanitization precedes
            # extraction and the two views never double-count; counts are
            # merged by key sum (no value invented without evidence, no
            # evidence lost). Non-zero structured counts land in
            # metadata["security_carriers"], fact codes appended to warnings.
            _carrier_census = _detect_security_carriers(extracted)
            if _carrier_census:
                _merged_census = dict(metadata.get("security_carriers") or {})
                for _key, _count in _carrier_census.items():
                    _merged_census[_key] = _merged_census.get(_key, 0) + _count
                metadata["security_carriers"] = _merged_census
                metadata.setdefault("warnings", []).extend(
                    _build_carrier_facts(_carrier_census)
                )

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            # Working-text sanitization (epub contract): normalize
            # Document.text only; source_text keeps the extracted raw view
            # (ctx.source_text); structural/image facts were already
            # derived above from the original view. When the base channel
            # already registered source-view sanitization, the extraction
            # view counts merge by key sum (the two views never overlap).
            sanitized_extracted, _extract_sanitization = (
                _sanitize_invisible_chars(extracted)
            )
            if _extract_sanitization:
                _prior_sanitization = dict(metadata.get("sanitization") or {})
                for _key, _count in _extract_sanitization.items():
                    if isinstance(_count, int):
                        _prior_sanitization[_key] = (
                            _prior_sanitization.get(_key, 0) + _count
                        )
                metadata["sanitization"] = _prior_sanitization
                _normalized = sum(
                    _extract_sanitization.get(key, 0)
                    for key in (
                        "zero_width_space", "bidi_resolved",
                        "bidi_unresolved", "zero_width", "mid_file_bom",
                        "ansi_escape", "control_char", "form_feed",
                    )
                )
                if _normalized:
                    metadata.setdefault("warnings", []).append(
                        f"{_normalized} invisible characters normalized "
                        "(see metadata.sanitization)"
                    )
            return Document(
                text=sanitized_extracted,
                metadata=metadata,
                source_text=ctx.source_text,
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            return Document(text="", metadata=metadata)


class RtxParser(RtfParser):
    """D3 A: .rtx content-sniffing route (rtf_family_prereg_v1, decided).

    ``{\\rtf`` head hit → full RTF channel (parent parse:
    complete rtf_structure_v2 evidence); miss → treated as text
    (TxtParser semantics) plus a ``format_mismatch`` evidence. A
    declaration mismatch is a document fact, not a failure: text is
    extracted as usual and the mismatch evidence is for audit (sniffing
    only routes; parsing semantics on both sides stay untouched).
    """

    def parse(self, file_path) -> Document:
        """Parse .rtx: sniff the content, then route to the RTF or text channel."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "rtx"
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "rtx", self.encoding, self.tolerant
            )
            if ctx.text.startswith(_RTF_HEAD):
                # Sniff hit: full RTF channel (parent implementation includes the head check)
                return RtfParser.parse(self, file_path)

            # No {\rtf head: treat as text + format_mismatch evidence
            # (D3 A). TxtParser produces the full text-channel evidence;
            # file_type is restored to the declared suffix fact (TxtParser
            # records "text" by default).
            from provendoc.core.parsers.narrative.txt_parser import (
                TxtParser,
            )

            document = TxtParser(
                encoding=self.encoding, tolerant=self.tolerant,
            ).parse(file_path)
            document.metadata["file_type"] = "rtx"
            document.metadata["format_mismatch"] = {
                "declared_suffix": ".rtx",
                "expected_head": _RTF_HEAD,
                "observed_head": ctx.text[: len(_RTF_HEAD)],
                "routed_to": "text",
                "reason": "rtx_content_sniff_miss",
            }
            document.metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return document
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            return Document(text="", metadata=metadata)


class RtfdParser(BaseParser):
    """D19 A: .rtfd directory bundle (rtf_family_prereg_v2, decided).

    A .rtfd is a directory bundle: the main document ``TXT.rtf`` goes
    through the full ``RtfParser`` channel (rtf_structure_v2 structural
    evidence + the image asset inventory); every other file gets a
    sha256, reconciled against the main document's pict asset
    ``asset_id`` (``img_`` + sha256[:12], same schema as
    docx_image_assets) — hit → ``attachment`` (image resource actually
    referenced by the main document), miss → ``orphan`` (bundle file
    not referenced by the main document); known noise (.DS_Store/
    Thumbs.db) is still registered with role ``bundle_noise``. RTFD has
    no manifest: orphan judgement goes by main-document references
    (honest boundary, no speculative matching).
    """

    def __init__(self, encoding: str = "utf-8",
                 tolerant: bool = True) -> None:
        """Same parameters as RtfParser (encoding/tolerant passed to the main document parse)."""
        self.encoding = encoding
        self.tolerant = tolerant

    def parse(self, file_path: str | Path) -> Document:
        """Parse an RTFD directory bundle: validate → main document → resource reconciliation (see class docstring)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "rtfd"
        start_time = time.perf_counter()
        bundle_path = Path(file_path)
        if not bundle_path.is_dir():
            metadata["parse_failed"] = True
            metadata["parse_error"] = "rtfd bundle must be a directory; got non-directory path"
            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(text="", metadata=metadata)
        try:
            entries = sorted(p.name for p in bundle_path.iterdir())
            main_name = next(
                (name for name in entries if name.upper() == "TXT.RTF"), None,
            )
            if main_name is None:
                metadata["parse_failed"] = True
                metadata["parse_error"] = "rtfd bundle missing main document TXT.rtf"
                metadata["rtfd_resources"] = []
                metadata["parse_duration_ms"] = round(
                    (time.perf_counter() - start_time) * 1000, 3,
                )
                return Document(text="", metadata=metadata)
            main_path = bundle_path / main_name
            document = RtfParser(
                encoding=self.encoding, tolerant=self.tolerant,
            ).parse(main_path)
            document.metadata["file_type"] = "rtfd"
            document.metadata["rtfd_main_document"] = main_name
            # Main-document image asset asset_id set (img_ + sha256[:12])
            manifest = document.metadata.get("rtf_image_assets") or {}
            asset_ids = {
                a.get("asset_id")
                for a in manifest.get("assets", [])
                if a.get("asset_id")
            }
            known_noise = {".DS_Store", "Thumbs.db"}
            resources: list[dict[str, Any]] = []
            for name in entries:
                if name == main_name:
                    continue
                entry_path = bundle_path / name
                if not entry_path.is_file():
                    continue
                data = entry_path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                asset_id = "img_" + digest[:12]
                referenced = asset_id in asset_ids
                if name in known_noise:
                    role = "bundle_noise"
                elif referenced:
                    role = "attachment"
                else:
                    role = "orphan"
                resources.append(
                    {
                        "name": name,
                        "size_bytes": len(data),
                        "sha256_prefix": digest[:16],
                        "role": role,
                        "matches_main_asset": referenced,
                    }
                )
            document.metadata["rtfd_resources"] = resources
            document.metadata["rtfd_resources_total"] = len(resources)
            document.metadata["rtfd_attachments"] = sum(
                1 for r in resources if r["role"] == "attachment"
            )
            document.metadata["rtfd_orphans"] = sum(
                1 for r in resources if r["role"] == "orphan"
            )
            document.metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return document
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            return Document(text="", metadata=metadata)
