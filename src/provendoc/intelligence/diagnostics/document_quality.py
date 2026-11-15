"""Document Quality Report — evaluates the SOURCE document,
not the chunks.

Separation principle:
    Document quality = "is the source text clean and well-formed?"
    Chunk quality    = "are the chunks well-cut?"

DQR always evaluates document.original_text (the parse
output, before semantic cleaning). Cleaning removes noise —
scoring the cleaned text would hide exactly the problems
the user needs to know about. How much was cleaned is
reported separately via the cleaning trace in document
metadata.

These are independent concerns. A perfectly chunked dirty document
should get high quality scores but low DQR. A poorly chunked clean document
should get low quality scores but high DQR.

Mixing them would mean: bad document → chunk score drops →
optimizer picks wrong strategy → solves the wrong problem.

Scenario evidence (scenario framework prereg): any ledger stamped
at ``document.metadata["invoice_scenario"]`` is surfaced VERBATIM
under ``result["scenario"]["invoice"]``. It is reported, never
scored — scenario witnesses/conflicts must not bend the four
dimension scores (same cold-path discipline as the rest of DQR:
DQR measures the raw material, the scenario ledger reports what
the invoice chain found in it).
"""

from provendoc.core.diagnostics import blank_run_excess_ratio
from provendoc.core.foundation.char_policy import (
    ANSI_ESCAPE_PATTERN,
    HIDDEN_UNICODE_MAP,
)
from provendoc.core.scenario.invoice_scenario_dqr import (
    SCENARIO_METADATA_KEY as INVOICE_SCENARIO_KEY,
)


