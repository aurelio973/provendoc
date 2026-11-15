# ============================================================
# Record Stream Domain
# LOG Semantic Structure Parser
# ============================================================
import hashlib
import re
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
from provendoc.core.parsers.event.log_families import (
    FAMILY_BOUNDARY_HINTS,
    classify_log_line,
    detect_log_format_family,
)
from provendoc.core.text.normalization import sanitize_invisible_chars

# ============================================================
# Common log patterns
# ============================================================
TIMESTAMP_PATTERNS = [
    # 2026-08-10 12:30:45
    r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}",

    # 2026-08-10T12:30:45
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",

    # 12:30:45
    r"\d{2}:\d{2}:\d{2}"
]

LEVEL_PATTERNS = [
    "DEBUG",
    "INFO",
    "WARN",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "FATAL"
]

# ============================================================
# Parser
# ============================================================
class LogParser(BaseParser):
    """Generic log parser (multiline event recovery + session detection)."""

    def __init__(
            self,
            encoding = "utf-8",
            multiline_recovery=True,
            detect_sessions=True,
            max_event_lines=200
    ):
        """Initialize with encoding, multiline recovery, session detection and the event line cap."""
        self.encoding = encoding
        self.multiline_recovery = multiline_recovery
        self.detect_sessions = detect_sessions
        self.max_event_lines = max_event_lines

    # ========================================================
    # public
    # ========================================================
    def parse(self, file_path):
        """Parse a .log file into a Document (recovering multiline events)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "log"
        start_time = time.perf_counter()

        try:
            raw_bytes = Path(file_path).read_bytes()
            metadata["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
            text = raw_bytes.decode(self.encoding,errors="replace")
            lines = text.splitlines()

            # Format-family detection (log_format_family_prereg_v1): runs
            # BEFORE event recovery — when a family holds, its line-level
            # syntax doubles as the event boundary (D2: CLF/syslog/logfmt
            # timestamps are not line-start anchored, so the generic
            # heuristic is blind to them); unrecognized families keep the
            # current heuristic unchanged. Observe only, never modify.
            family, family_facts = detect_log_format_family(lines)
            events = self._recover_events(lines, family_hint=family)
            diagnostics = self._build_log_diagnostics(text,events)

            metadata["diagnostics"] = diagnostics
            metadata["warnings"] = build_warnings(diagnostics)

            # Security-carrier census (invisible_carrier_v1): log decodes by
            # itself and never goes through base's _read_and_diagnose path, so
            # the same census is mounted here; it observes the decoded raw
            # view, appends fact codes to warnings, and writes structured
            # counts into metadata["security_carriers"] only when non-zero.
            # Carriers can hide in log message bodies or forged terminal
            # control sequences — observe only, never modify.
            carrier_census = detect_security_carriers(text)
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata["warnings"].extend(
                    build_carrier_facts(carrier_census)
                )

            metadata["event_count"] = len(events)

            # Family facts on the record (written only when detection holds;
            # generic does not impersonate): family name + field-level
            # statistics for downstream consumption (retrieval / LLM context
            # assembly can pick the structured view accordingly).
            if family:
                metadata["log_format_family"] = family
                metadata["log_family_facts"] = family_facts

            metadata["multiline_recovery"] = self.multiline_recovery
            metadata["parse_duration_ms"] = round((time.perf_counter()-start_time) * 1000,3)
            metadata["severity_distribution"] = self._severity_distribution(events)
            metadata["session_count"] = len({
                e["session_id"]
                for e in events
                if e["session_id"]
            })

            # Working-text sanitization (same contract as base.
            # _read_and_diagnose): after event recovery completes on the raw
            # view, only Document.text is normalized; source_text keeps the
            # decoded original (Source Document / provenance view), and the
            # sanitization is honestly disclosed via metadata["sanitization"]
            # + warning.
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
                    metadata["warnings"].append(
                        f"{normalized} invisible characters normalized "
                        "(see metadata.sanitization)"
                    )

            return Document(
                text=sanitized_text,
                metadata={
                    **metadata,
                    "events": events
                },
                # Keep the decoded file text independently so report/raw
                # provenance views remain faithful even if log processing is
                # extended with later working-text transformations.
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
    # Event Recovery
    # ========================================================
    def _recover_events(self,lines,family_hint=None):
        events = []
        current_event = []
        # 0-based indices of the current event's first/last lines. The span
        # is the [first, last] covered range (1-based, landing in the event
        # dict) — member lines are contiguous on the recovery path; on the
        # non-recovery path's extreme edges (a blank line stuck to a pending
        # boundary event) they may be non-contiguous, and the covered-range
        # semantics stay honest.
        current_start = 0
        current_end = 0

        for index, line in enumerate(lines):
            # empty
            if not line.strip():
                if current_event:
                    current_event.append(line)
                    current_end = index

                continue

            # new event boundary
            if self._is_new_event(line,family_hint):
                if current_event:
                    events.append(
                        self._build_event(
                            current_event, current_start, current_end
                        )
                    )

                current_event = [line]
                current_start = index
                current_end = index

            else:
                # multiline continuation
                if self.multiline_recovery:
                    if not current_event:
                        # File starts with a non-boundary line: that line is the event's first line
                        current_start = index
                    current_event.append(line)
                    current_end = index
                else:
                    events.append(self._build_event([line], index, index))


            # prevent runaway aggregation
            if len(current_event) >= self.max_event_lines:
                events.append(
                    self._build_event(
                        current_event, current_start, current_end
                    )
                )
                current_event = []

        # flush tail
        if current_event:
            events.append(
                self._build_event(current_event, current_start, current_end)
            )
        return events

    # ========================================================
    # Boundary Detection
    # ========================================================
    def _is_new_event(self,line,family_hint=None):
        stripped = line.strip()

        # Family-aware boundary (prereg D2): when a family holds, lines
        # matching its line-level syntax are event boundaries — the
        # line-start-anchored family criterion is more precise than the
        # generic heuristic, and tightens only when family evidence is used.
        if family_hint in FAMILY_BOUNDARY_HINTS:
            if classify_log_line(stripped) == family_hint:
                return True

        # timestamp boundary
        for pattern in TIMESTAMP_PATTERNS:
            if re.match(pattern,stripped):
                return True

        # severity boundary
        for level in LEVEL_PATTERNS:
            if stripped.startswith(level):
                return True

        return False

    # ========================================================
    # Event Construction
    # ========================================================
    def _build_event(self,lines,start_index=None,end_index=None):
        """Build the event dict.

        Args:
            lines: the source text lines composing the event.
            start_index: 0-based index of the first line in the source
                file; None means no line-number context (a hand-built
                event), in which case no line-number keys are written —
                absence does not impersonate (honest degradation, same
                contract as SourceContext).
            end_index: 0-based index of the last line; when None it is
                back-derived from contiguous membership
                (start + len(lines) - 1).
        """
        raw = "\n".join(lines)
        timestamp = self._extract_timestamp(raw)
        level = self._extract_level(raw)
        session_id = self._extract_session_id(raw)

        event = {
            "raw": raw,
            "line_count": len(lines),
            "timestamp": timestamp,
            "level": level,
            "session_id": session_id,
            "is_multiline": len(lines) > 1,
            "contains_traceback": self._contains_traceback(raw),
            "event_type": self._infer_event_type(raw)
        }
        if start_index is not None:
            # 1-based closed covered span; continuation lines (incl. blanks) count in
            event["line_start"] = start_index + 1
            event["line_end"] = (
                end_index + 1
                if end_index is not None
                else start_index + len(lines)
            )
        return event

    # ========================================================
    # Extraction Helpers
    # ========================================================
    def _extract_timestamp(self,text):
        for pattern in TIMESTAMP_PATTERNS:
            match = re.search(pattern,text)

            if match:
                return match.group(0)
        return None

    def _extract_level(self,text):
        upper = text.upper()

        for level in LEVEL_PATTERNS:
            if level in upper:
                return level
        return None

    def _extract_session_id(self,text):
        patterns = [
            r"request_id=([a-zA-Z0-9_-]+)",

            r"session_id=([a-zA-Z0-9_-]+)",

            r"trace_id=([a-zA-Z0-9_-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern,text)
            if match:
                return match.group(1)
        return None

    def _contains_traceback(self,text):
        keywords = [
            "Traceback",
            "Exception",
            "at ",
            "stack trace"
        ]
        return any(
            k.lower() in text.lower()
            for k in keywords
        )

    def _infer_event_type(self,text):
        if self._contains_traceback(text):
            return "traceback"

        if "timeout" in text.lower():
            return "timeout"

        if "retry" in text.lower():
            return "retry"

        return "generic"

    # ========================================================
    # Diagnostics
    # ========================================================
    def _build_log_diagnostics(self,text,events):
        return build_diagnostics(
            replacement_chars=(
                count_replacement_chars(text)
            ),
            suspicious_chars=(
                count_suspicious_chars(text)
            ),
            printable_ratio=printable_ratio(text),
            symbol_ratio=0.0,
            emoji_ratio=0.0,  # event domain: semantic-density metrics don't apply, same policy as symbol_ratio
            empty_line_ratio=0.0,
            hidden_unicode=False,
            contains_ansi_escape=False
        )

    # ========================================================
    # Severity Distribution
    # ========================================================
    def _severity_distribution(self,events):
        distribution = {}
        for e in events:
            level = e["level"] or "UNKNNOW"
            distribution[level] = distribution.get(level,0) + 1

        return distribution
