# ============================================================
# Structured Config Domain
# Generic `.conf` Content-Gated Parser
# ============================================================
"""Generic ``.conf`` content-gated parser.

Positioning: ``.conf`` is an extension family, not a single
syntax family — content detection takes precedence over
extension semantics. This parser is the content-gated entry:

1. Read the raw text first (only this file's IO, following the
   encoding-diagnosis convention);
2. The content detector decides detected_syntax / profile /
   confidence / alternative_profiles / resolution_status;
3. Recognized as an existing family (JSON/YAML/TOML/INI/XML/HCL
   with machine-verifiable evidence) → **hand off to the
   corresponding parser for reuse** (no second copy of the
   syntax implementation);
4. Otherwise → the generic unified recovery layer over five
   syntax classes: generic_directive / key_value / section /
   brace_block / shell_like (identify-only, never executed);
5. include registration with zero IO
   (resolution_status="not_resolved");
6. scope_path preservation + all duplicate occurrences kept
   (no semantic adjudication);
7. config_entry_v1.1 contract + conf_profile metadata
   disclosure.

Red lines: zero execution; additive zero drift; zero IO (no
include expansion, no external file reads, no directory
scans); honest detection (uncertain → profile_uncertain,
unknown directives are not syntax errors); tool-profile
semantics stay out (nginx/apache/sshd… belong to the
tool-profile pass).
"""
from __future__ import annotations

import bisect
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

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

_FORMAT = "conf"
_MAX_DEPTH = 64
_EXECUTE_SEMANTICS = "not-executed-disclosed"

# ------------------------------------------------------------
# Content detection: filename hints (evidence type "filename")
# ------------------------------------------------------------
_FILENAME_PROFILE_HINTS = {
    "nginx.conf": ("nginx", 0.9),
    "httpd.conf": ("apache", 0.9),
    "apache2.conf": ("apache", 0.9),
    "redis.conf": ("redis", 0.85),
    "sysctl.conf": ("sysctl", 0.9),
    "resolv.conf": ("resolv", 0.95),
    "named.conf": ("bind", 0.9),
    "openvpn.conf": ("openvpn", 0.85),
}

# Syntax feature hints (evidence type "syntax"; candidates
# only, no tool semantics implemented)
_SYNTAX_PROFILE_HINTS = (
    (re.compile(r"^\s*nameserver\s+\S", re.M), "resolv", 0.85),
    (re.compile(
        r"<\s*(?:VirtualHost|Directory|Location|IfModule|Files)\b",
        re.I,
    ), "apache", 0.8),
    (re.compile(r"\bserver\s*\{"), "nginx", 0.7),
    (re.compile(r"^\s*(?:location|server_name|proxy_pass)\s+\S", re.M),
     "nginx", 0.6),
    (re.compile(r"^\s*Match\s+\w+", re.M), "sshd", 0.75),
    (re.compile(r'\bzone\s+"[^"]+"\s*\{'), "bind", 0.75),
    (re.compile(r"^\s*(?:proto|dev|ca|cert|dh|remote)\s+\S", re.M),
     "openvpn", 0.6),
    (re.compile(
        r"^\s*(?:net|fs|kernel|vm)\.[\w.]+\s*[=:]", re.M,
    ), "sysctl", 0.7),
)

# ------------------------------------------------------------
# include registration (detect, never read);
# blocked-execution directive lists
# ------------------------------------------------------------
_INCLUDE_DIRECTIVES = frozenset((
    "include", "include_optional", "includeoptional",
    "tryinclude", "try_include", "source", "config_include",
))
_PLUGIN_DIRECTIVES = frozenset((
    "load_module", "loadmodule", "load_plugins", "loadmodule",
    "plugin", "plugins", "module",
))

# shell_like detection: leading token hits a shell keyword
# (identify-only, never executed)
_SHELL_KEYWORDS = frozenset((
    "if", "then", "else", "elif", "fi", "for", "while", "until",
    "do", "done", "case", "esac", "function", "export", "unset",
    "set", "return", "source", "alias", "local",
))

# Candidate types (weak declaration, disclosed but not
# coerced: different services interpret yes/no, on/off,
# numbers and time units differently)
_INT_RE = re.compile(r"^[+-]?\d+$")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?(?:ms|s|m|h|d|w)$")
_BOOLEAN_WORDS = frozenset(("yes", "no", "on", "off", "true", "false"))
_URL_RE = re.compile(r"^(?:https?|ftp)://", re.I)
_GLOB_CHARS = ("*", "?", "[")
_TRAVERSAL_SEG = re.compile(r"(?:^|/)\.\.(?:/|$)")
_CMD_SUB_RE = re.compile(r"\$\(|`")

# Directive name shape (rejects leading digits/punctuation —
# INVALID_DIRECTIVE criterion)
_BAD_NAME_RE = re.compile(r"^[^A-Za-z_@.\-]")