class DocumentQualityReport:
    """Evaluate source document quality, independent of chunking.

    Dimensions:
        encoding_health     — decoding artifacts, replacement chars
        noise_level         — zero-width chars, ANSI escapes, control chars
        structure_signals   — detectable headings, tables, enumerations
        content_density     — printable ratio, symbol/emoji ratio, empty lines

    Each dimension returns 0.0–1.0.
    Overall is advisory; the profile is the real output.
    """

    WEIGHTS = {
        "encoding_health": 0.30,
        "noise_level": 0.30,
        "structure_signals": 0.20,
        "content_density": 0.20,
    }

    # The invisible-character set is declared by the policy table (a self-held subset once missed the U+2066-2069 isolates)
    _HIDDEN_CHARS = frozenset(HIDDEN_UNICODE_MAP)

    _ANSI_PATTERN = ANSI_ESCAPE_PATTERN

    # ==================================
    # public API
    # ==================================

    def evaluate(self, document):
        """Evaluate document quality.

        Evaluates the ORIGINAL text (document.original_text)
        — the parse output before any cleaning or repair.
        Document quality measures the raw material the user
        handed us, not our own cleaned version. Falls back to
        document.text for objects without original_text
        (e.g. legacy constructions).

        Args:
            document: Document with text and metadata

        Returns:
            dict with profile, overall (advisory), issues
        """
        original = getattr(document, "original_text", None)
        text = original if original is not None else document.text
        diagnostics = document.metadata.get("diagnostics", {})
        semantic_signals = document.metadata.get(
            "semantic_signals", {}
        )

        # encoding_fallback_disclosure_v1 fix A: the parser places this key at
        # the metadata top level rather than in diagnostics (build_diagnostics
        # keys are fixed); the injected copy makes _encoding_health's -0.1
        # penalty actually fire (restoring the frozen DQR claimed behavior; the shared dict is untouched).
        if "encoding_fallback" not in diagnostics:
            diagnostics = {
                **diagnostics,
                "encoding_fallback": bool(
                    document.metadata.get("encoding_fallback")
                ),
            }

        if not text:
            return self._with_scenario(document, self._empty_result())

        dimensions = {
            "encoding_health": self._encoding_health(
                text, diagnostics
            ),
            "noise_level": self._noise_level(
                text, diagnostics
            ),
            "structure_signals": self._structure_signals(
                semantic_signals
            ),
            "content_density": self._content_density(
                text, diagnostics
            ),
        }

        overall = sum(
            dimensions[d] * self.WEIGHTS[d]
            for d in dimensions
        )

        issues = self._generate_issues(dimensions)

        return self._with_scenario(document, {
            "profile": {
                k: round(v, 3) for k, v in dimensions.items()
            },
            "overall": round(overall, 3),
            "issues": issues,
            "metadata": {
                "char_count": len(text),
                "line_count": text.count("\n") + 1,
                "has_hidden_unicode": bool(
                    diagnostics.get("hidden_unicode")
                ),
                "has_ansi_escape": bool(
                    diagnostics.get("contains_ansi_escape")
                ),
                "encoding_used": document.metadata.get(
                    "encoding_used"
                ),
                "encoding_fallback": document.metadata.get(
                    "encoding_fallback"
                ),
            },
        })

    def _with_scenario(self, document, result):
        """Surface stamped scenario ledgers verbatim (never scored).

        Scenario chains stamp their full ledgers on the document
        metadata (invoice: ``metadata["invoice_scenario"]`` via
        invoice_scenario_dqr; academic_paper:
        ``metadata["academic_paper_scenario"]`` via
        academic_paper_scenario_dqr). DQR attaches each as-is under
        ``result["scenario"][label]`` for downstream reconciliation.
        No dimension score is affected — evidence is reported, not
        folded into quality judgement.
        """
        from provendoc.core.scenario.academic_paper_scenario_dqr import (
            SCENARIO_METADATA_KEY as ACADEMIC_SCENARIO_KEY,
        )

        scenario_blocks = {}
        for label, key in (
            ("invoice", INVOICE_SCENARIO_KEY),
            ("academic_paper", ACADEMIC_SCENARIO_KEY),
        ):
            ledger = document.metadata.get(key)
            if isinstance(ledger, dict):
                scenario_blocks[label] = ledger
        if scenario_blocks:
            result["scenario"] = scenario_blocks
        return result

    # ==================================
    # dimension 1: encoding health
    # ==================================
    #
    # Checks for decoding artifacts:
    #   - replacement characters (U+FFFD)
    #   - encoding fallback events
    #   - suspicious characters
    # ==================================

    def _encoding_health(self, text, diagnostics):
        if not text:
            return 0.0

        score = 1.0

        # replacement characters from decode
        replacement_count = diagnostics.get(
            "replacement_chars", 0
        )
        if replacement_count > 0:
            ratio = min(replacement_count / max(len(text), 1), 1)
            score -= ratio * 5

        # suspicious characters
        suspicious_count = diagnostics.get(
            "suspicious_chars", 0
        )
        if suspicious_count > 0:
            ratio = min(suspicious_count / max(len(text), 1), 1)
            score -= ratio * 3

        # encoding fallback occurred
        fallback = diagnostics.get("encoding_fallback")
        if fallback:
            score -= 0.1

        return round(max(score, 0.0), 3)

    # ==================================
    # dimension 2: noise level
    # ==================================
    #
    # Checks for noise artifacts in text:
    #   - zero-width characters
    #   - ANSI escape sequences
    #   - control characters (excluding \n \t \r)
    # ==================================

    def _noise_level(self, text, diagnostics):
        if not text:
            return 0.0

        noise_count = 0

        # invisible characters: zero-width / bidi etc. (full policy-table set, isolates included)
        for hidden in self._HIDDEN_CHARS:
            noise_count += text.count(hidden)

        # ANSI escape sequences
        noise_count += len(
            self._ANSI_PATTERN.findall(text)
        )

        # control characters
        for char in text:
            if (ord(char) < 32
                    and char not in ("\n", "\t", "\r")):
                noise_count += 1

        if len(text) == 0:
            return 0.0

        noise_ratio = noise_count / len(text)
        score = 1.0 - min(noise_ratio * 20, 1.0)

        return round(max(score, 0.0), 3)

    # ==================================
    # dimension 3: structure signals
    # ==================================
    #
    # Checks if the document has detectable structural elements.
    # A document with no detectable structure is harder to
    # chunk well — and harder to evaluate confidently.
    # ==================================

    def _structure_signals(self, semantic_signals):
        score = 0.0

        sections = semantic_signals.get("sections", {})
        tables = semantic_signals.get("tables", {})
        enumeration = semantic_signals.get("enumeration", {})

        if sections.get("contains_section_headings"):
            score += 0.4

        if tables.get("contains_table_like_text"):
            score += 0.3

        if enumeration.get("contains_enumeration"):
            score += 0.3

        return round(score, 3)

    # ==================================
    # dimension 4: content density
    # ==================================
    #
    # Checks how much actual readable content the document has.
    # ==================================

    def _content_density(self, text, diagnostics):
        if not text:
            return 0.0

        score = 1.0

        # printable ratio
        printable = diagnostics.get("printable_ratio", 1.0)
        if printable < 0.8:
            score -= (0.8 - printable) * 2

        # symbol ratio (high symbol = low density).
        # emoji has been split into its own metric: symbol_ratio is the umbrella
        # ratio including emoji — only after subtracting emoji do we get the
        # "math/currency-symbol density" effect itself, avoiding double-counting emoji spam and symbol density.
        symbols = diagnostics.get("symbol_ratio", 0.0)
        emojis = diagnostics.get("emoji_ratio", 0.0)
        non_emoji_symbols = max(symbols - emojis, 0.0)
        if non_emoji_symbols > 0.15:
            score -= (non_emoji_symbols - 0.15) * 3

        # emoji ratio: an earlier warning of semantic thinness (threshold 0.2, consistent with warnings_builder)
        if emojis > 0.2:
            score -= (emojis - 0.2) * 4

        # empty line penalty (2026-10-09 scope fix): the penalty base moved
        # from empty_line_ratio to the excess ratio of consecutive blank
        # lines (a core.diagnostics shared metric, same base as the
        # warnings_builder report). The old scope mis-penalized markdown's
        # inter-paragraph blank-line convention (30-50% blank) as padding
        # (typical sample 0.676); single blanks are typesetting separation, not padding — only the 2nd+ line of a blank run counts. Threshold 0.3 and ×2 unchanged; real padding docs keep their penalty.
        blank_excess = blank_run_excess_ratio(text)
        if blank_excess > 0.3:
            score -= (blank_excess - 0.3) * 2

        return round(max(score, 0.0), 3)

    # ==================================
    # issue generation
    # ==================================

    _THRESHOLDS = {
        "encoding_health": (
            0.7,
            "encoding artifacts detected — "
            "replacement characters or fallback decoding"
        ),
        "noise_level": (
            0.7,
            "noise artifacts in text — "
            "zero-width chars, ANSI escapes, or control chars"
        ),
        "structure_signals": (
            0.3,
            "no detectable structure — "
            "headings, tables, or enumerations not found"
        ),
        "content_density": (
            0.6,
            "low content density — "
            "high symbol ratio or excessive empty lines"
        ),
    }

    def _generate_issues(self, dimensions):
        issues = []

        for dim, (threshold, message) in (
            self._THRESHOLDS.items()
        ):
            if dimensions[dim] < threshold:
                issues.append({
                    "dimension": dim,
                    "score": round(dimensions[dim], 3),
                    "threshold": threshold,
                    "message": message,
                })

        return issues

    def _empty_result(self):
        return {
            "profile": {
                d: 0.0 for d in self.WEIGHTS
            },
            "overall": 0.0,
            "issues": [
                {
                    "dimension": "all",
                    "message": "empty document"
                }
            ],
            "metadata": {
                "char_count": 0,
                "line_count": 0,
            },
        }
