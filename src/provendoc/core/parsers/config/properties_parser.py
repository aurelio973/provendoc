# ============================================================
# Structured Config Domain
# Java-style .properties Parser
# ============================================================
import logging
import os
import re
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)

_log = logging.getLogger(__name__)

# ============================================================
# escape decoding facts
# ============================================================

#: known escape characters (standard control chars + syntax
#: chars and whitespace that need escaping)
_KNOWN_ESCAPES = frozenset("ntrf\\:= #!\"'")

#: control-char escape map (any other \c literal escape drops
#: the backslash and keeps the char, per Java semantics)
_ESC_MAP = {"n": "\n", "t": "\t", "r": "\r", "f": "\f"}

# ============================================================
# filename candidates (exact basename matching)
# ============================================================

#: ResourceBundle shape: messages[_lang][_COUNTRY].properties
#: (the country segment requires the language segment first —
#: Java locale rules; case-sensitive)
_FILENAME_BUNDLE_RE = re.compile(
    r"^messages(?:_([a-z]{2})(?:_([A-Z]{2}))?)?\.properties$"
)

#: Spring profile shape: application-<profile>.properties
_FILENAME_PROFILE_RE = re.compile(r"^application-([a-z0-9-]+)\.properties$")

#: build-framework identity shapes: gradle.properties /
#: pom.properties (Maven META-INF basenames — exact match, no
#: path guessing)
_BUILD_FRAMEWORK_NAMES = frozenset((
    ("gradle.properties", "gradle"),
    ("pom.properties", "maven"),
))

#: \uXXXX hex segment validation
_HEX4_RE = re.compile(r"^[0-9a-fA-F]{4}$")

#: dotted-path / array-index candidate — whole-key word segments
#: plus optional [N] indices (escaped or non-ASCII segments do
#: not match — ambiguous keys get zero candidates to avoid
#: false hits)
_KEY_PATH_CANDIDATE_RE = re.compile(
    r"^[\w-]+(?:\[\d+\])?(?:\.[\w-]+(?:\[\d+\])?)*$"
)


