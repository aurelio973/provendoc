# ============================================================
# Narrative Domain
# reStructuredText (.rst) Parser
# ============================================================
"""reStructuredText parser: source-evidence mode (same contract as TexParser).

Fact boundaries (honest declarations):
    - text keeps the RST source verbatim (directives/roles/literal blocks are
      document facts, never stripped at the parser layer)
    - headings are extracted only from real adornment structures (underline
      and overline+underline); levels follow first-appearance order of the
      style (docutils semantics), never guessed from typographic clues like
      bold or ALL CAPS
    - directive evidence and the static-structure census are fully delegated
      to rst_structure (single source of truth): rst_directives v2
      (counts/categories/language histogram, executed=False),
      rst_structure_v1 census, rst_parse_status tri-state derivation,
      rst_system_messages bounded diagnostics. include/image targets are
      registered + locally reconciled, never expanded; raw directives are
      never executed
"""
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import (
    DEFAULT_TEXT_MAX_SOURCE_BYTES,
    BaseParser,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.parsers.narrative.rst_structure import (
    _ADORNMENT_CHARS,
    collect_rst_structure_facts,
    directive_evidence_v2,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.heading_patterns import HeadingCandidate
from provendoc.core.structure.special_modules import detect_special_modules


class RstParser(BaseParser):
    """reStructuredText parser: heading structure + directive/structure evidence (source kept verbatim)."""

    # Source-size guard (text_source_limits_v1): default 32 MiB, see base.parser.
    max_source_bytes = DEFAULT_TEXT_MAX_SOURCE_BYTES

    def __init__(self, encoding="utf-8", tolerant=True):
        """Initialize with encoding and tolerant switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    def parse(self, file_path):
        """Parse .rst into a Document (source verbatim + heading candidates + structure evidence).

        Any read/decode failure takes the honest-failure path: parse_failed=True
        and rst_parse_status is always failed (reasons prefixed with PARSE_FAILED,
        prereg §3 tri-state contract).
        """
        start_time = time.perf_counter()
        metadata = {}
        try:
            base = _build_metadata(file_path)
            metadata.update(base)
            metadata["file_type"] = "rst"

            ctx = self._read_and_diagnose(
                file_path, "rst", self.encoding, self.tolerant,
            )
            text = ctx.text
            metadata = ctx.metadata

            metadata["heading_candidates"] = self._section_candidates(text)
            metadata["special_modules"] = detect_special_modules(text)
            metadata["domain_signals"] = detect_domain_signals(text)
            metadata["section_count"] = len(metadata["heading_candidates"])
            metadata["rst_directives"] = directive_evidence_v2(text)
            collect_rst_structure_facts(text, metadata, file_path)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text,
                metadata=metadata,
                source_text=ctx.source_text,
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            # The failure path always writes the tri-state (PARSE_FAILED never swallowed)
            metadata["rst_parse_status"] = {
                "status": "failed",
                "reasons": ["PARSE_FAILED"],
            }
            return Document(text="", metadata=metadata)

    # ========================================================
    # section candidates (docutils level semantics)
    # ========================================================

    @staticmethod
    def _is_adornment(line, min_len=3):
        """A whole line composed of one adornment character, at least min_len long."""
        stripped = line.rstrip()
        if len(stripped) < min_len:
            return None
        if stripped[0] not in _ADORNMENT_CHARS:
            return None
        if set(stripped) != {stripped[0]}:
            return None
        return stripped[0]

    @staticmethod
    def _section_candidates(text):
        """Extract HeadingCandidates from adornment structures (with absolute offsets).

        Two legal forms:
            underline:  a ``Title`` line followed by an equal-length adornment line
            overline:  adornment / ``Title`` / same-char adornment triple
        Level = first-appearance order of the (char, overline) style (docutils
        semantics; "=" is not presumed to rank above "-").
        """
        lines = text.splitlines(keepends=True)
        offsets = []
        pos = 0
        for line in lines:
            offsets.append(pos)
            pos += len(line)

        styles = []  # (char, overline) first-appearance order → depth
        candidates = []

        def depth_for(char, overline):
            key = (char, overline)
            if key not in styles:
                styles.append(key)
            return styles.index(key) + 1

        def is_title_line(idx):
            """Candidate title line: non-empty, not an adornment, not a directive/comment."""
            if idx < 0 or idx >= len(lines):
                return False
            stripped = lines[idx].strip()
            if not stripped:
                return False
            if RstParser._is_adornment(lines[idx]) is not None:
                return False
            if stripped.startswith(".. "):
                return False
            return True

        for i, line in enumerate(lines):
            char = RstParser._is_adornment(line)
            if char is None:
                continue
            adorn_len = len(line.rstrip())

            # overline form: adornment / title / same-char adornment
            if (
                i + 2 < len(lines)
                and is_title_line(i + 1)
                and RstParser._is_adornment(lines[i + 2]) == char
                and len(lines[i + 2].rstrip()) >= adorn_len - 1
            ):
                title = lines[i + 1].strip()
                if adorn_len >= len(title):
                    candidates.append(HeadingCandidate(
                        raw=title,
                        pattern="rst_overline",
                        depth_hint=depth_for(char, True),
                        start=offsets[i + 1],
                        end=offsets[i + 2] + len(lines[i + 2]),
                    ))
                continue

            # underline form: title line (previous) + this adornment line
            prev = i - 1
            if not is_title_line(prev):
                continue
            # The line before the title must be blank or the document start (excludes wrapped-paragraph misjudgement)
            if prev - 1 >= 0 and lines[prev - 1].strip():
                continue
            # The previous line must not be another overline title (avoids double counting)
            if prev - 1 >= 0 and (
                RstParser._is_adornment(lines[prev - 1]) is not None
            ):
                continue
            title = lines[prev].strip()
            if adorn_len < len(title):
                continue
            candidates.append(HeadingCandidate(
                raw=title,
                pattern="rst_underline",
                depth_hint=depth_for(char, False),
                start=offsets[prev],
                end=offsets[i] + len(line),
            ))
        return candidates
