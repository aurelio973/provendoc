"""Orchestration layer: the shared read-decode-diagnose-normalize
pipeline used by every parser.

Layering contract:
    - the transform layer (core/text) only changes text, no warnings;
    - the observation layer (core/diagnostics) only observes, owns
      warnings;
    - this orchestration layer chains them: diagnose the original
      text first (warnings cannot be bypassed), then let the
      transform layer normalize invisible characters; stats land
      in metadata["sanitization"].
"""
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from provendoc.base.models.document import Document
from provendoc.core.diagnostics import (
    blank_run_excess_ratio,
    build_carrier_facts,
    build_diagnostics,
    build_warnings,
    contains_ansi_escape,
    count_replacement_chars,
    count_suspicious_chars,
    detect_hidden_unicode,
    detect_repetition,
    detect_security_carriers,
    emoji_ratio,
    empty_line_ratio,
    extract_semantic_signals,
    printable_ratio,
    symbol_ratio,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.text import (
    is_binary_suspected,
    normalize_newlines,
    safe_decode,
    sanitize_invisible_chars,
    strip_bom,
)


@dataclass
class ParseContext:
    """Shared parsing state produced by ``_read_and_diagnose``.

    Each parser receives this object after the common read-decode-diagnose
    pipeline runs, and only needs to add format-specific metadata + structured data.

    ``text`` is the sanitized text (U+200B becomes a normal space; unsafe
    invisible characters are replaced with U+FFFD); the pre-sanitization
    diagnostics live in ``metadata``.

    ``source_text`` is the decoded file text exactly as read — before BOM
    stripping, newline normalization, and invisible-char sanitization.  It
    is the faithful "open the file in an editor" view (what PyCharm shows)
    and must never be modified by any downstream stage.
    """

    text: str
    metadata: dict = field(default_factory=dict)
    source_text: str = ""


# Plain-text source-size guard. The comfort cap is calibrated to
# parser throughput (real corpora ~0.9 MB/s; extreme short-line +
# CJK-dense synthetic loads ~0.25-0.33 MB/s). BaseParser default
# None = no cap (opt-in); the four plain-text narrative parsers
# (txt/md/rst/tex) enable the default 16 MiB cap.
TEXT_SOURCE_LIMITS_VERSION = "text_source_limits_v1"
DEFAULT_TEXT_MAX_SOURCE_BYTES = 16 * 1024 * 1024  # 16 MiB

# Hard ceiling: a parser that explicitly declares max_source_bytes
# gets its declared value clamped to this — the "willing to wait
# longer" path also needs a bound (memory: a 128 MiB CJK source
# peaks at ~1 GB across decode-stage copies; time: ~2.4 min real
# corpora, ~6.5-8.5 min extreme synthetic). None semantics unchanged
# (undeclared policy = no cap); opting out of the narrative cap
# requires an explicit large number, which gets clamped.
ABSOLUTE_TEXT_MAX_SOURCE_BYTES = 128 * 1024 * 1024  # 128 MiB

# Oversize policy: auto (default) / strict / truncate.
# auto: the comfort cap (max_source_bytes) is only a reporting line —
#   files below the hard ceiling are always parsed in full (slow
#   lane; warnings record the fact, status is not downgraded); above
#   the hard ceiling, parse is refused. Refusal is the last line of
#   defense, not the first reaction.
# strict: exceeding the comfort cap refuses (honest failure, FAILED
#   + RESOURCE_LIMIT).
# truncate: explicit opt-in — keep the first N bytes + PARTIAL.
# Matching: only exact "truncate"/"strict" take their paths; every
# other value (including the default) behaves as auto.
OVERSIZE_POLICY_AUTO = "auto"
OVERSIZE_POLICY_TRUNCATE = "truncate"
OVERSIZE_POLICY_STRICT = "strict"


class SourceSizeLimitError(Exception):
    """Honest-failure carrier for oversize files under the strict policy.

    The message carries the stable prefix ``resource guard engaged``
    (matched by ingestion_status's _RESOURCE_LIMIT_MARKERS -> FAILED
    + RESOURCE_LIMIT); byte counts, cap, and hard ceiling are all
    in the text. Each parser's ``except Exception`` fallback converts
    it into a parse_failed document — parsers stay unaware.
    """


class BaseParser(ABC):
    """Abstract base class for all parsers.

    Subclasses call :meth:`_read_and_diagnose` at the top of
    ``parse()`` to get the shared read-decode-diagnose-normalize
    product (a ParseContext), then add only their format-specific
    logic (structured_data, headings, etc.).
    """

    # Default decode encoding, overridable by subclasses (declared
    # on the base class so _read_and_diagnose reads one place:
    # encoding or self.encoding).
    encoding: str = "utf-8"

    # Source-size guard: None = undeclared policy, no cap; a declared
    # cap refuses when exceeded (strict, honest failure) unless
    # "truncate" is set explicitly (first N bytes + PARTIAL); sha256
    # always hashes the full file stream — see the constants above.
    max_source_bytes: Optional[int] = None

    # Oversize policy: "auto" (default: full parse below the hard
    # ceiling + a warning; refuse above it) | "strict" (refuse at
    # the cap) | "truncate" (opt-in truncation).
    oversize_policy: str = OVERSIZE_POLICY_AUTO

    # ==================================================
    # Shared read-decode-diagnose-normalize pipeline
    # ==================================================

    def _read_and_diagnose(
        self,
        file_path: str,
        file_type: str,
        encoding: Optional[str] = None,
        tolerant: bool = True,
    ) -> ParseContext:
        """Read raw bytes, decode, normalize, diagnose, then sanitize.

        This is the common pipeline shared by all parsers.  Subclasses call
        this at the top of ``parse()`` and then add only the format-specific
        logic (structured_data extraction, headings, etc.).

        Pipeline order (contract):
            1. read + sha256 + binary suspicion
            2. decode (fallback chain, U+FFFD on hard failure)
            3. strip BOM + normalize newlines
            4. diagnostics on the *original* text (warning layer sees raw content)
            5. semantic signals (also on the original text)
            6. normalize invisible characters (transform layer): U+200B becomes
               a normal space; unsafe characters become U+FFFD; stats are
               recorded in metadata["sanitization"] + a warning is appended

        Returns a :class:`ParseContext` with the sanitized ``text`` and a
        fully-populated ``metadata`` dict (everything except
        ``parse_duration_ms`` which the subclass sets after its own work).
        """

        path = Path(file_path)
        metadata = _build_metadata(file_path)
        metadata["file_type"] = file_type

        # Source-size guard: a declared cap above the hard ceiling is
        # clamped (recorded in reasons, never silently). Oversize
        # dispatch: auto (default) — full parse below the hard ceiling,
        # the comfort cap is only a warnings fact (status not
        # downgraded), above the hard ceiling refuse; strict — refuse
        # at the cap (honest failure, each parser's exception fallback
        # turns it into parse_failed); truncate — explicit opt-in,
        # first N bytes + PARTIAL (sha256 still hashes the full file
        # stream — truncation affects the parse input, not the
        # content-addressed fingerprint).
        size_limit = self.max_source_bytes
        comfort_cap_warning = None  # auto slow-lane report (appended after warnings are rebuilt)
        clamped = False
        if (
            size_limit is not None
            and size_limit > ABSOLUTE_TEXT_MAX_SOURCE_BYTES
        ):
            clamped = True
            size_limit = ABSOLUTE_TEXT_MAX_SOURCE_BYTES
        file_size = path.stat().st_size
        oversize = size_limit is not None and file_size > size_limit
        if oversize and self.oversize_policy != OVERSIZE_POLICY_TRUNCATE:
            clamp_note = (
                " (clamped from the requested cap)" if clamped else ""
            )
            if self.oversize_policy == OVERSIZE_POLICY_STRICT:
                raise SourceSizeLimitError(
                    f"text resource guard engaged (strict policy): "
                    f"file is {file_size} bytes, cap is {size_limit}"
                    f"{clamp_note}; parse refused — raise max_source_bytes "
                    f"(hard ceiling {ABSOLUTE_TEXT_MAX_SOURCE_BYTES}) or "
                    f"switch oversize_policy to 'auto' or 'truncate'"
                )
            if file_size > ABSOLUTE_TEXT_MAX_SOURCE_BYTES:
                raise SourceSizeLimitError(
                    f"text resource guard engaged (auto policy): file is "
                    f"{file_size} bytes, above the hard ceiling "
                    f"{ABSOLUTE_TEXT_MAX_SOURCE_BYTES}; parse refused — "
                    f"split the file, or accept a partial read via "
                    f"oversize_policy='truncate'"
                )
            # auto + below the hard ceiling: the comfort cap is only
            # a fact report; parse in full (slow lane). warnings are
            # rebuilt later by build_warnings, so the message is held
            # in a local and appended after the rebuild (same mount
            # pattern as the encoding_fallback disclosure).
            comfort_cap_warning = (
                f"source size {file_size} bytes exceeds the comfort cap "
                f"{size_limit} (max_source_bytes); parsed in full below "
                f"the hard ceiling {ABSOLUTE_TEXT_MAX_SOURCE_BYTES} "
                f"bytes — expect a slower parse"
            )
            oversize = False
        if oversize:
            with path.open("rb") as fh:
                raw_bytes = fh.read(size_limit)
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            metadata["sha256"] = digest.hexdigest()
            metadata["resource_guard"] = {
                "scheme": "resource_limits_v1",
                "limits": {
                    "version": TEXT_SOURCE_LIMITS_VERSION,
                    "max_source_bytes": size_limit,
                },
                "reasons": [
                    f"source bytes beyond {size_limit} not read "
                    f"(total {file_size}, max_source_bytes={size_limit}; "
                    f"byte cut may split a multibyte character)"
                ],
                "source_bytes_read": len(raw_bytes),
                "source_bytes_total": file_size,
            }
            if clamped:
                metadata["resource_guard"]["reasons"].insert(
                    0,
                    f"requested max_source_bytes={self.max_source_bytes} "
                    f"clamped to hard ceiling {size_limit}",
                )
            # The cap point can split a multibyte character: strict
            # decoding then fails, statistical detection kicks in and
            # a pure-CJK prefix is often misdetected as cp932/cp949,
            # garbling the whole file. When the only strict failure is
            # a trailing incomplete sequence ("unexpected end of
            # data"), drop the trailing bytes and decode normally with the
            # declared encoding; the dropped-tail fact is appended to
            # guard reasons, never silent. (Non-truncated files never
            # take this branch — genuine corruption is still caught by
            # decode_errors observation.)
            declared = encoding or self.encoding
            try:
                raw_bytes.decode(declared)
            except UnicodeDecodeError as exc:
                if (
                    exc.reason == "unexpected end of data"
                    and 0 <= exc.start >= len(raw_bytes) - 4
                ):
                    dropped = len(raw_bytes) - exc.start
                    raw_bytes = raw_bytes[: exc.start]
                    guard = metadata["resource_guard"]
                    guard["source_bytes_read"] = len(raw_bytes)
                    guard["reasons"].append(
                        f"trailing incomplete multibyte sequence dropped "
                        f"({dropped} byte(s)) so the declared encoding "
                        f"'{declared}' stays decodable"
                    )
            except LookupError:
                pass  # unknown encoding name: safe_decode's chain handles it
        else:
            raw_bytes = path.read_bytes()
            metadata["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
        metadata["binary_suspected"] = is_binary_suspected(raw_bytes)

        # decode
        decoded = safe_decode(
            raw_bytes,
            encoding or self.encoding,
            tolerant,
        )
        # Source snapshot: exactly what the decoded file contains — before
        # BOM stripping, newline normalization, or invisible-char sanitization.
        # Consumers (report "Source Document" panel, raw artifact) use it to
        # present the file the way an editor would show it.
        source_text = decoded.text
        text = decoded.text
        text = strip_bom(text)
        text = normalize_newlines(text)

        metadata.update({
            "encoding_used": decoded.encoding_used,
            "encoding_fallback": decoded.encoding_fallback,
            "encoding_detection": decoded.encoding_detection,
            "decode_trace": decoded.decode_trace,
        })

        # diagnostics — run on pre-sanitization text so warnings
        # reflect the original content
        diagnostics = build_diagnostics(
            replacement_chars=count_replacement_chars(text),
            suspicious_chars=count_suspicious_chars(text),
            printable_ratio=printable_ratio(text),
            symbol_ratio=symbol_ratio(text),
            empty_line_ratio=empty_line_ratio(text),
            hidden_unicode=detect_hidden_unicode(text),
            contains_ansi_escape=contains_ansi_escape(text),
            emoji_ratio=emoji_ratio(text),
            repetition=detect_repetition(text),
            blank_run_excess_ratio=blank_run_excess_ratio(text),
        )
        metadata["diagnostics"] = diagnostics
        metadata["warnings"] = build_warnings(diagnostics)

        # auto slow-lane report: the full-parse fact between comfort
        # cap and hard ceiling, same mount pattern as the
        # encoding_fallback disclosure (appended after rebuild).
        if comfort_cap_warning is not None:
            metadata["warnings"].append(comfort_cap_warning)

        # encoding fallback disclosure: the warnings builder only sees
        # diagnostics (ten fixed keys) and never the top-level
        # encoding_fallback — disclose the decode-detection failure /
        # forced utf-8 replace event here (same mount pattern as the
        # security-carrier census).
        if decoded.encoding_fallback:
            metadata["warnings"].append(
                "encoding fallback applied: detection rejected, "
                "forced utf-8 replace"
            )

        # Security-carrier census on the same pre-sanitization view:
        # fact codes appended to warnings (family-aggregated after
        # tiering normalization); structured counts land in
        # metadata["security_carriers"] (written only when non-zero,
        # same style as sanitization) for external consumers reading
        # run artifacts directly.
        carrier_census = detect_security_carriers(text)
        if carrier_census:
            metadata["security_carriers"] = carrier_census
            metadata["warnings"].extend(build_carrier_facts(carrier_census))

        metadata["semantic_signals"] = extract_semantic_signals(text)

        # Normalize after diagnostics so raw findings remain traceable.
        sanitized_text, sanitization = sanitize_invisible_chars(text)
        if sanitization:
            metadata["sanitization"] = sanitization
            normalized = sum(
                sanitization.get(key, 0)
                for key in (
                    "zero_width_space", "bidi_resolved", "bidi_unresolved",
                    "zero_width", "mid_file_bom", "ansi_escape", "control_char",
                    "form_feed", "line_sep", "tag_replaced",
                    "vs_stripped", "zwj_stripped",
                )
            )
            if normalized:
                metadata["warnings"].append(
                    f"{normalized} invisible characters normalized "
                    "(see metadata.sanitization)"
                )
            # U+FFFD double-role accounting: diagnostics.
            # replacement_chars counts the pre-sanitization text
            # (source-native / decode-corrupted); U+FFFD injected by
            # sanitize is accounted separately — only their sum is
            # the total U+FFFD in original_text. BiDi: the resolved
            # part (bidi_resolved) drops markers without injecting;
            # only the degraded part injects.
            injected = sum(
                sanitization.get(key, 0)
                for key in (
                    "bidi_unresolved", "zero_width",
                    "ansi_escape", "control_char", "tag_replaced",
                )
            )
            if injected:
                metadata["sanitization"]["replacement_injected"] = injected

        return ParseContext(
            text=sanitized_text,
            metadata=metadata,
            source_text=source_text,
        )

    # ==================================================
    # Abstract interface
    # ==================================================

    @abstractmethod
    def parse(self, file_path) -> Document:
        """Parse a single file into a :class:`Document`."""
        ...

    def parse_many(self, file_paths):
        """Parse files one by one; returns a Document list matching the
        input length."""
        return [self.parse(fp) for fp in file_paths]