class _Diag:
    """Honest failure counter (reason code → count; zero hits,
    zero keys)."""

    def __init__(self):
        self.counts: dict = {}
        self.extra: list = []

    def hit(self, code: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1

    def reasons(self) -> list:
        out = [f"{code}:{n}" for code, n in self.counts.items()]
        out.extend(self.extra)
        return out


class _ConfScanner:
    """Character-level scanner (pos → 1-based line number via
    line_starts bisect)."""

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.line_starts = [0]
        for idx, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(idx + 1)

    def line_of(self, pos: int) -> int:
        """1-based line number."""
        return bisect.bisect_right(self.line_starts, pos)

    def peek(self, offset: int = 0) -> str:
        p = self.pos + offset
        return self.text[p] if p < self.n else ""

    def at_eof(self) -> bool:
        return self.pos >= self.n

    def starts_with(self, s: str) -> bool:
        return self.text.startswith(s, self.pos)

    def skip_ws(self) -> None:
        """Skip all whitespace (including newlines)."""
        while self.pos < self.n and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def at_comment(self) -> bool:
        ch = self.peek()
        if ch == "#":
            return True
        if ch == "/" and self.peek(1) in ("*", "/"):
            return True
        if ch == "<" and self.starts_with("<!--"):
            return True
        return False

    def consume_comment(self):
        """Consume one comment → dict(kind/text/raw/line_start/
        line_end).

        kind: "line" (# or //) | "block" (/* */ or <!-- -->).
        An unclosed block comment is swallowed to EOF (kept
        verbatim; no diagnostic code is recorded).
        """
        start = self.pos
        ls = self.line_of(start)
        if self.starts_with("<!--"):
            end = self.text.find("-->", self.pos + 4)
            end = self.n if end == -1 else end + 3
            raw, inner, kind = (
                self.text[start:end],
                self.text[start + 4:end - 3],
                "block",
            )
        elif self.starts_with("/*"):
            end = self.text.find("*/", self.pos + 2)
            end = self.n if end == -1 else end + 2
            raw, inner, kind = (
                self.text[start:end],
                self.text[start + 2:end - 2],
                "block",
            )
        else:  # # or //
            nl = self.text.find("\n", self.pos)
            end = self.n if nl == -1 else nl
            raw = self.text[start:end]
            inner = raw.lstrip("#/").strip()
            kind = "line"
        self.pos = end
        return {
            "kind": kind,
            "text": inner.strip(),
            "raw": raw,
            "line_start": ls,
            "line_end": self.line_of(max(end - 1, start)),
        }

    def consume_quoted(self, quote: str):
        """Consume a quoted token → (raw including quotes,
        closed). pos must be at the opening quote.

        Quotes do not span lines (recovery semantics: an
        unclosed quote ends at the line boundary and later
        statements recover normally; UNCLOSED_QUOTE is
        recorded by the caller)."""
        start = self.pos
        self.pos += 1
        while self.pos < self.n:
            ch = self.text[self.pos]
            if quote == '"' and ch == "\\":
                self.pos += 2
                continue
            if ch == quote:
                self.pos += 1
                return self.text[start:self.pos], True
            if ch == "\n":
                # unclosed at end of line — the statement
                # recovers at the line boundary
                return self.text[start:self.pos], False
            self.pos += 1
        return self.text[start:self.pos], False


# ============================================================
# Content detector (pure functions, zero IO)
# ============================================================

def _detect_existing_family(text: str, file_name: str):
    """Existing-family handoff decision (hand off only on
    machine-verifiable evidence — honest degradation).

    Returns (family, confidence, alternatives) or None.

    kv-shaped families (ini/toml) overlap naturally with the
    generic ``key=value`` syntax (sysctl ``net.x.y = 1`` is
    also a valid TOML dotted key) — no handoff when the
    filename already carries a tool-profile hint (filename
    evidence wins); a plain dotted ``=`` alone is not TOML
    handoff evidence (sysctl shape), real TOML markers such
    as ``[[section]]`` or array values are required.
    """
    s = text.lstrip()
    if not s:
        return None
    if s[:1] in "{[":
        try:
            json.loads(s)
            return "json", 0.98, []
        except ValueError:
            pass  # invalid JSON → no guessing, falls to the
                  # generic recovery layer
    # XML handoff only on machine-verifiable evidence
    # (prolog/DOCTYPE); Apache angle containers
    # (<VirtualHost …>) overlap naturally with XML tags, so
    # without a prolog XML is not guessed (honest degradation;
    # the generic layer's angle recovery handles it)
    if s[:5].lower() == "<?xml" or s[:9].lower() == "<!doctype":
        return "xml", 0.95, []
    if text.startswith("---"):
        return "yaml", 0.85, []

    lines = [ln.strip() for ln in text.splitlines()]
    content = [ln for ln in lines if ln and not ln.startswith(("#", ";"))]
    if not content:
        return None

    def is_section(ln):
        return re.match(r"^\[+[^\[\]\n]+\]+$", ln) is not None

    def is_kv(ln):
        return re.match(r"^[A-Za-z0-9_.\-]+\s*[=:]\s*\S", ln) is not None

    has_brace = any("{" in ln for ln in content)
    semicolon_lines = [ln for ln in content if ln.endswith(";")]

    # kv-shaped families: if the filename already carries a
    # tool-profile hint → no handoff (profile candidates and
    # generic key_value recovery take precedence)
    if file_name.lower() not in _FILENAME_PROFILE_HINTS and (
        all(is_section(ln) or is_kv(ln) for ln in content)
        and not has_brace
        and not semicolon_lines
    ):
        toml_only = (
            any(re.match(r"^\s*\[\[.+\]\]", ln) for ln in content)
            or any(
                re.match(r"^\s*\w[\w.\-]*\s*=\s*\[", ln)
                for ln in content
            )
        )
        if toml_only:
            return "toml", 0.8, ["ini"]
        if any(is_section(ln) for ln in content):
            return "ini", 0.8, ["toml"]
        # pure key-value without sections (generic shapes
        # like sysctl) → stay in the generic layer
        return None

    # HCL: brace blocks + `=` attributes, no `;` terminator,
    # no [section] (nginx directives end with `;` → no hit,
    # stays in the generic layer)
    if (
        has_brace
        and not semicolon_lines
        and not any(is_section(ln) for ln in content)
    ):
        quoted_label = re.search(r'\b\w+\s+"[^"]+"\s*\{', text)
        eq_kv = any(
            re.match(r"^\s*[A-Za-z_][\w\-]*\s*=\s*\S", ln)
            for ln in content
        )
        if quoted_label or eq_kv:
            return "hcl", 0.7, []
    return None


def detect_conf_profile(text: str, file_name: str) -> dict:
    """Content detector → detection result dict (evidence-only)."""
    family = _detect_existing_family(text, file_name)
    if family:
        fam, conf, alts = family
        return {
            "detected_syntax": fam,
            "profile": f"{fam}_family",
            "confidence": conf,
            "alternative_profiles": [
                {"profile": a, "evidence": ["native_conf"]}
                for a in alts
            ],
            "resolution_status": "handed_off",
            "evidence_type": "native_conf",
        }

    # Tool-profile candidates (listed only, no tool
    # semantics implemented)
    merged: dict = {}
    hint = _FILENAME_PROFILE_HINTS.get(file_name.lower())
    if hint:
        merged[hint[0]] = (hint[1], ["filename"])
    for rx, name, conf in _SYNTAX_PROFILE_HINTS:
        if rx.search(text):
            if name in merged:
                conf = max(conf, merged[name][0])
                ev = merged[name][1] + ["syntax"]
            else:
                ev = ["syntax"]
            merged[name] = (conf, ev)

    # Generic syntax-shape evidence (decides evidence_type
    # and the resolved baseline)
    shape = bool(re.search(
        r"(^\s*\[[^\]\n]+\]\s*$"
        r"|^\s*[A-Za-z_][\w.\-]*\s*[=:]\s*\S"
        r"|\{\s*$"
        r"|^\s*\w[\w.\-]*\s+\S+;\s*$)",
        text, re.M,
    ))

    if merged:
        ranked = sorted(
            merged.items(), key=lambda kv: kv[1][0], reverse=True,
        )
        top_name, (top_conf, top_ev) = ranked[0]
        alternatives = [
            {"profile": n, "confidence": c, "evidence": ev}
            for n, (c, ev) in ranked[1:]
        ]
        if top_conf >= 0.7:
            return {
                "detected_syntax": "generic_conf",
                "profile": top_name,
                "confidence": top_conf,
                "alternative_profiles": alternatives,
                "resolution_status": "resolved",
                "evidence_type": (
                    "native_conf"
                    if "filename" in top_ev and "syntax" in top_ev
                    else "inferred" if "filename" in top_ev
                    else "generic_directive_recovered"
                ),
            }
        # weak candidates do not settle a profile → honest
        # degradation
        return {
            "detected_syntax": "generic_conf",
            "profile": "generic_conf_candidate",
            "confidence": top_conf,
            "alternative_profiles": [
                {"profile": top_name, "confidence": top_conf,
                 "evidence": top_ev}
            ] + alternatives,
            "resolution_status": "profile_uncertain",
            "evidence_type": (
                "generic_directive_recovered" if shape else "inferred"
            ),
        }

    # no tool hints at all → generic candidate +
    # profile_uncertain
    return {
        "detected_syntax": "generic_conf",
        "profile": "generic_conf_candidate",
        "confidence": 0.5,
        "alternative_profiles": [],
        "resolution_status": "profile_uncertain",
        "evidence_type": (
            "generic_directive_recovered" if shape else "inferred"
        ),
    }


# ============================================================
# ConfParser
# ============================================================

class ConfParser(BaseParser):
    """Generic ``.conf`` content-gated parser (recovery
    semantics).

    Unified recovery over the five generic syntax classes:
        - generic_directive — ``name args… ;`` (or line-terminated, tolerated)
        - key_value         — ``key = value`` / ``key: value``
        - section           — ``[name]`` (INI style)
        - brace_block       — ``name [labels] { … }`` (nesting + unclosed recovery)
        - shell_like        — shell keyword lines / command substitution (identify-only)

    Five comment kinds: leading (bound to the next statement) /
    inline (bound to the current statement) / block (# // /*
    */ <!-- -->) / trailing / orphan; ``#`` and ``;`` inside
    quotes do not truncate; ``\\`` and quoted continuation
    (physical line vs logical statement). ``;`` acts as a
    line comment only at the start of a statement (smb/grub
    convention); elsewhere it is an nginx-style terminator —
    the dialect criteria live in the contract's dialect key.
    """

    def __init__(self, encoding: str = "utf-8-sig"):
        """Init: encoding only serves as the decoding fallback
        (INVALID_ENCODING code)."""
        self.encoding = encoding

    # ==================================
    # BaseParser interface
    # ==================================

    def parse(self, file_path) -> Document:
        """Entry point: content detection → handoff or the
        generic layer, producing a Document + contract."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path, _FORMAT, self.encoding, True,
            )
            text = ctx.text
            metadata = ctx.metadata
            file_name = Path(file_path).name

            # Content detection takes precedence over
            # extension semantics
            detection = detect_conf_profile(text, file_name)

            # recognized as an existing family → hand off
            # for reuse (no second implementation)
            if detection["resolution_status"] == "handed_off":
                doc = self._delegate_parse(
                    detection["detected_syntax"], file_path,
                )
                self._attach_handoff_profile(doc, detection)
                return doc

            # --- generic five-class syntax recovery layer ---
            root, diag = self._parse_statements(text)
            if metadata.get("encoding_fallback"):
                diag.hit("INVALID_ENCODING")
            if detection["resolution_status"] == "profile_uncertain":
                diag.hit("UNKNOWN_CONF_PROFILE")

            metadata["parse_success"] = not any(
                k.startswith(("INVALID", "UNCLOSED", "TRUNCATED"))
                for k in diag.counts
            )
            metadata["top_level_keys"] = [
                c["name"] for c in root["children"] if c.get("name")
            ]
            metadata["structured_data"] = {
                "statements": self._flat_statements(root),
                "structure": self._structure(root),
            }

            contract, loss_codes = self._build_contract(
                root, diag, detection, text,
            )
            metadata["config_entries"] = contract

            # Object-level unresolved registry (family
            # convention; silent degradation under the guard)
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            # conf_profile disclosure (including
            # blocked-execution loss_codes)
            profile = dict(detection)
            if loss_codes:
                profile["loss_codes"] = loss_codes
            metadata["conf_profile"] = profile

            # three-state + six-state disclosure
            metadata["conf_status"] = self._status_of(
                diag, detection, contract, loss_codes,
            )

            # Tool-profile semantic pass (a separate module,
            # disclosure-only, zero evaluation zero IO;
            # guarded hookup — a failure degrades silently and
            # does not affect the contract chain). Build
            # criteria: resolution_status=resolved and the
            # profile is one of the 8 supported tools;
            # semantic-layer codes go to
            # conf_tool_profile["loss_codes"], the contract's
            # loss.reasons stays drift-free.
            try:
                from provendoc.core.parsers.config.conf_tool_profile import (
                    build_conf_tool_profile,
                )
                tool_profile = build_conf_tool_profile(
                    root, contract, detection, file_name, text,
                    file_path=str(file_path),
                )
                if tool_profile is not None:
                    metadata["conf_tool_profile"] = tool_profile
                    # file_role disclosed alongside (following
                    # the terraform precedent)
                    file_role = tool_profile.get("file_role") or {}
                    if file_role.get("file_role"):
                        profile["file_role"] = file_role["file_role"]
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("conf tool profile pass skipped: %s", exc)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text, metadata=metadata,
                source_text=ctx.source_text,
            )
        except Exception as e:
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                    "structured_data": {
                        "statements": [], "structure": {},
                    },
                },
            )

    # ==================================
    # Multi-file include graph + per-policy merge (thin
    # wrapper over a new channel)
    # ==================================

    def parse_many_merged(self, file_paths):
        """Multi-file merge: include graph + per-policy merge.

        Follows the plist/xml ``parse_many_merged`` thin-wrapper
        precedent: each file goes through the existing
        single-file contract channel — disk-reading
        responsibility belongs entirely to the caller's paths
        list (restricted-IO convention; the merge module itself
        does zero IO). Collects contracts + conf_profile +
        conf_tool_profile, then hands them to
        ``conf_include_merge.merge_conf_contracts`` for the
        include-graph build (in-set matching), graph-code
        adjudication, load-order preservation and per-policy
        merging (four strategies). The single-file ``parse``
        channel is fully frozen (a purely additive method; the
        graph codes only land in the merged contract's
        loss.reasons).

        Returns:
            The merged contract dict (not a Document — across
            files there is no single original text; source
            attribution is via entry/container source_file /
            source_file_index, load order via
            include_graph.resolved_file_order and
            directive_order / scope_order).

        Raises:
            ValueError: ≥2 different identified tool profiles
                appear (mixed-tool rejection — following the
                SDK mixed-family rejection precedent).
        """
        from provendoc.core.parsers.config.conf_include_merge import (
            merge_conf_contracts,
        )

        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        tool_profiles = []
        paths = []
        for i, (fp, doc) in enumerate(zip(file_paths, docs)):
            meta = doc.metadata
            contracts.append(
                meta.get("config_entries")
                or cc.build_contract("conf", cc.DUP_OVERRIDE_CHAIN),
            )
            sources.append({
                "index": i,
                "file_name": meta.get("file_name") or Path(fp).name,
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
            tool_profiles.append(meta.get("conf_tool_profile"))
            paths.append(str(fp))
        return merge_conf_contracts(
            contracts, sources, tool_profiles, paths,
        )

    # ==================================
    # Handoff (delegate to an existing family parser)
    # ==================================

    def _delegate_parse(self, family: str, file_path) -> Document:
        if family == "json":
            from provendoc.core.parsers.config.json_parser import (
                JsonParser,
            )
            return JsonParser().parse(file_path)
        if family == "xml":
            from provendoc.core.parsers.config.xml_parser import (
                XmlParser,
            )
            return XmlParser().parse(file_path)
        if family == "yaml":
            from provendoc.core.parsers.config.yaml_parser import (
                YamlParser,
            )
            return YamlParser().parse(file_path)
        if family == "ini":
            from provendoc.core.parsers.config.ini_parser import (
                IniParser,
            )
            return IniParser().parse(file_path)
        if family == "toml":
            from provendoc.core.parsers.config.toml_parser import (
                TomlParser,
            )
            return TomlParser().parse(file_path)
        if family == "hcl":
            from provendoc.core.parsers.config.hcl_parser import (
                HclParser,
            )
            return HclParser().parse(file_path)
        raise ValueError(f"unknown delegate family: {family!r}")

    @staticmethod
    def _attach_handoff_profile(doc: Document, detection: dict) -> None:
        """Attach additive detection disclosure after handoff
        (existing keys are never rewritten)."""
        try:
            md = doc.metadata
            if isinstance(md, dict) and "conf_profile" not in md:
                md["conf_profile"] = {
                    "detected_syntax": detection["detected_syntax"],
                    "profile": detection["profile"],
                    "confidence": detection["confidence"],
                    "alternative_profiles": detection[
                        "alternative_profiles"
                    ],
                    "resolution_status": "handed_off",
                    "evidence_type": "native_conf",
                    "delegated_to": (
                        f"{detection['detected_syntax']} parser"
                    ),
                }
        except Exception:  # pragma: no cover - defensive guard
            _log.debug("conf handoff profile attach skipped")

    # ==================================
    # Statement-level parsing (logical statements)
    # ==================================

    def _parse_statements(self, text: str):
        """Statement-tree parsing → (root, diag).

        Recovery semantics: unclosed blocks/quotes/
        continuations are recorded truthfully and their
        content preserved; mismatched ``}`` / angle closing
        tags are not guessed — disclosed as extra reasons.
        """
        sc = _ConfScanner(text)
        diag = _Diag()
        root: dict[str, Any] = {
            "kind": "root", "name": None, "children": [],
        }
        stack = [root]
        pending_leading: list[dict] = []

        while True:
            stmt, leading = self._next_statement(sc, diag)
            if stmt is None:
                # EOF: unclosed-block recovery + trailing
                # orphan comments
                while len(stack) > 1:
                    blk = stack.pop()
                    blk["unclosed"] = True
                    diag.hit("UNCLOSED_BLOCK")
                root["trailing_comments"] = (
                    pending_leading + leading
                )
                break

            all_leading = pending_leading + leading
            pending_leading = []

            if stmt["kind"] == "empty":
                pending_leading = all_leading
                continue
            stmt["leading_comments"] = all_leading

            kind = stmt["kind"]

            if kind == "block_end":
                if len(stack) == 1:
                    diag.extra.append(
                        f"unmatched_closing_brace:"
                        f"{stmt['line_start']}"
                    )
                    stmt["kind"] = "unmatched_close"
                    self._attach(stack, root, stmt)
                else:
                    popped = stack.pop()
                    popped["trailing_comments"] = stmt.get(
                        "leading_comments"
                    ) or []
                continue

            if kind == "angle_close":
                self._close_angle(stack, stmt, diag)
                continue

            if kind in ("block_open", "angle_open", "section"):
                stmt.setdefault("children", [])
                if kind == "section":
                    # sections are sibling nodes; later
                    # statements attach under them (scope
                    # preservation)
                    stack[-1]["children"].append(stmt)
                    stack[-1]["current_section"] = stmt
                    continue
                self._attach(stack, root, stmt)
                stack.append(stmt)
                continue

            self._attach(stack, root, stmt)

        return root, diag

    @staticmethod
    def _attach(stack, root, stmt):
        """Attach a statement: prefer the current section
        (scope_path preservation)."""
        target = stack[-1].get("current_section")
        if target is None or target is stack[-1]:
            target = stack[-1]
        target["children"].append(stmt)

    @staticmethod
    def _close_angle(stack, stmt, diag):
        """Angle closing tag: same name on top → pop; same
        name deeper in the stack → intermediate frames marked
        unclosed; no name match → extra reason (no
        guessing)."""
        name = (stmt.get("name") or "").lower()
        for idx in range(len(stack) - 1, 0, -1):
            frame = stack[idx]
            if frame["kind"] == "angle_open" and (
                frame.get("name") or ""
            ).lower() == name:
                for orphan in stack[idx + 1:]:
                    orphan["unclosed"] = True
                    diag.hit("UNCLOSED_BLOCK")
                del stack[idx:]
                return
        diag.extra.append(
            f"angle_close_unmatched:{stmt['line_start']}"
        )

    def _next_statement(self, sc: _ConfScanner, diag: _Diag):
        """Scan the next logical statement → (stmt | None,
        leading_comments)."""
        text = sc.text
        leading: list[dict] = []
        while True:
            sc.skip_ws()
            if sc.at_eof():
                return None, leading
            if sc.at_comment():
                c = sc.consume_comment()
                if c["raw"].strip():
                    leading.append(c)
                continue
            if sc.peek() == ";" and sc.peek(1) != "":
                # statement-leading `;` → line comment (smb/grub
                # convention, dialect disclosure)
                nl = text.find("\n", sc.pos)
                end = sc.n if nl == -1 else nl
                raw = text[sc.pos:end]
                if raw[1:].strip():
                    leading.append({
                        "kind": "line",
                        "text": raw[1:].strip(),
                        "raw": raw,
                        "line_start": sc.line_of(sc.pos),
                        "line_end": sc.line_of(max(end - 1, sc.pos)),
                    })
                sc.pos = end
                continue
            break

        start_pos = sc.pos
        start_line = sc.line_of(start_pos)
        tokens: list = []
        buf: list = []
        separator = None
        value_start = None
        value_quote = None
        continuation = None
        terminated = "eof"
        inline_comment = None
        stop_pos = None  # end of the statement body (excluding
                         # terminator/inline comment)

        def flush():
            if buf:
                tokens.append({"raw": "".join(buf), "quoted": False})
                buf.clear()

        while True:
            if sc.at_eof():
                flush()
                terminated = "eof"
                stop_pos = sc.pos
                break
            ch = sc.peek()

            # ---- continuation (backslash, outside quotes) ----
            if ch == "\\":
                nxt = sc.peek(1)
                if nxt == "\n":
                    continuation = "backslash"
                    sc.pos += 2
                    continue
                if nxt == "\r" and sc.peek(2) == "\n":
                    continuation = "backslash"
                    sc.pos += 3
                    continue
                if nxt == "":
                    diag.hit("INVALID_CONTINUATION")
                    continuation = "backslash_eof"
                    sc.pos += 1
                    continue
                buf.append(ch)
                sc.pos += 1
                continue

            # ---- quoted token (# ; { } = inside quotes do not
            # truncate) ----
            if ch in ('"', "'") and not buf:
                if separator is None:
                    raw_q, closed = sc.consume_quoted(ch)
                    tokens.append({
                        "raw": raw_q, "quoted": True, "quote": ch,
                    })
                    if not closed:
                        diag.hit("UNCLOSED_QUOTE")
                        continuation = (
                            continuation or "unclosed_quote"
                        )
                    continue
                # value mode: quotes are kept verbatim in the
                # raw value
                if value_quote is None:
                    value_quote = ch
                    buf.append(ch)
                    sc.pos += 1
                    continue
                if value_quote == ch:
                    value_quote = None
                    buf.append(ch)
                    sc.pos += 1
                    continue
                buf.append(ch)
                sc.pos += 1
                continue

            # ---- whitespace ----
            if ch in " \t\r":
                if separator is None:
                    flush()
                else:
                    buf.append(ch)
                sc.pos += 1
                continue
            if ch == "\n":
                flush()
                terminated = "newline"
                stop_pos = sc.pos
                sc.pos += 1
                if value_quote is not None:
                    # quote inside the value unclosed at end of
                    # line — recorded truthfully, recovery at
                    # the line boundary
                    diag.hit("UNCLOSED_QUOTE")
                    continuation = continuation or "unclosed_quote"
                break

            at_boundary = (
                not buf
                or (separator is not None and buf[-1] in " \t")
            ) and value_quote is None

            # ---- comment (token boundary; not truncated inside
            # quotes) ----
            if sc.at_comment() and at_boundary:
                stop_pos = sc.pos
                c = sc.consume_comment()
                if c["raw"].strip():
                    inline_comment = c
                terminated = "comment"
                break

            if separator is None:
                # ---- structural chars (statement-head mode) ----
                if ch == "[" and not tokens and not buf:
                    sc.pos += 1
                    while sc.pos < sc.n and sc.peek() not in ("]", "\n"):
                        sc.pos += 1
                    closed = sc.peek() == "]"
                    name_raw = text[start_pos + 1:sc.pos]
                    if closed:
                        sc.pos += 1
                    else:
                        diag.hit("MISSING_TERMINATOR")
                    tokens.append({
                        "raw": f"[{name_raw}]",
                        "syntax": "section",
                        "name": name_raw.strip(),
                        "closed": closed,
                    })
                    # `[a b]`: multiple tokens inside brackets
                    # are not a valid section name —
                    # EXTRA_ARGUMENT recorded truthfully (the
                    # name is kept verbatim)
                    if len(name_raw.split()) > 1:
                        diag.hit("EXTRA_ARGUMENT")
                    continue
                if ch == "<" and not tokens and not buf:
                    tok = self._consume_angle(sc)
                    if tok is None:
                        continue
                    if not tok["closed"]:
                        diag.hit("MISSING_TERMINATOR")
                    tokens.append(tok)
                    continue
                if ch == "{":
                    flush()
                    terminated = "brace_open"
                    stop_pos = sc.pos
                    sc.pos += 1
                    break
                if ch == "}":
                    flush()
                    terminated = "brace_close"
                    stop_pos = sc.pos
                    sc.pos += 1
                    break
                if ch == ";":
                    flush()
                    terminated = "semicolon"
                    stop_pos = sc.pos
                    sc.pos += 1
                    break
                if ch == "=":
                    flush()
                    if not tokens:
                        diag.hit("INVALID_DIRECTIVE")
                        buf.append(ch)
                        sc.pos += 1
                        continue
                    separator = "="
                    sc.pos += 1
                    value_start = sc.pos
                    continue
                if ch == ":":
                    nxt = sc.peek(1)
                    if nxt in (" ", "\t", "\n", "\r", "") and (
                        tokens or buf
                    ):
                        flush()
                        separator = ":"
                        sc.pos += 1
                        value_start = sc.pos
                        continue
                    buf.append(ch)
                    sc.pos += 1
                    continue
                buf.append(ch)
                sc.pos += 1
                continue

            # ---- value mode: kept verbatim to the terminator ----
            if ch == ";" and at_boundary:
                flush()
                terminated = "semicolon"
                stop_pos = sc.pos
                sc.pos += 1
                break
            if ch == "}" and at_boundary:
                flush()
                terminated = "brace_close"
                stop_pos = sc.pos
                sc.pos += 1
                break
            if ch in ("{", "}", ";", ":"):
                buf.append(ch)
                sc.pos += 1
                continue
            buf.append(ch)
            sc.pos += 1

        end_pos = sc.pos
        end_line = sc.line_of(max(end_pos - 1, start_pos))
        raw_full = text[start_pos:end_pos]
        if stop_pos is None:
            stop_pos = end_pos
        body = text[start_pos:stop_pos]
        body_stripped = body.strip()
        # value region: after the separator up to the end of
        # the statement body (one absolute-coordinate slice)
        value_raw = ""
        if separator is not None and value_start is not None:
            value_raw = text[value_start:stop_pos].strip()

        stmt = {
            "kind": "empty",
            "raw": body_stripped,
            "raw_full": raw_full,
            "line_start": start_line,
            "line_end": end_line,
            "terminated": terminated,
            "continuation": continuation,
            "inline_comment": inline_comment,
        }

        if terminated == "eof" and tokens and not text.endswith("\n"):
            diag.hit("TRUNCATED_INPUT")

        sec_tok = next(
            (t for t in tokens if t.get("syntax") == "section"), None,
        )
        angle_tok = next(
            (t for t in tokens if t.get("syntax", "").startswith("angle")),
            None,
        )

        if sec_tok is not None:
            extra = [t for t in tokens if t is not sec_tok]
            if extra or separator:
                diag.hit("EXTRA_ARGUMENT")
            stmt["kind"] = "section"
            stmt["name"] = sec_tok["name"]
            return stmt, leading

        if angle_tok is not None:
            if angle_tok["syntax"] == "angle_close":
                stmt["kind"] = "angle_close"
                stmt["name"] = angle_tok["name"]
            else:
                stmt["kind"] = "angle_open"
                stmt["name"] = angle_tok["name"]
                stmt["args_raw"] = angle_tok.get("args", "")
            if len(tokens) > 1:
                diag.hit("EXTRA_ARGUMENT")
            return stmt, leading

        if terminated == "brace_open":
            stmt["kind"] = "block_open"
            if tokens:
                stmt["name"] = tokens[0]["raw"]
                stmt["labels"] = [t["raw"] for t in tokens[1:]]
            else:
                stmt["name"] = None
                stmt["labels"] = []
                diag.extra.append(
                    f"anonymous_block:{start_line}"
                )
            return stmt, leading

        if terminated == "brace_close":
            if not tokens:
                stmt["kind"] = "block_end"
                return stmt, leading
            # trailing statement before `}` (missing `;`
            # tolerated)
            self._classify_body(stmt, tokens, separator, value_raw,
                                body, body_stripped, diag)
            return stmt, leading

        if not tokens and not separator:
            return stmt, leading  # empty (bare `;`/blank line)

        self._classify_body(
            stmt, tokens, separator, value_raw, body, body_stripped,
            diag,
        )
        return stmt, leading

    @staticmethod
    def _consume_angle(sc: _ConfScanner):
        """Consume an `<...>` container tag → token dict, or
        None (empty tag)."""
        text = sc.text
        start = sc.pos
        p = start + 1
        quote = None
        while p < sc.n:
            ch = text[p]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == ">":
                break
            elif ch == "\n":
                return {
                    "raw": text[start:p],
                    "syntax": "angle_open",
                    "name": "",
                    "args": "",
                    "closed": False,
                }
            p += 1
        if p >= sc.n:
            raw = text[start:sc.n]
            sc.pos = sc.n
            inner = raw[1:]
            return {
                "raw": raw, "syntax": "angle_open",
                "name": inner.split(None, 1)[0] if inner.strip() else "",
                "args": "", "closed": False,
            }
        raw = text[start:p + 1]
        inner = raw[1:-1]
        sc.pos = p + 1
        parts = inner.split(None, 1)
        name = parts[0] if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""
        if name.startswith("/"):
            return {
                "raw": raw, "syntax": "angle_close",
                "name": name[1:], "closed": True,
            }
        return {
            "raw": raw, "syntax": "angle_open",
            "name": name, "args": args, "closed": True,
        }

    @staticmethod
    def _classify_body(stmt, tokens, separator, value_raw, body,
                       body_stripped, diag):
        """Statement classification (five syntax classes +
        include/plugin/shell detection)."""
        raw_full = stmt["raw_full"]

        # command substitution (detected, never executed;
        # whole-statement scope including key_value/shell)
        if _CMD_SUB_RE.search(raw_full):
            stmt["cmd_substitution"] = True

        # shell_like: shell keyword as the leading token
        # (identify-only, never executed)
        first_raw = tokens[0]["raw"] if tokens else ""
        if first_raw and (
            first_raw in _SHELL_KEYWORDS
            or first_raw.lower() in _SHELL_KEYWORDS
        ):
            stmt["kind"] = "shell_like"
            stmt["name"] = first_raw
            stmt["args_raw"] = body_stripped[len(first_raw):].strip()
            return

        if separator is not None and tokens:
            key_raw = tokens[0]["raw"]
            stmt["kind"] = "key_value"
            stmt["key"] = key_raw
            stmt["separator"] = separator
            stmt["value_raw"] = value_raw
            stmt["value_clean"] = ConfParser._unquote(value_raw)
            return

        # generic_directive
        name_raw = tokens[0]["raw"] if tokens else ""
        stmt["kind"] = "directive"
        stmt["name"] = name_raw
        if tokens and (
            tokens[0].get("quoted") or _BAD_NAME_RE.match(name_raw)
        ):
            diag.hit("INVALID_DIRECTIVE")
        args_raw = ""
        if body_stripped.startswith(name_raw):
            args_raw = body_stripped[len(name_raw):].strip()
        stmt["args_raw"] = args_raw
        name_l = name_raw.lower()
        if name_l in _INCLUDE_DIRECTIVES:
            stmt["directive_class"] = "include"
            if not args_raw:
                diag.hit("MISSING_ARGUMENT")
        elif name_l in _PLUGIN_DIRECTIVES:
            stmt["directive_class"] = "plugin_load"

    @staticmethod
    def _unquote(raw: str) -> str:
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            inner = raw[1:-1]
            if '"' == raw[0]:
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            return inner
        return raw

    # ==================================
    # Contract building (config_entry_v1.1)
    # ==================================

    def _build_contract(self, root, diag, detection, text):
        contract = cc.build_contract("conf", cc.DUP_OVERRIDE_CHAIN)
        entries = contract["entries"]
        containers = contract["containers"]
        comments = contract["comments"]
        reasons = contract["loss"]["reasons"]
        includes: list = []
        loss_codes: list = []
        observed: set = set()

        def flush_group(cs, bound_path, bound_kind):
            if not cs:
                return
            comments.append(cc.make_comment(
                "block",
                "\n".join(c["text"] for c in cs),
                "\n".join(c["raw"] for c in cs),
                cs[0]["line_start"], cs[-1]["line_end"],
                bound_path=bound_path, bound_kind=bound_kind,
            ))

        def emit_include(entry, stmt, path, attach=True):
            """include registration (zero IO; duplicate
            occurrences are registered too — full
            preservation)."""
            target_raw = stmt["args_raw"].split(None, 1)[0] \
                if stmt["args_raw"] else ""
            target = self._unquote(target_raw)
            if _URL_RE.match(target):
                t_type = "url"
                loss_codes.append("EXTERNAL_URL_BLOCKED")
            elif any(c in target for c in _GLOB_CHARS):
                t_type = "glob"
            elif "$" in target:
                t_type = "variable"
            else:
                t_type = "file"
            if _TRAVERSAL_SEG.search(target):
                loss_codes.append("PATH_TRAVERSAL_BLOCKED")
            inc = {
                "raw_target": target_raw,
                "target_type": t_type,
                "resolution_status": "not_resolved",  # zero IO
                "directive": stmt["name"],
                "line_start": stmt["line_start"],
            }
            if attach:
                entry["include"] = inc
            includes.append(dict(inc, path=list(path)))

        def emit_statement(stmt, prefix, entry_map, block_seen,
                           depth, inherited_dup=False):
            if depth > _MAX_DEPTH:
                return
            kind = stmt["kind"]
            path_prefix = list(prefix)
            bound = None

            if kind == "key_value":
                path = path_prefix + [stmt["key"]]
                ps = cc.path_str(path)
                flush_group(
                    stmt.get("leading_comments"), path, "key",
                )
                if ps in entry_map:
                    existing = entry_map[ps]
                    existing["duplicate"] = True
                    if existing["override_chain"] is None:
                        existing["override_chain"] = [{
                            "value": existing["raw_value"],
                            "line": existing["line_start"],
                        }]
                    existing["override_chain"].append({
                        "value": stmt["value_raw"],
                        "line": stmt["line_start"],
                    })
                    diag.hit("DUPLICATE_DIRECTIVE")
                    return
                desc, desc_lines = self._desc_of(stmt)
                entry = cc.make_entry(
                    path, stmt["key"], stmt["value_raw"],
                    stmt["value_clean"],
                    line_start=stmt["line_start"],
                    line_end=stmt["line_end"],
                    description=desc, description_lines=desc_lines,
                    duplicate=inherited_dup,
                )
                entry["conf_syntax"] = "key_value"
                entry["separator"] = stmt["separator"]
                observed.add("key_value")
                cand = self._candidate_types(stmt["value_clean"])
                if cand:
                    entry["candidate_types"] = cand
                if stmt.get("continuation"):
                    entry["continuation_type"] = stmt["continuation"]
                refs = cc.detect_references(stmt["value_raw"])
                if refs:
                    entry["references"] = refs
                if stmt.get("cmd_substitution"):
                    entry["command_substitution"] = True
                    loss_codes.append("COMMAND_EXECUTION_BLOCKED")
                entries.append(entry)
                entry_map[ps] = entry
                bound = path

            elif kind in ("directive", "shell_like"):
                name = stmt.get("name") or ""
                path = path_prefix + [name]
                ps = cc.path_str(path)
                flush_group(stmt.get("leading_comments"), path, "key")
                if ps in entry_map:
                    existing = entry_map[ps]
                    existing["duplicate"] = True
                    if existing["override_chain"] is None:
                        existing["override_chain"] = [{
                            "value": existing["raw_value"],
                            "line": existing["line_start"],
                        }]
                    existing["override_chain"].append({
                        "value": stmt.get("args_raw", ""),
                        "line": stmt["line_start"],
                    })
                    diag.hit("DUPLICATE_DIRECTIVE")
                    # duplicate include occurrence: the
                    # registry keeps every occurrence; the
                    # entry side goes through override_chain
                    # (no duplicate include key)
                    if stmt.get("directive_class") == "include":
                        emit_include(existing, stmt, path, attach=False)
                    return
                desc, desc_lines = self._desc_of(stmt)
                entry = cc.make_entry(
                    path, name, stmt.get("args_raw", ""),
                    stmt.get("args_raw", ""),
                    line_start=stmt["line_start"],
                    line_end=stmt["line_end"],
                    description=desc, description_lines=desc_lines,
                    duplicate=inherited_dup,
                )
                if kind == "shell_like":
                    entry["conf_syntax"] = "shell_like"
                    entry["execute_semantics"] = _EXECUTE_SEMANTICS
                    loss_codes.append("COMMAND_EXECUTION_BLOCKED")
                else:
                    entry["conf_syntax"] = "generic_directive"
                    entry["arguments_raw"] = stmt.get("args_raw", "")
                    dclass = stmt.get("directive_class")
                    if dclass == "include":
                        emit_include(entry, stmt, path)
                    elif dclass == "plugin_load":
                        entry["directive_class"] = "plugin_load"
                        entry["execute_semantics"] = _EXECUTE_SEMANTICS
                        loss_codes.append("PLUGIN_LOAD_BLOCKED")
                    if stmt.get("cmd_substitution"):
                        entry["command_substitution"] = True
                        loss_codes.append("COMMAND_EXECUTION_BLOCKED")
                    # candidate types for arguments too (the
                    # generic layer lists candidates only, no
                    # coercion)
                    cand = self._candidate_types(stmt.get("args_raw", ""))
                    if cand:
                        entry["candidate_types"] = cand
                observed.add(entry["conf_syntax"])
                entries.append(entry)
                entry_map[ps] = entry
                bound = path

            # kind == "unmatched_close" needs no handling: the
            # statement layer (_parse_statements) already wrote
            # the unmatched_closing_brace extra reason; writing
            # it here too would double-count the same line.

            if stmt.get("inline_comment") and bound:
                c = stmt["inline_comment"]
                comments.append(cc.make_comment(
                    "inline", c["text"], c["raw"],
                    c["line_start"], c["line_end"],
                    bound_path=bound, bound_kind="key",
                ))

        def emit_scope(node, prefix, block_seen, depth):
            """section / brace_block / angle container layer."""
            if depth > _MAX_DEPTH:
                return
            kind = node["kind"]
            if kind == "root":
                child_map = {}
                for child in node["children"]:
                    emit_statement(
                        child, prefix, child_map, block_seen, depth,
                    )
                    if child["kind"] in (
                        "section", "block_open", "angle_open",
                    ):
                        emit_scope(child, prefix, block_seen, depth + 1)
                flush_group(node.get("trailing_comments"), None, None)
                return

            if kind == "section":
                path = list(prefix) + [node["name"]]
            elif kind == "angle_open":
                path = list(prefix) + [node["name"] or ""]
            else:  # block_open
                labels = [
                    self._unquote(lab) for lab in node.get("labels") or []
                ]
                path = (
                    list(prefix) + [node["name"] or ""] + labels
                )
            ps = cc.path_str(path)
            flush_group(node.get("leading_comments"), path, "section")

            is_dup = ps in block_seen
            if is_dup:
                container = block_seen[ps]
                container["duplicate"] = True
                container.setdefault("occurrences", []).append({
                    "line_start": node["line_start"],
                    "line_end": node["line_end"],
                })
                diag.hit("DUPLICATE_BLOCK")
            else:
                container = cc.make_container(
                    path, "dict",
                    line_start=node["line_start"],
                    line_end=node["line_end"],
                )
                container["conf_syntax"] = (
                    "section" if kind == "section"
                    else "brace_block" if kind == "block_open"
                    else "angle_container"
                )
                if kind == "angle_open" and node.get("args_raw"):
                    container["arguments"] = node["args_raw"]
                if node.get("unclosed"):
                    container["unclosed"] = True
                containers.append(container)
                block_seen[ps] = container
            observed.add(container["conf_syntax"])

            child_map = {}
            for child in node["children"]:
                emit_statement(
                    child, path, child_map, block_seen, depth + 1,
                    inherited_dup=is_dup,
                )
                if child["kind"] in (
                    "section", "block_open", "angle_open",
                ):
                    emit_scope(child, path, block_seen, depth + 1)
            flush_group(node.get("trailing_comments"), path, "section")

        emit_scope(root, [], {}, 0)

        cc.fill_child_counts(entries, containers)
        reasons.extend(diag.reasons())
        # Family convention: sensitive disclosure (zero hits,
        # zero keys — zero drift)
        sensitive_count = cc.scan_sensitive_entries(entries)
        if sensitive_count:
            reasons.append(f"sensitive_entry_values:{sensitive_count}")
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")

        # include registration (top-level additive key)
        if includes:
            contract["includes"] = includes

        # dialect capability declaration (top-level additive
        # key)
        contract["dialect"] = {
            "profile": detection["profile"],
            "syntax_classes": sorted(observed),
            "comments": "#-;-//-and-block",
            "quotes": "paired-single-double",
            "continuation": "backslash+quoted_multiline",
            "terminator": "semicolon-or-newline",
            "type_inference": "candidate_only",
            "duplicate_policy": "all_occurrences_preserved",
            "include": "registered_not_resolved",
            "execution": _EXECUTE_SEMANTICS,
        }
        return contract, loss_codes

    @staticmethod
    def _desc_of(stmt):
        cs = stmt.get("leading_comments") or []
        if not cs:
            return None, None
        return (
            cs[0]["text"],
            [c["line_start"] for c in cs],
        )

    @staticmethod
    def _candidate_types(raw):
        """Candidate-type weak declaration (integer/boolean/
        duration; disclosed but never coerced — different
        services interpret them differently)."""
        if not raw:
            return None
        types = []
        if _INT_RE.match(raw):
            types.append("integer")
        if raw.lower() in _BOOLEAN_WORDS:
            types.append("boolean")
        if _DURATION_RE.match(raw):
            types.append("duration")
        return types or None

    # ==================================
    # structured_data projection
    # ==================================

    def _flat_statements(self, node, scope=(), out=None):
        if out is None:
            out = []
        for child in node["children"]:
            entry = {
                "kind": child["kind"],
                "name": child.get("name") or child.get("key"),
                "scope_path": list(scope),
                "line_start": child["line_start"],
                "line_end": child["line_end"],
                "raw": child.get("raw"),
            }
            out.append(entry)
            if child["kind"] in (
                "section", "block_open", "angle_open",
            ):
                self._flat_statements(
                    child, tuple(scope) + (entry["name"] or "",), out,
                )
        return out

    def _structure(self, node):
        d = {}
        for child in node["children"]:
            k = child.get("name") or child.get("key") \
                or f"@line_{child['line_start']}"
            if child["kind"] in (
                "section", "block_open", "angle_open",
            ):
                d[k] = self._structure(child)
            elif child["kind"] == "key_value":
                d[k] = child.get("value_clean", child.get("value_raw"))
            else:
                d[k] = child.get("args_raw", "")
        return d

    # ==================================
    # Three-state + six-state
    # ==================================

    @staticmethod
    def _status_of(diag, detection, contract, loss_codes):
        counts = diag.counts
        if "INVALID_ENCODING" in counts:
            syntax_status = "failed"
        elif any(
            k.startswith(("UNCLOSED", "TRUNCATED", "INVALID"))
            for k in counts
        ):
            syntax_status = "partial"
        else:
            syntax_status = "complete"
        includes = contract.get("includes") or []
        sensitive = any(
            r.startswith("sensitive_entry_values:")
            for r in contract["loss"]["reasons"]
        )
        security = (
            "review_required"
            if loss_codes or sensitive
            else "no_candidates"
        )
        # The overall state is driven by the syntax recovery
        # layer only; profile_uncertain is an orthogonal
        # disclosure (carried by profile_status in the six
        # states, it never downgrades the overall state —
        # honest, not stacked)
        overall = (
            "FAILED" if syntax_status == "failed"
            else "PARTIAL" if syntax_status == "partial"
            else "COMPLETE"
        )
        return {
            "overall": overall,
            "syntax_status": syntax_status,
            "profile_status": detection["resolution_status"],
            "schema_status": "unknown",
            "include_status": "not_resolved" if includes else "none",
            "runtime_status": "not_executed",
            "security_status": security,
        }
