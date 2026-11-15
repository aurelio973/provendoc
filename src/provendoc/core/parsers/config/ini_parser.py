# ============================================================
# Structured Config Domain
# INI Semantic Structure Parser
# ============================================================
import logging
import os
import re
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import sensitive_signals
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)

_log = logging.getLogger(__name__)

# ------------------------------------------------------------
# INI dialect closure: well-known dialect filenames map to a
# contract profile (dual condition: dialect filename +
# structural shape; generic .ini gets zero new keys, zero
# drift).
# ------------------------------------------------------------
_DIALECT_FILES = {
    ".gitconfig": "gitconfig",
    ".gitmodules": "gitconfig",
    ".editorconfig": "editorconfig",
    ".npmrc": "npmrc",
    ".coveragerc": "coveragerc",
    ".pylintrc": "pylintrc",
}

# gitconfig subsection shape: `section "subsection"` (inside
# quotes, \" and \\ escapes are allowed; the subsection is
# case-sensitive, section/key fold case).
# Possessive quantifiers prevent super-linear backtracking; the
# alternation branches have disjoint leading chars (`\` only
# matches via `\\.`), so possessive semantics are strictly
# equivalent. Python < 3.11 lacks possessive syntax → fall back
# to an equivalent slower form (identical behavior, only
# failing-path performance differs).
try:
    _GITCONFIG_SECTION_RE = re.compile(
        r'^([A-Za-z][\w-]*)\s+"((?:[^"\\]++|\\.)++)"\s*$'
    )
except re.error:  # pragma: no cover - Python < 3.11 回退
    _GITCONFIG_SECTION_RE = re.compile(
        r'^([A-Za-z][\w-]*)\s+"((?:[^"\\]|\\.)*)"\s*$'
    )
_SUBSECTION_ESCAPE_RE = re.compile(r"\\(.)")

# editorconfig glob section detection chars (static-rule
# pattern registration)
_GLOB_CHARS = ("*", "?", "{", "}")

# npmrc env-var reference shape (used for ${NPM_TOKEN}
# disclosure; same criteria as the family-level
# contracts._ENV_VAR_RE)
_NPMRC_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

_SENSITIVE_MASK = "****"


