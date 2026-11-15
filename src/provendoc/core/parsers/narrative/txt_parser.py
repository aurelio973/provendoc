# ============================================================
# Narrative Domain
# TXT Semantic Structure Parser
# ============================================================
import bisect
import logging
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import (
    DEFAULT_TEXT_MAX_SOURCE_BYTES,
    BaseParser,
)
from provendoc.core.diagnostics.sensitive_signals import (
    detect_sensitive_candidates,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.foundation.unresolved_registry import (
stamp_unresolved_registry,
)
from provendoc.core.parsers.narrative.txt_decode_errors import (
    collect_decode_errors,
)
from provendoc.core.parsers.narrative.txt_encoding_segments import (
    detect_encoding_segment_candidates,
)
from provendoc.core.parsers.narrative.txt_file_facts import (
    collect_txt_engineering_facts,
    collect_txt_file_facts,
)
from provendoc.core.parsers.narrative.txt_numeric_signals import (
    detect_numeric_signals,
)
from provendoc.core.parsers.narrative.txt_parse_status import (
    derive_parse_status,
)
from provendoc.core.parsers.narrative.txt_unresolved import (
derive_txt_unresolved,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.fixed_width_signals import (
detect_fixed_width_tables,
)
from provendoc.core.structure.heading_patterns import extract_heading_candidates
from provendoc.core.structure.special_modules import detect_special_modules
from provendoc.core.structure.subtype_signals import (
    detect_subtype,
    probe_delegation,
)

logger = logging.getLogger(__name__)


def _annotate_heading_overlap(fixed_width, heading_candidates, text):
    """Register line-number overlap between in-table rows and heading_candidates (cross-reference).

    Purely additive clue: no heading candidate is modified or removed (zero-regression
    discipline); only ``heading_overlap_lines`` is registered on candidate line numbers
    covered by a table row range, for the downstream layer to filter at will (observed
    evidence of a7/a10 caps heading pollution, see txt_recovery_dqr_findings_v1 §1).
    """
    if not heading_candidates:
        return
    line_starts = [0]
    for line in text.splitlines():
        line_starts.append(line_starts[-1] + len(line) + 1)
    heading_lines = sorted(
        bisect.bisect_right(line_starts, candidate.start)
        for candidate in heading_candidates
    )
    for table in fixed_width["tables"]:
        left = bisect.bisect_left(heading_lines, table["line_start"])
        right = bisect.bisect_right(heading_lines, table["line_end"])
        table["heading_overlap_lines"] = heading_lines[left:right]


class TxtParser(BaseParser):
    """TXT parser.

    The common flow (read → decode → normalize → diagnose → sanitize) all goes
    through the BaseParser shared pipeline; this class only handles txt-specific
    structure detection: heading_candidates + special_modules (executed on the
    sanitized text), plus pure observation fact registration (txt_file_facts:
    disguised-container/newline-stats/empty-file classification/truncation evidence/
    statistics blocks, subtype candidate registration — all additive, failures
    degrade silently, parsing behavior never changes).
    """

    # Source-size guard (text_source_limits_v1): default 32 MiB, see base.parser.
    max_source_bytes = DEFAULT_TEXT_MAX_SOURCE_BYTES

    def __init__(
        self,
        encoding="utf-8",
        tolerant=True,
    ):
        """Initialize with encoding and tolerant switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    # ========================================================
    # public
    # ========================================================

    def parse(self, file_path):
        """Parse .txt into a Document (shared pipeline + structure detection)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "text"

        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "text", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            # txt-specific: structure detection
            metadata["heading_candidates"] = extract_heading_candidates(text)
            metadata["special_modules"] = detect_special_modules(text)
            # Domain document signals (contracts/medical reports/drug inserts/invoices):
            # textual-convention evidence, count only, no interpretation — the same
            # fact discipline as heading_candidates.
            metadata["domain_signals"] = detect_domain_signals(text)

            # Pure observation fact registration (additive; failures degrade silently,
            # never dragging parsing down).
            # Disguised containers take the lenient profile: register + warning, then
            # keep parsing as text; no delegation/unpacking/execution (security stance in
            # the txt_file_facts docstring).
            try:
                collect_txt_file_facts(
                    file_path, metadata, ctx.source_text, text
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt file facts collection skipped: %s", exc)

            # Byte-wise decode-error registration (txt_engineering_gaps_prereg_v1 G4):
            # character positions are unstable under mojibake/multi-byte; byte coordinates
            # are the honest convention. Observation-only, write non-empty only,
            # failures degrade silently.
            try:
                decode_errors = collect_decode_errors(
                    file_path, metadata.get("encoding_used")
                )
                if decode_errors is not None:
                    metadata["decode_errors"] = decode_errors
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt decode error registry skipped: %s", exc)

            # Subtype candidates (json/ndjson/xml/csv/log/code, etc.): register only, never delegate.
            subtype = None
            try:
                subtype = detect_subtype(text)
                if subtype is not None:
                    metadata["detected_subtype"] = subtype
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt subtype detection skipped: %s", exc)

            # Delegation probe: distinguish "format detected" from "delegation actually
            # succeeded" — for safely delegable subtypes (stdlib json/csv/ET, inside the size
            # gate) rerun the real parser and register status; failure falls back to
            # plain_text with the reason registered. Delegation output is discarded; the
            # document text is never replaced.
            if subtype is not None:
                try:
                    metadata["delegation"] = probe_delegation(subtype, text)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("txt delegation probe skipped: %s", exc)

            # Engineering-grade facts: raw/structured view declarations, reproducible
            # config blocks, output strategy declarations, special_modules position
            # enrichment (line/char/byte).
            try:
                collect_txt_engineering_facts(
                    metadata, ctx.source_text, text,
                    self.encoding, self.tolerant,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt engineering facts skipped: %s", exc)

            # Sensitive information: identify only (type + line position, no value stored);
            # never redact or rewrite.
            try:
                sensitive = detect_sensitive_candidates(text)
                if sensitive is not None:
                    metadata["sensitive_candidates"] = sensitive
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt sensitive scan skipped: %s", exc)

            # Date/amount candidates (txt_engineering_gaps_prereg_v1 G8): identify only
            # (counts + bounded samples + capped line positions), never parse into
            # date/numeric types.
            try:
                numeric = detect_numeric_signals(text)
                if numeric is not None:
                    metadata["numeric_signals"] = numeric
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt numeric signals skipped: %s", exc)

            # Recovery-layer candidate evidence (txt_recovery_evidence_prereg_v1):
            # fixed-width table candidates + mixed-encoding segment candidates. All
            # observation-only (no text rearrangement/redecoding/HeadingCandidate
            # touching), additive, write non-empty only, failures degrade silently.
            try:
                fixed_width = detect_fixed_width_tables(text)
                if fixed_width is not None:
                    _annotate_heading_overlap(
                        fixed_width, metadata["heading_candidates"], text
                    )
                    metadata["fixed_width_table_candidates"] = fixed_width
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt fixed-width candidacy skipped: %s", exc)

            try:
                segment_candidates = detect_encoding_segment_candidates(
                    file_path, metadata.get("encoding_used")
                )
                if segment_candidates is not None:
                    metadata["encoding_segment_candidates"] = (
                        segment_candidates
                    )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt encoding segment candidacy skipped: %s", exc)

            # Object-level unresolved registry (txt_unresolved_prereg_v1): derived from
            # existing observation keys (truncation/disguised container/delegation
            # failure), never rewriting the original keys, write non-empty only.
            try:
                stamp_unresolved_registry(
                    metadata, "txt_unresolved",
                    derive_txt_unresolved(metadata, text),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt unresolved registry skipped: %s", exc)

            # Document-level tri-state (txt_engineering_gaps_prereg_v1 G1): derive
            # complete/partial/failed from existing evidence keys; no new detection logic.
            try:
                metadata["parse_status"] = derive_parse_status(metadata)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("txt parse status skipped: %s", exc)

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)

            return Document(
                text=text,
                metadata=metadata,
                source_text=ctx.source_text,
            )

        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            try:
                metadata["parse_status"] = derive_parse_status(metadata)
            except Exception:  # pragma: no cover - defensive guard
                metadata["parse_status"] = {
                    "status": "failed",
                    "reasons": ["parse_failed"],
                }

            return Document(
                text="",
                metadata=metadata
            )
