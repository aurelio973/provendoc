# ============================================================
# Record Stream Domain
# JSONL Semantic Structure Parser
# ============================================================
import hashlib
import json
import logging
import time
from pathlib import Path

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts,
    build_diagnostics,
    build_warnings,
    count_replacement_chars,
    count_suspicious_chars,
    detect_security_carriers,
    printable_ratio,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.event.jsonl_unresolved import (
    derive_jsonl_unresolved,
)
from provendoc.core.parsers.event.log_families import (
    detect_jsonl_envelope,
)
from provendoc.core.text.normalization import sanitize_invisible_chars

logger = logging.getLogger(__name__)


# ============================================================
# Parser
# ============================================================
class JsonlParser(BaseParser):
    """JSON Lines event-stream parser."""

    def __init__(
            self,
            encoding="utf-8",
            skip_invalid=True,
            max_line_length=100_000
    ):
        """Initialize with encoding, the bad-line skip switch and the per-line length cap."""
        self.encoding = encoding
        self.skip_invalid = skip_invalid
        self.max_line_length = max_line_length

    # ========================================================
    # public
    # ========================================================
    def parse(self, file_path):
        """Parse a .jsonl file into a Document (line-by-line events, bad lines skippable)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "jsonl"
        start_time = time.perf_counter()

        try:
            raw_bytes = Path(file_path).read_bytes()
            metadata["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
            text = raw_bytes.decode(self.encoding,errors="replace")
            lines = text.splitlines()

            records = []
            invalid_lines = []
            oversized_lines = 0

            # ====================================================
            # parse line-by-line
            # ====================================================
            for idx,line in enumerate(lines):
                line_no = idx + 1
                stripped = line.strip()

                # empty
                if not stripped:
                    continue

                # oversized
                if len(stripped) > self.max_line_length:
                    oversized_lines += 1
                    invalid_lines.append({
                        "line": line_no,
                        "reason": "oversized_line"
                    })
                    continue

                try:
                    obj = json.loads(stripped)
                    records.append({
                        "line": line_no,
                        "record": obj,
                        "record_type": self._infer_record_type(obj),
                    "keys":
                        list(obj.keys())
                        if isinstance(obj,dict)
                        else [],
                        "raw": stripped
                    })
                except Exception as e:
                    invalid_lines.append({
                        "line": line_no,
                        "reason": str(e)
                    })

                    if not self.skip_invalid:
                        raise

            # ====================================================
            # diagnostics
            # ====================================================
            diagnostics = build_diagnostics(
                replacement_chars=(
                    count_replacement_chars(text)
                ),
                suspicious_chars=(
                    count_suspicious_chars(text)
                ),
                printable_ratio=(
                    printable_ratio(text)
                ),
                symbol_ratio=0.0,
                emoji_ratio=0.0,  # event domain: semantic-density metrics don't apply, same policy as symbol_ratio
                empty_line_ratio=0.0,
                hidden_unicode=False,
                contains_ansi_escape=False
            )
            metadata["diagnostics"] = diagnostics
            metadata["warnings"] = build_warnings(diagnostics)

            # Security-carrier census (invisible_carrier_v1): observed on
            # the decoded raw view (jsonl decodes by itself and never goes
            # through base's _read_and_diagnose path, so the same census is
            # mounted here); fact codes are appended to warnings, structured
            # counts are written into metadata["security_carriers"] only when
            # non-zero, for external test-point consumption. Carriers can
            # hide inside JSON string values, unrelated to record semantics —
            # observe only, never modify.
            carrier_census = detect_security_carriers(text)
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata["warnings"].extend(
                    build_carrier_facts(carrier_census)
                )

            metadata["record_count"] = len(records)
            metadata["invalid_line_count"] = len(invalid_lines)
            metadata["oversized_line_count"] = oversized_lines
            metadata["record_type_distribution"] = self._record_distribution(records)

            # Envelope-family detection (log_format_family_prereg_v1 §2.4):
            # record-level key-set fingerprint (CloudTrail/ECS/OTLP); when
            # the threshold hits, write metadata["jsonl_envelope_family"];
            # record_type inference stays unchanged — the envelope is an
            # observation, not a rerouting. generic does not impersonate
            # (no key written).
            envelope = detect_jsonl_envelope(records)
            if envelope:
                metadata["jsonl_envelope_family"] = envelope

            # Object-level unresolved registry (jsonl_unresolved_prereg_v1):
            # derived from the existing invalid_lines fact source; the
            # original key is never rewritten, write only when non-empty.
            try:
                stamp_unresolved_registry(
                    metadata, "jsonl_unresolved",
                    derive_jsonl_unresolved(
                        invalid_lines, self.max_line_length
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug(
                    "jsonl unresolved registry skipped: %s", exc
                )

            metadata["parse_duration_ms"] = round(
                (
                    time.perf_counter()
                    - start_time
                ) * 1000,
                3
            )

            # ====================================================
            # reconstructed text
            # ====================================================
            reconstructed = self._build_text_preview(records)

            # Working-text sanitization (same contract as base.
            # _read_and_diagnose): the preview is the chunker layer's real
            # input, so carriers are normalized here; the raw lines inside
            # records and source_text stay verbatim (evidence facts are
            # never rewritten), and the sanitization is honestly disclosed
            # via metadata["sanitization"] + warning.
            sanitized_text, sanitization = sanitize_invisible_chars(
                reconstructed
            )
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
                    metadata["warnings"].append(
                        f"{normalized} invisible characters normalized "
                        "(see metadata.sanitization)"
                    )

            return Document(
                text=sanitized_text,
                metadata={
                    **metadata,
                    "records": records,
                    "invalid_lines": invalid_lines
                },
                # JSONL's working text is a semantic reconstruction of valid
                # records; preserve the independently decoded file source for
                # Source Document / raw provenance views.
                source_text=text,
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)

            return Document(
                text="",
                metadata=metadata
            )

    # ========================================================
    # infer type
    # ========================================================
    def _infer_record_type(self,obj):
        if isinstance(obj,dict):
            keys = set(obj.keys())

            # log/event style
            if (
                "timestamp" in keys
                or "event" in keys
                or "level" in keys
            ):
                return "event"

            # conversation/chat
            if "role" in keys and "content" in keys:
                return "conversation"

            # embedding/vector
            if "embedding" in keys or "vector" in keys:
                return "embedding"

            return "object"

        if isinstance(obj,list):
            return "array"

        return type(obj).__name__

    # ========================================================
    # distribution
    # ========================================================
    def _record_distribution(self,records):
        distribution = {}

        for r in records:
            t = r["record_type"]
            distribution[t] = distribution.get(t,0) + 1
        return distribution

    # ========================================================
    # preview
    # ========================================================
    def _build_text_preview(self,records):
        preview = []
        for r in records[:50]:
            line = (
                f"[line {r['line']}] "
                f"{r['record_type']}"
            )
            keys = r.get("keys")

            if keys:
                line += f" keys={keys}"

            preview.append(line)

        return "\n".join(preview)