class PropertiesParser(BaseParser):
    """Java-style ``.properties`` parser (recovery mode).

    Syntax facts (aligned with the Java Properties spec):
        - separators: ``=`` / ``:`` / the first unescaped
          whitespace (all three are legal)
        - comments: ``#`` or ``!`` at line start (only the
          unescaped form at the start of a logical line)
        - continuation: an odd run of trailing backslashes
          continues the line (even = escaped backslash, no
          continuation; an escaped trailing space ``\\ ``
          does not continue — the decision is based on the
          raw line ending, not on rstripped text)
        - escapes: ``\\n \\t \\r \\f \\\\ \\uXXXX`` plus any
          ``\\c`` literal escape (Java semantics: drop the
          backslash, keep the char; unknown escapes register
          decode_errors fact codes)
        - surrogates: ``\\uD800-\\uDBFF`` followed by
          ``\\uDC00-\\uDFFF`` merges into one code point;
          isolated/reversed surrogates keep the character
          and register an invalid_unicode_surrogate fact code
        - value span: from the first non-whitespace char
          after the separator to end of line (trailing
          whitespace belongs to the value); leading whitespace
          of continuation lines is stripped
        - duplicate keys: Java Properties semantics = last
          wins (the later one overrides the earlier)
    """

    def __init__(self, encoding="utf-8-sig", type_infer=True):
        """Initialize with an encoding and a type-inference
        switch."""
        self.encoding = encoding
        self.type_infer = type_infer

    def parse(self, file_path) -> Document:
        """Parse a .properties file into a Document (duplicate
        keys: last wins)."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(file_path, "properties", self.encoding, True)
            text = ctx.text
            metadata = ctx.metadata

            # filename candidates (ResourceBundle / Spring
            # profile) — keys are written only on an exact
            # basename match (zero drift)
            self._stamp_filename_hints(metadata, file_path)

            logical_lines = self._logical_lines(text)
            flat = self._parse_flat(logical_lines)

            metadata["parse_success"] = not any(
                k == "_error" for k in flat
            )
            metadata["top_level_keys"] = list(flat.keys())

            metadata["structured_data"] = {
                "titles": list(flat.keys()),
                "contents": [{"section": "", "items": dict(flat)}],
                "tables": [],
                "structure": dict(flat),
            }

            metadata["config_entries"] = self._build_contract(logical_lines)

            # Object-level unresolved registry: derived from
            # existing fact sources (reference three-state /
            # last_wins shadowed occurrences) in the contract;
            # original keys are not rewritten, only non-empty
            # writes; guarded silent degradation, never blocks
            # the main parse.
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text, metadata=metadata, source_text=ctx.source_text,
            )
        except Exception as e:
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                    "structured_data": {"_error": str(e)},
                },
            )

    # ==================================
    # logical line assembly (continuation)
    # ==================================

    @staticmethod
    def _logical_lines(text):
        """Merge continuation lines into logical lines, keeping
        (start_line, end_line, text).

        Java Properties continuation rule: count the
        backslash run at the **raw line ending** (no rstrip —
        rstrip would destroy the premise for judging the
        ``value\\ `` escaped-trailing-whitespace shape):
            - odd run → continuation (strip the final
              continuation-marker backslash; the rest of the
              run is left to the decoding layer per escape
              semantics);
            - even run or 0 → the logical line ends (the
              tail is an escaped backslash or nothing).
        Leading whitespace of continuation lines is stripped
        (Java spec); trailing whitespace of continuation
        lines (both the ``\\ `` escaped form and plain
        whitespace) is kept for the value span — the value
        runs to end of line.
        """
        result = []
        current = None  # [start_idx, end_idx, parts]
        for i, raw in enumerate(text.splitlines()):
            if current is None:
                current = [i, i, [raw]]
            else:
                # continuation line: strip leading whitespace
                # (Java spec)
                current[1] = i
                current[2].append(raw.lstrip(" \t\f"))
            # count the backslash run at the raw line ending
            # (no rstrip)
            run = 0
            j = len(raw) - 1
            while j >= 0 and raw[j] == "\\":
                run += 1
                j -= 1
            if run % 2 == 1:
                # continuation: strip the final marker
                # backslash (the rest of the run stays)
                current[2][-1] = current[2][-1][:-1]
                continue
            result.append((current[0], current[1], "".join(current[2])))
            current = None
        if current is not None:
            # file ends on a dangling continuation: the trailing
            # backslash was already stripped, close it as a
            # logical line
            result.append((current[0], current[1], "".join(current[2])))
        return result

    def _parse_flat(self, logical_lines):
        flat = {}
        for _start, _end, line in logical_lines:
            kv = self._split_line(line)
            if kv is None:
                continue
            key, val = kv
            flat[key] = (
                self._infer_type(val) if self.type_infer else val
            )
        return flat

    # ==================================
    # key/value splitting
    # ==================================

    @staticmethod
    def _rstrip_unescaped(s):
        """Strip trailing **unescaped** whitespace (escaped
        whitespace ``\\ `` is content and is kept).

        Rule: an even backslash run (or 0) before the
        trailing whitespace char → it is unescaped,
        strippable; odd → escaped, stop.
        """
        end = len(s)
        while end > 0 and s[end - 1] in " \t\f":
            run = 0
            j = end - 2
            while j >= 0 and s[j] == "\\":
                run += 1
                j -= 1
            if run % 2 == 1:
                break
            end -= 1
        return s[:end]

    @classmethod
    def _split_line(cls, line):
        """One logical line → (key, value); None for
        comment/blank/valueless lines."""
        kv = cls._split_detailed(line)
        if kv is None:
            return None
        return kv["key"], kv["value"]

    @classmethod
    def _split_detailed(cls, line):
        """One logical line → fact dict; None for comment/blank
        lines.

        Single pass, skipping two characters at each
        ``\\``, **never mutating the original string** —
        split at the first unescaped separator; key and
        value each go through the decoding layer (replaces
        the old ``\\x00`` placeholder mutation scheme and
        eliminates index misalignment).

        Returned keys:
            key / value              — decoded key and value
            separator                — whether a separator exists
                                       (bare key = False; a
                                       three-state fact source)
            value_source             — raw lexeme after the
                                       separator (undecoded,
                                       unstripped; None for a
                                       bare key)
            key_sequences / key_errors, value_sequences /
            value_errors             — decoding-layer facts
        """
        clean = line.lstrip(" \t\f")
        if not clean or clean[0] in ("#", "!"):
            return None
        idx = 0
        n = len(clean)
        while idx < n:
            ch = clean[idx]
            if ch == "\\":
                # skip the escaped pair (no mutation of the
                # original string)
                idx += 2
                continue
            if ch in ("=", ":"):
                return cls._assemble_kv(
                    clean[:idx], clean[idx + 1:], separator=True,
                )
            if ch in (" ", "\t", "\f"):
                tail = clean[idx:].lstrip(" \t\f")
                if tail[:1] in ("=", ":"):
                    # = / : right after whitespace: merge into
                    # the separator region
                    tail = tail[1:]
                return cls._assemble_kv(
                    clean[:idx], tail, separator=True,
                )
            idx += 1
        # no separator: key only (escaped whitespace etc.
        # belongs to the key), value is an empty string
        key, kseq, kerr = cls._unescape_detailed(clean)
        if not key:
            return None
        return {
            "key": key,
            "value": "",
            "separator": False,
            "value_source": None,
            "key_sequences": kseq,
            "key_errors": kerr,
            "value_sequences": [],
            "value_errors": [],
        }

    @classmethod
    def _assemble_kv(cls, key_region, value_region, separator):
        """The two regions around a separator → fact dict.

        key: trailing unescaped whitespace stripped (Java:
        whitespace before the separator is ignorable),
        escaped whitespace kept as part of the key. value:
        leading ignorable whitespace stripped (trailing
        whitespace is kept to end of line — no rstrip);
        value_source keeps the raw lexeme after the
        separator (source fidelity).
        """
        kraw = cls._rstrip_unescaped(key_region)
        key, kseq, kerr = cls._unescape_detailed(kraw)
        vraw = value_region.lstrip(" \t\f")
        val, vseq, verr = cls._unescape_detailed(vraw)
        return {
            "key": key,
            "value": val,
            "separator": separator,
            "value_source": value_region if separator else None,
            "key_sequences": kseq,
            "key_errors": kerr,
            "value_sequences": vseq,
            "value_errors": verr,
        }

    # ==================================
    # escape decoding
    # ==================================

    @classmethod
    def _unescape_detailed(cls, value):
        """Properties escape decoding (Java semantics) + fact
        recovery.

        Returns (decoded, escape_sequences, decode_errors):
            - escape_sequences: each escape lexeme verbatim
              (a merged surrogate pair counts as 2 sequences,
              1 code point)
            - decode_errors: fact-code list
                ``unknown_escape:<c>``               — non-standard escape char
                ``incomplete_unicode_escape``        — incomplete/illegal \\uXXXX
                ``dangling_backslash``               — dangling backslash
                ``invalid_unicode_surrogate:U+XXXX`` — isolated/reversed surrogate

        Java semantics: any ``\\c`` drops the backslash and
        keeps the char (unknown forms register a fact code
        while the semantics stay correct — wrong semantics
        are not a contract); a \\uXXXX high surrogate
        followed by a low surrogate merges into one code
        point (UTF-16 semantic equivalence), other
        surrogate shapes keep the character and register
        a fact code; incomplete \\uXXXX / dangling
        backslashes keep the original text conservatively
        (decodability first, no fact loss).
        """
        out = []
        seqs = []
        errs = []
        i = 0
        n = len(value)
        while i < n:
            ch = value[i]
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= n:
                out.append("\\")
                errs.append("dangling_backslash")
                i += 1
                continue
            nxt = value[i + 1]
            if nxt == "u":
                hexpart = value[i + 2:i + 6]
                if _HEX4_RE.match(hexpart):
                    cp = int(hexpart, 16)
                    if 0xD800 <= cp <= 0xDBFF:
                        # high surrogate: merges only when
                        # followed by \uDC00-DFFF
                        tail = value[i + 6:i + 12]
                        if value[i + 6:i + 8] == "\\u" \
                                and _HEX4_RE.match(tail[2:]):
                            cp2 = int(tail[2:], 16)
                            if 0xDC00 <= cp2 <= 0xDFFF:
                                combined = (
                                    0x10000
                                    + (cp - 0xD800) * 0x400
                                    + (cp2 - 0xDC00)
                                )
                                out.append(chr(combined))
                                seqs.append(value[i:i + 6])
                                seqs.append(value[i + 6:i + 12])
                                i += 12
                                continue
                        # isolated high surrogate
                        out.append(chr(cp))
                        seqs.append(value[i:i + 6])
                        errs.append(f"invalid_unicode_surrogate:U+{cp:04X}")
                        i += 6
                        continue
                    if 0xDC00 <= cp <= 0xDFFF:
                        # isolated low surrogate /
                        # reversed-pair first half
                        out.append(chr(cp))
                        seqs.append(value[i:i + 6])
                        errs.append(f"invalid_unicode_surrogate:U+{cp:04X}")
                        i += 6
                        continue
                    out.append(chr(cp))
                    seqs.append(value[i:i + 6])
                    i += 6
                    continue
                # incomplete / illegal \uXXXX: conservatively
                # keep the original text
                out.append(value[i:])
                errs.append("incomplete_unicode_escape")
                break
            # \c literal escape (Java: drop the backslash,
            # keep the char)
            seqs.append(value[i:i + 2])
            out.append(_ESC_MAP.get(nxt, nxt))
            if nxt not in _KNOWN_ESCAPES:
                errs.append(f"unknown_escape:{nxt}")
            i += 2
        return "".join(out), seqs, errs

    @staticmethod
    def _infer_type(value):
        # shared with env_parser (config/contracts.py)
        return cc.infer_scalar(value)

    # ==================================
    # filename candidates
    # ==================================

    @staticmethod
    def _stamp_filename_hints(metadata, file_path):
        """Filename candidate disclosure (ResourceBundle /
        Spring profile).

        Keys are written only on an exact basename match
        (ordinary filenames get zero keys — zero drift).
        fallback_chain is derived statically from the
        file's own name (locale segments erased step by
        step); ``resolved`` is always False — a single-file
        view never claims runtime fallback results.
        """
        try:
            fname = os.path.basename(str(file_path))
        except Exception:  # pragma: no cover - defensive guard
            return
        m = _FILENAME_BUNDLE_RE.match(fname)
        if m:
            lang, country = m.group(1), m.group(2)
            locale = "_".join(p for p in (lang, country) if p) or None
            parts = ["messages"]
            if lang:
                parts.append(f"messages_{lang}")
            if lang and country:
                parts.append(f"messages_{lang}_{country}")
            chain = [f"{p}.properties" for p in reversed(parts)]
            metadata["resource_bundle"] = {
                "bundle": "messages",
                "locale": locale,
                "fallback_chain": chain,
                "resolved": False,
            }
            return
        m = _FILENAME_PROFILE_RE.match(fname)
        if m:
            metadata["profile_candidate"] = {
                "value": m.group(1),
                "source": "filename",
                "confidence": 0.96,
            }
            return
        for exact, framework in _BUILD_FRAMEWORK_NAMES:
            if fname == exact:
                metadata["build_framework_candidate"] = {
                    "value": framework,
                    "source": "filename",
                    "confidence": 0.95,
                }
                return

    # ==================================
    # config entry contract
    # ==================================

    def _build_contract(self, logical_lines):
        """Build the ConfigEntry contract (flat key space,
        last_wins override chains)."""
        contract = cc.build_contract(
            "properties", cc.DUP_LAST_WINS,
            line_numbers=True, comments=True,
        )
        reasons = contract["loss"]["reasons"]
        reasons.extend([
            "flat_key_space: dotted keys are kept verbatim, not nested",
        ])

        entries = contract["entries"]
        comments = contract["comments"]
        entry_map = {}

        pending_block = None  # (start, end, text)

        def flush_block(target_path):
            """Bind the pending block comment to a target path and
            write it into the contract."""
            nonlocal pending_block
            if pending_block is None:
                return
            s, e, t = pending_block
            # logical_lines are (start, end, line) tuples; the
            # comment echo takes the line texts
            block_lines = [
                line for i, _ie, line in logical_lines if s <= i <= e
            ]
            comments.append(cc.make_comment(
                "block", t, "\n".join(block_lines), s, e,
                bound_path=target_path, bound_kind="key" if target_path else None,
            ))
            pending_block = None

        for start, end, line in logical_lines:
            clean = line.lstrip(" \t\f")
            if not clean or clean[0] in ("#", "!"):
                comment_text = clean.lstrip("#!").strip()
                if pending_block is None:
                    pending_block = (start, end, comment_text)
                else:
                    s, _, t = pending_block
                    pending_block = (s, end, t)
                continue
            kv = self._split_detailed(line)
            if kv is None:
                continue
            key, val = kv["key"], kv["value"]
            flush_block([key])
            facts = self._collect_facts(kv, start, end)
            if key in entry_map:
                existing = entry_map[key]
                if existing["override_chain"] is None:
                    existing["override_chain"] = [
                        {"value": existing["raw_value"], "line": existing["line_start"]},
                        {"value": val, "line": start},
                    ]
                else:
                    existing["override_chain"].append({"value": val, "line": start})
                existing["duplicate"] = True
                existing["raw_value"] = val
                existing["value"] = (
                    self._infer_type(val) if self.type_infer else val
                )
                existing["line_start"] = start
                existing["line_end"] = end
                # additive disclosure keys reset to the latest
                # occurrence's facts (idempotent)
                self._apply_facts(existing, facts)
            else:
                entry = cc.make_entry(
                    [key], key, val,
                    self._infer_type(val) if self.type_infer else val,
                    line_start=start, line_end=end,
                )
                self._apply_facts(entry, facts)
                entry_map[key] = entry
                entries.append(entry)

        flush_block(None)
        cc.fill_child_counts(entries, contract["containers"])
        # sensitive disclosure (shared dual-channel helper; zero
        # keys zero reasons when nothing hits — zero drift).
        sensitive_count = cc.scan_sensitive_entries(entries)
        if sensitive_count:
            reasons.append(f"sensitive_entry_values:{sensitive_count}")
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")
        return contract

    @staticmethod
    def _collect_facts(kv, start, end):
        """Additive disclosure key set (written only when the
        fact exists — zero drift).

        - physical_lines: the physical-line span of a
          continuation entry (omitted for a single physical
          line); line_end already anchors the last physical
          line
        - source_value: the raw lexeme after the separator —
          written when it differs from the decoded value or
          is an empty string (an empty string is also a
          "separator exists" fact, keeping
          empty_string / whitespace_string / key_without_value
          three-state decidable); bare keys never carry it
        - escape_sequences: key+value escape lexemes combined
        - decode_errors: key+value fact codes combined
        """
        facts = {}
        if end > start:
            facts["physical_lines"] = list(range(start, end + 1))
        if kv["separator"]:
            src = kv["value_source"]
            if src != kv["value"] or src == "":
                facts["source_value"] = src
        seqs = kv["key_sequences"] + kv["value_sequences"]
        if seqs:
            facts["escape_sequences"] = seqs
        errs = kv["key_errors"] + kv["value_errors"]
        if errs:
            facts["decode_errors"] = errs
        # only purely literal keys (no escapes before
        # decoding) get a path candidate — an ambiguous key
        # like an escaped dot is indistinguishable from a
        # literal key after decoding, so zero candidates
        # avoid false hits
        if not kv["key_sequences"]:
            candidate = PropertiesParser._key_path_candidate(kv["key"])
            if candidate:
                facts["path_candidate"] = candidate
        return facts

    @staticmethod
    def _key_path_candidate(key):
        """Dotted-path + array-index candidate (a disclosure
        layer, not an interpretation layer).

        When the whole key matches the word-segment pattern
        with optional ``[N]`` indices, it expands to a
        segment list (indices become decimal string
        segments); a single segment is not written, and
        ambiguous keys with escapes or special characters
        are not written (the flat_key_space verbatim-keeping
        semantics are unchanged).
        """
        if not _KEY_PATH_CANDIDATE_RE.match(key):
            return None
        segments = []
        for part in key.split("."):
            idx = part.find("[")
            if idx >= 0:
                segments.append(part[:idx])
                segments.append(part[idx + 1:-1])
            else:
                segments.append(part)
        if len(segments) <= 1:
            return None
        return segments

    @staticmethod
    def _apply_facts(entry, facts):
        """Write the additive fact keys into the entry (reset
        first, then write; idempotent)."""
        for k in ("physical_lines", "source_value",
                  "escape_sequences", "decode_errors",
                  "path_candidate"):
            entry.pop(k, None)
        entry.update(facts)

    # ==================================
    # multi-file cascading merge (merge logic lives in the
    # shared cc.merge_contracts layer)
    # ==================================

    _FILENAME_HINT_KEYS = (
        "resource_bundle", "profile_candidate", "build_framework_candidate",
    )

    def parse_many_merged(self, file_paths):
        """Multi-file cascading merge: later files shadow
        earlier ones (the same direction as Spring profile
        layering / ResourceBundle fallback / kustomize-helm;
        the ascending-specificity order is supplied by the
        caller or via ``order_specificity``).

        Each file goes through the existing single-file
        contract channel (with all disclosure semantics);
        the merge logic goes through the shared
        ``cc.merge_contracts`` layer. properties specifics:
        - flat key space — no multi-document, doc_index is
          always 0, no collision surface
        - containers always empty
        - filename_hints: a pass-through snapshot only for
          source files carrying candidates
          ({file_index, file_name, ...candidate keys}; the
          merged result is not a Document, so hints must
          travel explicitly to stay visible; sources with
          zero candidates are not listed)

        Returns the merged contract dict (not a Document —
        across files there is no single original text;
        line-number semantics are attributed via
        source_file). The base parse_many semantics stay
        frozen.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        filename_hints = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            c = meta.get("config_entries") or cc.build_contract(
                "properties", cc.DUP_LAST_WINS,
            )
            contracts.append(c)
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
            hints = {
                k: meta[k]
                for k in self._FILENAME_HINT_KEYS if k in meta
            }
            if hints:
                filename_hints.append({
                    "file_index": i,
                    "file_name": meta.get("file_name"),
                    **hints,
                })
        merged = cc.merge_contracts("properties", contracts, sources)
        if filename_hints:
            merged["filename_hints"] = filename_hints
        return merged

    # ==================================
    # chain-discovery helper
    # ==================================

    @staticmethod
    def order_specificity(file_paths):
        """Ascending-specificity ordering (statically decidable
        ResourceBundle / Spring profile shapes; a convenience
        layer, not a correctness layer).

        - ResourceBundle family (messages*.properties of the
          same bundle): base first, locale variants by
          ascending locale-segment count
          (messages < messages_en < messages_en_US)
        - Spring profile family (application*.properties):
          the application.properties base file first, the
          rest keep input order (profile activation order is
          a runtime fact, not guessed statically)
        - non-family members / mixed shapes: passed through
          as-is (ordering responsibility stays with the
          caller)

        Returns a new list; the input is not modified.
        """
        paths = list(file_paths)
        names = [os.path.basename(str(p)) for p in paths]

        def _locale_depth(name):
            m = _FILENAME_BUNDLE_RE.match(name)
            if not m:
                return None
            lang, country = m.group(1), m.group(2)
            return (1 if lang else 0) + (1 if country else 0)

        depths = [_locale_depth(n) for n in names]
        if all(d is not None for d in depths):
            # the whole group is one ResourceBundle family:
            # stable sort by ascending locale-segment count
            return [p for _, p in sorted(
                zip(depths, paths), key=lambda t: t[0]
            )]
        base = "application.properties"
        if all(
            n == base or _FILENAME_PROFILE_RE.match(n)
            for n in names
        ) and base in names:
            # the whole group is one Spring profile family: base
            # file first, the rest keep input order (profile
            # activation order is a runtime fact, not guessed
            # statically; mixed shapes never reach this branch
            # — passed through as-is)
            out = [p for p, n in zip(paths, names) if n == base]
            out.extend(
                p for p, n in zip(paths, names) if n != base
            )
            return out
        return paths