class IniParser(BaseParser):
    """INI parser (recovery semantics).

    Maximizes semantic recovery and tolerates malformed input;
    losses are recorded truthfully in the contract's
    loss.reasons. (No strict mode: it had zero callers, and
    fail-fast conflicts with ingestion positioning.)
    """

    def __init__(self, encoding="utf-8-sig",
                 comment_markers=(";", "#"),
                 nest_sections=False, auto_add_section=False,
                 interpolation=False, type_infer=True):
        """
        :param encoding: file encoding; defaults to utf-8-sig for BOM tolerance
        :param comment_markers: comment marker set
        :param nest_sections: True = nest dotted section names into a nested structure
        :param auto_add_section: True = add a section automatically when missing
        :param interpolation: True = enable multi-syntax variable parsing (%(key)s, ${section.key}, ${key}, ${{key}}) with [DEFAULT] global variables
        :param type_infer: True = perform type inference
        """
        self.encoding = encoding
        self.comment_markers = comment_markers
        self.nest_sections = nest_sections
        self.auto_add_section = auto_add_section
        self.interpolation = interpolation
        self.type_infer = type_infer

    # ==================================
    # BaseParser interface
    # ==================================

    def parse(self, file_path) -> Document:
        """Parse an INI file into a Document.

        Returns a Document with:
            - text: raw file text (decoded, BOM-stripped, normalized)
            - metadata: file info + diagnostics + structured_data
        """
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "ini", self.encoding, True
            )
            text = ctx.text
            metadata = ctx.metadata

            # --- INI parsing ---
            ini_data = self._parse_ini(text)

            metadata["parse_success"] = not any(
                s.get("section") == "_error" for s in ini_data if isinstance(s, dict)
            )

            # titles
            titles = [s.get("section", "_error") for s in ini_data if isinstance(s, dict)]
            metadata["top_level_keys"] = titles

            # structure (nesting is done in _parse_ini via
            # _nest_sections_for_list)
            structure_data = self._list_to_dict(ini_data)

            metadata["structured_data"] = {
                "titles": titles,
                "contents": ini_data,
                "tables": [],
                "structure": structure_data,
            }

            # ConfigEntry contract (fact layer; failure does not
            # block parsing; dialect profile detection needs the
            # source filename → file_path is passed in)
            try:
                metadata["config_entries"] = self._build_contract(
                    text, file_path,
                )
            except Exception as e:
                metadata["config_entries"] = cc.build_contract(
                    "ini", cc.DUP_OVERRIDE_CHAIN,
                )
                metadata["config_entries"]["loss"]["reasons"].append(
                    f"contract_build_failed: {e}"
                )

            # Object-level unresolved registry: derived from the
            # contract's existing three-state reference facts (ini
            # declares override_chain; F2 is not derived). Original
            # keys are never rewritten; non-empty writes only;
            # silently degrades under the guard.
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)

            return Document(
                text=text,
                metadata=metadata,
                source_text=ctx.source_text,
            )

        except Exception as e:
            metadata = {
                "parse_failed": True,
                "parse_error": str(e),
                "parse_success": False,
                "structured_data": {
                    "titles": [],
                    "contents": [{"section": "_error", "items": {"error": str(e)}}],
                    "tables": [],
                    "structure": {},
                },
            }
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)
            return Document(text="", metadata=metadata)

    def parse_many(self, file_paths):
        """Parse multiple INI files."""
        return [self.parse(fp) for fp in file_paths]

    # ==================================
    # config entry contract
    # ==================================

    def _build_contract(self, text, filename=None):
        """Build the ConfigEntry contract (contract v1).

        Fact source: an independent line scanner (same semantics
        as the recovery parser: section tolerance, =/:/whitespace
        separators, duplicate keys kept in occurrence order as an
        override chain).

        duplicate_key_semantics: "override_chain" (all
        occurrences kept in order).

        Contract fact boundaries:
            - Values keep the raw fact; interpolation's expanded
              values exist only in structured_data (the contract
              records references, it does not replay expansions)
            - INI has no null/empty-list syntax → only empty_dict
              occurs among the three states
            - [DEFAULT] is not expanded into sections (the
              contract records original ownership)

        Additive dialect keys (all gated on a dialect profile;
        generic .ini gets zero new keys):
            - gitconfig: container subsection/raw_subsection/
              fold_section; entry fold_key/values/multi_value
            - editorconfig: container pattern; entry
              profile_rule(editorconfig_root); header reason
              effective_resolution_not_applied
            - npmrc: entry registry_target/npm_scope/
              sensitive_candidates; header reasons
              env_var_references:N / sensitive_entry_values:N
            - coveragerc/pylintrc: entry candidate_type (weak
              declaration, disclosed but not executed)
            - .cfg content detection: header detected_format/
              confidence/alternative_formats (disclosed, no
              rerouting)
        """
        contract = cc.build_contract("ini", cc.DUP_OVERRIDE_CHAIN)
        reasons = contract["loss"]["reasons"]
        if self.interpolation:
            reasons.append(
                "interpolation_not_applied_in_contract_values"
            )
            # Interpolation I/O prefixes (file:/json:/yaml:) are
            # recognized but not executed; hits are disclosed
            # (io_hits channel, see _replace_vars)
            for prefix in sorted(set(
                getattr(self, "_interpolation_io_hits", None) or []
            )):
                reasons.append(
                    f"interpolation_io_prefix_disabled:{prefix}"
                )

        # Dialect profile (dialect filename; structural shape is
        # detected inside the scanner)
        profile = self._detect_dialect(filename)
        if profile:
            contract["dialect_profile"] = profile
        if profile == "editorconfig":
            # Static rule layer is complete; no effective-attribute
            # computation without target file paths
            reasons.append("effective_resolution_not_applied")

        # .cfg content detection (offline feature sniffing,
        # disclosed without rerouting)
        if filename and profile is None \
                and os.path.basename(str(filename)).lower().endswith(".cfg"):
            sniff = self._sniff_cfg_mismatch(text)
            if sniff:
                contract["detected_format"] = sniff["detected_format"]
                contract["confidence"] = sniff["confidence"]
                contract["alternative_formats"] = sniff[
                    "alternative_formats"
                ]
                reasons.append("suffix_content_mismatch:cfg")
                reasons.append("cfg_sniff_heuristic:feature_ratio")

        entries = contract["entries"]
        containers = contract["containers"]
        comments = contract["comments"]

        # section match key → container record (path_str for
        # non-profiles, identical to the existing convention)
        section_records = {}
        # (section match key, key) → entry (key folded for
        # gitconfig)
        entry_map = {}
        section_seen_count = {}

        current_section_raw = None
        current_sec_key = None
        current_section_path = []
        pending_block = None  # (start, end, text)
        bound_desc = {}

        def flush_block(target_path, target_kind):
            """Bind the pending block comment to a target path and
            record it in the contract."""
            nonlocal pending_block
            if pending_block is None:
                return
            s, e, btext = pending_block
            comments.append(cc.make_comment(
                "block", btext, "\n".join(lines[s:e + 1]), s, e,
                bound_path=target_path,
                bound_kind=target_kind if target_path else None,
            ))
            if target_path:
                bound_desc.setdefault(
                    cc.path_str(target_path), (btext, [s, e])
                )
            pending_block = None

        lines = text.splitlines()
        n_lines = len(lines)
        i = 0
        while i < n_lines:
            raw_line = lines[i]
            clean = raw_line.strip()

            if self._is_comment_line(clean):
                if pending_block is None:
                    pending_block = (i, i, clean.lstrip(";#").strip())
                else:
                    s, _, t = pending_block
                    pending_block = (s, i, t)
                i += 1
                continue

            if not clean:
                i += 1
                continue

            # section header (same tolerance as the recovery
            # parser)
            if clean.startswith("[") or clean.endswith("]"):
                if clean.startswith("[") and clean.endswith("]"):
                    name = clean[1:-1].strip()
                elif clean.startswith("["):
                    name = clean[1:].strip()
                else:
                    name = clean[:-1].strip()

                # gitconfig subsection shape (dual condition:
                # dialect filename + quoted shape; generic .ini
                # gets zero new keys)
                sec_name = name
                sub_raw = None
                sub_dec = None
                if profile == "gitconfig":
                    m = _GITCONFIG_SECTION_RE.match(name)
                    if m:
                        sec_name = m.group(1)
                        sub_raw = m.group(2)
                        sub_dec = _SUBSECTION_ESCAPE_RE.sub(
                            r"\1", sub_raw,
                        )

                if self.nest_sections and "." in name:
                    current_section_path = name.split(".")
                else:
                    current_section_path = [name]
                current_section_raw = name

                if profile == "gitconfig":
                    # Case-folded match key: section/key are
                    # case-insensitive, subsection is
                    # case-sensitive (git semantics; path and raw
                    # keep the first-seen original text)
                    srec_key = ("gitconfig", sec_name.lower(), sub_dec)
                else:
                    srec_key = cc.path_str(current_section_path)
                current_sec_key = srec_key

                section_seen_count[srec_key] = (
                    section_seen_count.get(srec_key, 0) + 1
                )
                if srec_key not in section_records:
                    record = cc.make_container(
                        current_section_path, "dict",
                        line_start=i, line_end=i,
                        duplicate=False,
                    )
                    if profile == "gitconfig":
                        if sub_dec is not None:
                            record["subsection"] = sub_dec
                            if sub_raw != sub_dec:
                                record["raw_subsection"] = sub_raw
                        fold = sec_name.lower()
                        if fold != sec_name:
                            record["fold_section"] = fold
                    elif profile == "editorconfig" and any(
                        ch in name for ch in _GLOB_CHARS
                    ):
                        # Static rule layer: glob section pattern
                        # registration (effective-attribute
                        # computation needs target file paths,
                        # not done)
                        record["pattern"] = name
                    containers.append(record)
                    section_records[srec_key] = record
                elif section_seen_count[srec_key] > 1:
                    section_records[srec_key]["duplicate"] = True
                flush_block(list(current_section_path), "section")
                i += 1
                continue

            # key-value line
            kv_line = i
            key, val = self._split_kv(clean)
            key = key.strip()
            val = val.strip()
            if not key:
                i += 1
                continue

            # Inline comment (quote-aware; stripped at the
            # contract fact layer)
            val, inline_text = self._split_inline_comment(val)

            # Multi-line value aggregation (configparser
            # semantics: empty-value kv line + more-deeply
            # indented continuation lines; semantically fixes
            # shapes like coveragerc source = indented list —
            # fragmenting empty-value keys is a bug, not a
            # contract)
            value_lines = None
            if val == "":
                kv_indent = len(raw_line) - len(raw_line.lstrip(" \t"))
                conts, next_idx = self._multiline_continuation(
                    lines, i + 1, kv_indent,
                )
                if conts:
                    value_lines = conts
                    i = next_idx - 1

            if current_section_raw is None:
                # key before the first section — keep the fact
                # (path has no section segment)
                entry_path = [key]
                reasons.append("pre_section_keys_present")
            else:
                entry_path = list(current_section_path) + [key]

            flush_block(entry_path, "key")

            if profile == "gitconfig":
                map_key = (current_sec_key, key.lower())
            else:
                map_key = (current_section_raw, key)
            if map_key in entry_map:
                # duplicate key: the override chain keeps every
                # occurrence in order
                existing = entry_map[map_key]
                if existing["override_chain"] is None:
                    existing["override_chain"] = [
                        {"value": existing["raw_value"],
                         "line": existing["line_start"]},
                        {"value": val, "line": kv_line},
                    ]
                else:
                    existing["override_chain"].append(
                        {"value": val, "line": kv_line}
                    )
                existing["duplicate"] = True
                if profile == "gitconfig":
                    # multi-value semantics (additive: gitconfig
                    # duplicate keys are legal multi-values, not
                    # shadowing; override_chain coexists to
                    # disclose both semantics)
                    if "values" in existing:
                        existing["values"].append(val)
                    else:
                        existing["values"] = [
                            existing["raw_value"], val,
                        ]
                    existing["multi_value"] = True
                existing["raw_value"] = val
                existing["value"] = (
                    self._infer_type(val) if self.type_infer else val
                )
                existing["line_start"] = kv_line
                existing["line_end"] = kv_line
            else:
                if value_lines is not None:
                    entry_value = list(value_lines)
                else:
                    entry_value = (
                        self._infer_type(val) if self.type_infer else val
                    )
                entry = cc.make_entry(
                    entry_path, key, val, entry_value,
                    line_start=kv_line,
                    line_end=(i if value_lines is not None else kv_line),
                )
                if value_lines is not None:
                    # original lines kept (no lossy joining);
                    # candidate_type is only a weak declaration
                    # under dialect profiles
                    entry["value_lines"] = value_lines
                    if profile in ("coveragerc", "pylintrc"):
                        entry["candidate_type"] = "multiline"
                elif profile in ("coveragerc", "pylintrc"):
                    cand = self._candidate_type(val)
                    if cand:
                        entry["candidate_type"] = cand
                if profile == "gitconfig" and key.lower() != key:
                    # key case-folding disclosure (raw keeps the
                    # original text)
                    entry["fold_key"] = key.lower()
                if profile == "npmrc":
                    # host-scoped / scope registry semantic keys
                    # (the original path is already kept; this
                    # only adds semantic registration)
                    if key.startswith("//") and ":" in key:
                        rest = key[2:]
                        pos = rest.rfind(":")
                        target = rest[:pos].rstrip("/")
                        if target:
                            entry["registry_target"] = target
                    if key.startswith("@") and key.endswith(":registry"):
                        entry["npm_scope"] = key.split(":", 1)[0]
                if profile == "editorconfig" and key.lower() == "root":
                    # root declaration slot (registration only;
                    # config hierarchy is process context — no
                    # inference without evidence)
                    entry["profile_rule"] = "editorconfig_root"
                entry_map[map_key] = entry
                entries.append(entry)

            if inline_text:
                comments.append(cc.make_comment(
                    "inline", inline_text, "", kv_line, kv_line,
                    bound_path=entry_path, bound_kind="key",
                ))

            i += 1

        # Wrap up: trailing comment block → orphan
        flush_block(None, None)

        # description backfill
        for record in entries + containers:
            d = bound_desc.get(record["path_str"])
            if d:
                record["description"] = d[0]
                record["description_lines"] = d[1]

        # DEFAULT inheritance note (fact: the contract does not
        # expand it, legacy structure does)
        if any(s == "DEFAULT" for s in section_seen_count):
            reasons.append("default_section_inheritance_not_expanded")

        # npmrc sensitive disclosure (key vocabulary + value
        # pattern scanning, never store value — plaintext never
        # enters any contract key)
        if profile == "npmrc":
            sensitive_count = 0
            for entry in entries:
                types = []
                if cc.is_sensitive_key(entry.get("key") or ""):
                    types.append("sensitive_key")
                raw = entry.get("raw_value")
                if isinstance(raw, str) and raw:
                    for t in sensitive_signals.scan_sensitive_types(raw):
                        if t not in types:
                            types.append(t)
                if types:
                    entry["sensitive_candidates"] = [
                        {"type": t, "masked": _SENSITIVE_MASK}
                        for t in types
                    ]
                    sensitive_count += 1
            if sensitive_count:
                reasons.append(f"sensitive_entry_values:{sensitive_count}")

        cc.fill_child_counts(entries, containers)
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")

        # npmrc env-var reference disclosure (with
        # interpolation=False the raw value is kept and
        # references are already flagged dynamic; here the reason
        # is registered with the per-entry reference count)
        if profile == "npmrc":
            env_count = 0
            for entry in entries:
                for ref in entry.get("references") or []:
                    base = ref.get("target") or ""
                    for sep in (":-", "?"):
                        if sep in base:
                            base = base.split(sep, 1)[0]
                    if _NPMRC_ENV_VAR_RE.match(base):
                        env_count += 1
            if env_count:
                reasons.append(f"env_var_references:{env_count}")

        return contract

    @staticmethod
    def _detect_dialect(filename):
        """Map a dialect filename to a contract profile
        (None = generic INI)."""
        if not filename:
            return None
        return _DIALECT_FILES.get(os.path.basename(str(filename)).lower())

    @staticmethod
    def _sniff_cfg_mismatch(text):
        """Offline .cfg feature sniffing (detect, do not reroute;
        parse semantics are frozen).

        Feature hit ratio (heuristic, not probabilistic —
        mirrored in the contract header reason
        cfg_sniff_heuristic:feature_ratio): the ratio of
        structured-value signals (quoted values / arrays /
        inline table literals / quoted keys) to bare kv signals.
        Pure INI (zero structured signals) → None, zero keys
        zero drift; on hits, disclose ini_candidate +
        alternatives. Routing changes are a separate decision.
        """
        structured = 0
        bare_kv = 0
        plain = 0
        for raw in text.splitlines():
            s = raw.strip()
            if not s or s.startswith((";", "#")):
                continue
            if s.startswith("[") and s.endswith("]"):
                continue
            if "=" in s:
                k, v = s.split("=", 1)
            elif ":" in s:
                k, v = s.split(":", 1)
            else:
                plain += 1
                continue
            k = k.strip()
            v = v.strip()
            if not k:
                plain += 1
                continue
            if k[:1] in ('"', "'") or v[:1] in ('"', "'", "[", "{"):
                structured += 1
            else:
                bare_kv += 1
        if structured == 0:
            return None
        alternatives = ["toml"]
        if plain:
            alternatives.append("plain_text")
        total = structured + bare_kv
        confidence = round(structured / total, 2) if total else 0.0
        return {
            "detected_format": "ini_candidate",
            "confidence": confidence,
            "alternative_formats": alternatives,
        }

    def _multiline_continuation(self, lines, start, kv_indent):
        """Collect more-deeply indented continuation lines after
        an empty-value kv line (configparser semantics).

        Stops on: blank line / indentation not deeper than the
        kv line / comment line / section-shaped line.
        Returns (stripped continuation list, index of the first
        unconsumed line).
        """
        conts = []
        j = start
        n = len(lines)
        while j < n:
            nxt = lines[j]
            if not nxt.strip():
                break
            indent = len(nxt) - len(nxt.lstrip(" \t"))
            if indent <= kv_indent:
                break
            s = nxt.strip()
            if self._is_comment_line(s) or s.startswith("[") \
                    or s.endswith("]"):
                break
            conts.append(s)
            j += 1
        return conts, j

    @staticmethod
    def _candidate_type(raw):
        """Candidate-type weak declaration (disclose only, do
        not execute: no CSV splitting, no regex compilation).

        coveragerc/pylintrc profile vocabulary: csv (comma
        list) / regex (^ \\ (? prefixes) / path_pattern (glob
        chars).
        """
        if not raw:
            return None
        if "," in raw:
            return "csv"
        if raw[:1] in ("^", "\\") or raw.startswith("(?"):
            return "regex"
        if "*" in raw:
            return "path_pattern"
        return None

    def _is_comment_line(self, clean_line):
        """Whole-line comment detection (same semantics as the
        recovery parser, uses self.comment_markers)."""
        return any(
            clean_line.startswith(m) for m in self.comment_markers
        )

    @staticmethod
    def _split_kv(clean_line):
        """Same separator precedence as the recovery parser:
        = > : > whitespace."""
        if "=" in clean_line:
            key, val = clean_line.split("=", 1)
        elif ":" in clean_line:
            key, val = clean_line.split(":", 1)
        elif " " in clean_line:
            key, val = clean_line.split(None, 1)
        else:
            key, val = clean_line, ""
        return key, val

    def _split_inline_comment(self, value):
        """Strip an inline comment (marker preceded by
        whitespace, quote-aware)."""
        quote = None
        for idx, ch in enumerate(value):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch in self.comment_markers and (
                idx == 0 or value[idx - 1] in (" ", "\t")
            ):
                return (
                    value[:idx].rstrip(),
                    value[idx:].lstrip(";#").strip(),
                )
        return value, None

    # ==================================
    # INI parsing core
    # ==================================

    def _parse_ini(self, text):
        """Parse ini text into list of {section, items} dicts."""
        ini_data = []

        try:
            ini_data = self._parser_recovery_custom_text(text)
            if self.interpolation:
                ini_data = self._apply_advanced_interpolation(ini_data)
            if self.type_infer:
                ini_data = self._apply_type_infer(ini_data)
            if self.nest_sections:
                ini_data = self._nest_sections_for_list(ini_data)

        except Exception as e:
            ini_data = [{"section": "_error", "items": {"error": f"读取失败：{e}"}}]

        return ini_data

    # ==================================
    # recovery mode parser (text-based)
    # ==================================

    def _parser_recovery_custom_text(self, text):
        """
        Recovery mode: custom parser, maximize semantic recovery.
        - preserves section order (outer list)
        - preserves key order within sections
        - duplicate keys stored as list
        - same section merged into one dict
        - tolerates missing brackets, no section, malformed lines
        """
        sections = []
        section_map = {}
        has_section = False

        lines = text.splitlines()
        idx = 0
        n_lines = len(lines)
        while idx < n_lines:
            raw_line = lines[idx]
            clean_line = raw_line.strip()

            if not clean_line:
                idx += 1
                continue

            if any(clean_line.startswith(m) for m in self.comment_markers):
                idx += 1
                continue

            # section line
            if clean_line.startswith("[") or clean_line.endswith("]"):
                has_section = True

                if clean_line.startswith("[") and clean_line.endswith("]"):
                    section_name = clean_line[1:-1].strip()
                elif clean_line.startswith("["):
                    section_name = clean_line[1:].strip()
                elif clean_line.endswith("]"):
                    section_name = clean_line[:-1].strip()
                else:
                    section_name = ""

                if section_name in section_map:
                    current_section = section_map[section_name]
                else:
                    current_section = {"section": section_name, "items": {}}
                    sections.append(current_section)
                    section_map[section_name] = current_section
                idx += 1
                continue

            # key-value line
            if "=" in clean_line:
                key, val = clean_line.split("=", 1)
            elif ":" in clean_line:
                key, val = clean_line.split(":", 1)
            elif " " in clean_line:
                key, val = clean_line.split(None, 1)
            else:
                key, val = clean_line, ""

            key = key.strip()
            val = val.strip()

            if not has_section and self.auto_add_section:
                section_name = "auto_section"
                if section_name in section_map:
                    current_section = section_map[section_name]
                else:
                    current_section = {"section": "auto_section", "items": {}}
                    sections.append(current_section)
                    section_map[section_name] = current_section
                has_section = True
            elif not has_section:
                idx += 1
                continue

            items = current_section["items"]

            # Multi-line value aggregation (configparser
            # semantics: empty value + more-deeply indented
            # continuation lines; structured-value drift is
            # test-anchored — fragmenting empty-value keys is a
            # semantic bug, not a contract; the contract layer
            # uses the same criteria)
            if val == "":
                kv_indent = len(raw_line) - len(raw_line.lstrip(" \t"))
                conts, next_idx = self._multiline_continuation(
                    lines, idx + 1, kv_indent,
                )
                if conts:
                    items[key] = conts
                    idx = next_idx
                    continue

            if key in items:
                if isinstance(items[key], list):
                    items[key].append(val)
                else:
                    items[key] = [items[key], val]
            else:
                items[key] = val
            idx += 1

        if not has_section and self.auto_add_section and not sections:
            sections.insert(0, {"section": "auto_section", "items": {}})

        # merge DEFAULT into each section
        if "DEFAULT" in section_map:
            default_items = section_map["DEFAULT"]["items"]
            for sec in sections:
                if sec["section"] == "DEFAULT":
                    continue
                for key, val in default_items.items():
                    if key not in sec["items"]:
                        sec["items"][key] = val

        return sections

    # ==================================
    # type inference
    # ==================================

    def _infer_type(self, value):
        """Type inference for recovery mode."""
        if not isinstance(value, str):
            return value

        if value.lower() in ("null", "none", "~"):
            return None

        # bool/int/float heuristic shared with the
        # env/properties parsers
        return cc.infer_scalar(value)

    def _apply_type_infer(self, ini_data):
        """Apply type inference to all values."""
        for s in ini_data:
            if isinstance(s, dict) and "items" in s:
                for key, val in s["items"].items():
                    if isinstance(val, list):
                        s["items"][key] = [self._infer_type(v) for v in val]
                    else:
                        s["items"][key] = self._infer_type(val)
        return ini_data

    # ==================================
    # interpolation
    # ==================================

    def _replace_vars(self, get_var_func, value, max_depth=25, _resolving=None,
                       io_hits=None):
        """Variable interpolation replacement.

        Interpolation I/O safety: file:/json:/yaml: prefixes are
        recognized but never executed (placeholder kept + io_hits
        reason disclosure) — no file-read surface.

        Semantic rules (the interpolation channel has zero
        external callers, so these fixes have no external drift
        surface):
            - Unknown references are kept: env/cross-section
              references with no backing value keep the
              placeholder verbatim (previously silently deleted,
              losing data); an explicit empty default `${v:}`
              still resolves to empty (explicit file semantics).
            - Type prefixes/conditionals: int:/float:/bool: and
              the VAR?a:b branch run before the env-default
              branch (previously intercepted as dead code by the
              `":" in var` check, so the documented feature
              never fired); truthiness uses the family-level
              infer_scalar heuristic ('0'/'off' are false,
              correcting the naive bool(str) which is always
              true).
            - Replacement shape convergence: only the actually
              matched form is replaced (the three forms no
              longer contaminate each other); bare $var gets a
              word-boundary guard ($x is a prefix substring of
              $xy; unbounded replace corrupts longer variable
              names).
            - The unreachable trailing else is deleted (the
              dot/no-dot branches are exhaustive).
            - get_var contract: a miss returns None (distinct
              from the empty value "").
        """
        value = re.sub(r"\$\{([^{}]*)$", r"${\1}", value)

        if _resolving is None:
            _resolving = set()
        if not isinstance(value, str):
            return value

        depth = 0
        pattern = re.compile(
            r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"
            r"|\%\(([^)]+)\)s"
            r"|\$([A-Za-z_][A-Za-z0-9_]*)"
        )
        # bare $var boundary-replacement pattern cache
        bare_re_cache = {}

        while depth <= max_depth:
            matches = pattern.findall(value)
            if not matches:
                break
            changed = False
            has_cycle = False

            for full_match in matches:
                var = full_match[0] or full_match[1] or full_match[2]
                replacement = ""
                unresolved = False

                if var in _resolving:
                    has_cycle = True
                    continue

                _resolving.add(var)

                # file:/json:/yaml: recognized, not executed
                # (intercepted up front — a malicious
                # ${file:/etc/passwd} gains no arbitrary
                # file-read surface)
                if var.startswith(("file:", "json:", "yaml:")):
                    if io_hits is not None:
                        io_hits.append(var.split(":", 1)[0])
                    _resolving.discard(var)
                    continue

                # type prefixes (hoisted above the env-default
                # branch; on miss → keep, same as unknown
                # references)
                if var.startswith(("int:", "float:", "bool:")):
                    kind, name = var.split(":", 1)
                    v = get_var_func(name)
                    if v is None:
                        unresolved = True
                    else:
                        try:
                            if kind == "bool":
                                replacement = str(
                                    bool(cc.infer_scalar(v))
                                )
                            elif kind == "int":
                                replacement = str(int(v))
                            else:
                                replacement = str(float(v))
                        except Exception:
                            replacement = ""

                # conditional expression VAR?true:false (hoisted;
                # truthiness uses the family scalar heuristic)
                elif "?" in var and ":" in var:
                    name, rest = var.split("?", 1)
                    true_val, false_val = rest.split(":", 1)
                    v = get_var_func(name)
                    replacement = true_val if (
                        v is not None and bool(cc.infer_scalar(v))
                    ) else false_val

                # env var + default value
                elif ":" in var and ("." not in var or var.index(":") < var.index(".")):
                    name, default = var.split(":", 1)
                    if default.startswith("-"):
                        default = default[1:]

                    # Same zero-IO discipline: environment
                    # variables are identified but never read
                    # (never probe the host os.environ, to keep
                    # host environment values out of the
                    # evidence output; the old code probed
                    # os.getenv(var) with the whole "VAR:default"
                    # string as the name, which was already
                    # wrong semantically). In-document cross
                    # references and defaults keep their
                    # original semantics.
                    cross_ref = get_var_func(f"{name}.{default}")
                    if cross_ref:
                        replacement = cross_ref
                    else:
                        replacement = default if default else ""

                elif "." not in var:
                    # Same zero-IO discipline: os.environ is
                    # never read — only in-document references
                    # are resolved; misses keep the original text
                    # per the unknown-reference rule, no false
                    # reports, no leaks.
                    v = get_var_func(var)
                    if v is None:
                        # unknown reference: keep
                        unresolved = True
                    else:
                        replacement = v

                # cross-section reference ${section.key}
                elif "." in var:
                    if ":" in var:
                        ref, default = var.split(":", 1)
                        sec, key = ref.split(".", 1)
                        val = get_var_func(f"{sec}.{key}")
                        if not val or (
                            isinstance(val, str)
                            and val.startswith("${")
                        ):
                            replacement = default
                        else:
                            replacement = val
                    else:
                        sec, key = var.split(".", 1)
                        val = get_var_func(f"{sec}.{key}")
                        if val is None:
                            # unknown cross-section reference: keep
                            unresolved = True
                        else:
                            replacement = val

                if unresolved:
                    # unknown reference: keep the placeholder
                    # verbatim (no delete, no replace)
                    _resolving.discard(var)
                    continue

                # replace only the actually matched form; bare
                # $var word-boundary guard
                rstr = str(replacement)
                if full_match[0]:
                    new_value = value.replace(f"${{{var}}}", rstr)
                elif full_match[1]:
                    new_value = value.replace(f"%({var})s", rstr)
                else:
                    bare_re = bare_re_cache.get(var)
                    if bare_re is None:
                        bare_re = re.compile(
                            r"\$" + re.escape(var)
                            + r"(?![A-Za-z0-9_])"
                        )
                        bare_re_cache[var] = bare_re
                    new_value = bare_re.sub(
                        lambda _m, _r=rstr: _r, value,
                    )
                if new_value != value:
                    value = new_value
                    changed = True

                _resolving.discard(var)

            if not changed or has_cycle:
                break
            depth += 1

        # clean up empty placeholders
        value = re.sub(r"\$\{\}", "", value)
        value = re.sub(r"\%\(\)s", "", value)
        value = re.sub(r"\$(?!\{)(?=[^A-Za-z_]|$)", "", value)
        value = re.sub(r"\$\{[^{}]*$", "", value)
        return value

    def _apply_advanced_interpolation(self, ini_data):
        """Apply interpolation to all values in recovery mode."""
        # I/O prefix hit channel (parse → _build_contract
        # consumes it as a reason)
        io_hits = getattr(self, "_interpolation_io_hits", None)
        if io_hits is None:
            io_hits = []
            self._interpolation_io_hits = io_hits
        raw_snapshot = {}
        for s in ini_data:
            if isinstance(s, dict) and "items" in s:
                raw_snapshot[s["section"]] = dict(s["items"])

        def get_var(name):
            """Fetch the interpolation variable by name from
            the raw snapshot (supports section.key
            qualification).

            A miss returns None (distinct from the empty value
            "" — the basis for unknown-reference detection).
            """
            if "." in name:
                sec, key = name.split(".", 1)
                if sec in raw_snapshot and key in raw_snapshot[sec]:
                    return str(raw_snapshot[sec][key])

            for _, sec_dict in raw_snapshot.items():
                if name in sec_dict:
                    return str(sec_dict[name])

            if "DEFAULT" in raw_snapshot and name in raw_snapshot["DEFAULT"]:
                return str(raw_snapshot["DEFAULT"][name])
            return None

        if isinstance(ini_data, dict):
            for _, sec_dict in ini_data.items():
                for key, val in sec_dict.items():
                    sec_dict[key] = self._replace_vars(
                        get_var, val, io_hits=io_hits,
                    )
        elif isinstance(ini_data, list):
            for s in ini_data:
                if isinstance(s, dict) and "items" in s:
                    for key, val in s["items"].items():
                        resolving = {f"{s['section']}.{key}"}
                        s["items"][key] = self._replace_vars(
                            get_var,
                            str(val) if not isinstance(val, str) else val,
                            _resolving=resolving,
                            io_hits=io_hits,
                        )
        return ini_data

    # ==================================
    # nesting helpers
    # ==================================

    def _list_to_dict(self, section_list):
        """Convert list of sections to dict."""
        result = {}
        for sec in section_list:
            if isinstance(sec, dict) and "section" in sec and "items" in sec:
                result[sec["section"]] = sec["items"]
        return result

    def _nest_sections_for_list(self, sections_list):
        """Nest dotted section names (list structure, recovery mode)."""
        nested_sections = []
        for sec in sections_list:
            if not isinstance(sec, dict) or "section" not in sec:
                nested_sections.append(sec)
                continue

            parts = sec["section"].split(".")

            if len(parts) == 1:
                nested_sections.append(sec)
            else:
                current = {}
                tmp = current
                for part in parts[:-1]:
                    tmp = tmp.setdefault(part, {})
                tmp[parts[-1]] = {"items": sec["items"]}
                nested_sections.append({
                    "section": parts[0],
                    "items": current[parts[0]],
                })
        return nested_sections

    # ========================================
    # Multi-file cascading merge (thin wrapper over the
    # shared cc.merge_contracts layer)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Multi-file cascading merge: later files shadow
        earlier ones (cc.merge_contracts shared layer).

        Each file goes through the existing single-file
        contract channel (with all disclosure semantics). The
        merged header's ``duplicate_key_semantics`` is uniformly
        ``last_file_wins`` (cross-file shadowing at the merge
        layer; within-file override_chain semantics stay at the
        entries layer — the two layers are orthogonal). Returns
        the merged contract dict (not a Document — line-number
        semantics are attributed via source_file). The base
        parse_many semantics are frozen.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            contracts.append(meta.get("config_entries") or cc.build_contract(
                "ini", cc.DUP_OVERRIDE_CHAIN))
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
        return cc.merge_contracts("ini", contracts, sources)
