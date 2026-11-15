# ============================================================
# Structured Config Domain
# Generic `.cnf` Content-Gated Parser (P0)
# ============================================================
"""Generic ``.cnf`` parser.

Positioning: ``.cnf`` is an **extension family, not a single syntax
family** (MySQL/MariaDB option files + OpenSSL configs + generic INI
shapes) — content detection takes precedence over extension
semantics, and this channel routes fully independently of
``.conf``. This parser is the content-gated entry point:

1. read the raw text first (this file only, per the encoding-diagnosis chain);
2. the content detector decides profile / confidence /
   alternative_profiles / resolution_status — five-way routing:
   mysql_cnf / mariadb_cnf / openssl_cnf / tool_specific_cnf /
   generic_ini_candidate;
3. machine-verifiable cross-family evidence (JSON/XML/YAML) → hand
   off to the corresponding parser for reuse +
   ``extension_format_mismatch`` disclosure (the kv + ``[section]``
   shape is native ``.cnf`` syntax and is never handed off);
4. everything else → generic-layer recovery: section / key_value /
   flag / include directives + comments + line continuation;
5. ``$variable`` / ``@section`` references are identify-only (no
   environment reads, no expansion, no certificate reads);
6. includes are registered with zero IO (resolution_status="not_resolved");
7. duplicate keys/sections are all preserved without adjudication
   (merge/override semantics belong to the Profile layer);
8. config_entry_v1.1 contract + cnf_profile metadata disclosure.

Red lines: zero execution (OpenSSL/MySQL/includes/external commands
are never executed), additive zero drift (``.conf``/``.ini``/``.cfg``
routing unchanged), zero IO (includes are never read, expanded, or
directory-scanned), honest detection (a bare ``[section]`` never
implies MySQL/OpenSSL; when uncertain → generic_ini_candidate +
profile_uncertain), and tool-profile semantics stay out of the
generic layer (MySQL option groups / option_form shapes / OpenSSL
ENV provenance distinctions belong to the Profile layer).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

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

_FORMAT = "cnf"
_MAX_STATEMENTS = 100_000
_EXECUTE_SEMANTICS = "not-executed-disclosed"

# ------------------------------------------------------------
# Content detection: filename hints (evidence type filename)
# ------------------------------------------------------------
_FILENAME_PROFILE_HINTS = {
    "my.cnf": ("mysql_cnf", 0.9),
    "mysql.cnf": ("mysql_cnf", 0.85),
    "mysqld.cnf": ("mysql_cnf", 0.9),
    "client.cnf": ("mysql_cnf", 0.75),
    "server.cnf": ("mysql_cnf", 0.75),
    "mariadb.cnf": ("mariadb_cnf", 0.9),
    "openssl.cnf": ("openssl_cnf", 0.9),
    "ssl.cnf": ("openssl_cnf", 0.85),
}

# tool_specific_cnf candidate words (candidates only, never resolved)
_TOOL_SPECIFIC_WORDS = (
    "php", "sphinx", "lighttpd", "varnish", "heartbeat",
    "corosync", "ganesha",
)

# section features (evidence type section; raw names compared after trim)
_MYSQL_SECTION_HINTS = frozenset((
    "client", "mysqld", "mysqld_safe", "mysqldump", "mysqladmin",
    "mysqlhotcopy", "server", "client-server", "mysql", "isamchk",
    "mysqlimport", "mysqlshow",
))
_MARIADB_SECTION_HINTS = frozenset((
    "galera", "mariadb", "mariabackup", "mariadb-dump",
))
_OPENSSL_SECTION_EXACT = frozenset((
    "req", "ca", "tsa", "engine", "crl", "ocsp", "functions",
    "modules", "default_ca", "ca_default",
))
_OPENSSL_SECTION_PREFIXES = (
    "policy_", "v3_", "alt_names", "req_distinguished_name",
    "distinguished_name",
)

# syntax-feature hints (evidence type syntax; candidates only, no tool semantics)
_SYNTAX_PROFILE_HINTS = (
    (re.compile(r"^\s*!\s*include(?:dir|_if_exists)?\b", re.M),
     "mysql_cnf", 0.8),
    (re.compile(r"^\s*\.include\b", re.M), "openssl_cnf", 0.8),
    (re.compile(
        r"^\s*(?:dir|default_bits|default_days|default_md|"
        r"distinguished_name|x509_extensions|req_extensions|"
        r"copy_extensions|encrypt_key|string_mask)\s*=",
        re.M,
    ), "openssl_cnf", 0.7),
    (re.compile(r"^\s*(?:DNS|IP|URI|RID|email)\.\d+\s*=", re.M),
     "openssl_cnf", 0.65),
    (re.compile(r"^\s*(?:skip|loose|enable|disable)[-_]", re.M),
     "mysql_cnf", 0.6),
    (re.compile(r"\$\{?ENV::", ), "openssl_cnf", 0.6),
    (re.compile(r"^\s*@\w+", re.M), "openssl_cnf", 0.5),
)

# ------------------------------------------------------------
# include registration (identify, never read); MySQL tristate directives + OpenSSL .include
# ------------------------------------------------------------
_CNF_INCLUDE_DIRECTIVES = {
    "!include": "file",
    "!includedir": "directory",
    "!include_if_exists": "file_if_exists",
    ".include": "file",
}

# ------------------------------------------------------------
# Lexical constants
# ------------------------------------------------------------
_SECTION_RE = re.compile(r"^\[([^\]\n]*)\](\s.*)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?(?:ms|s|m|h|d|w)$")
_BOOLEAN_WORDS = frozenset((
    "yes", "no", "on", "off", "true", "false", "enabled", "disabled",
))
_URL_RE = re.compile(r"^(?:https?|ftp)://", re.I)
_TRAVERSAL_SEG = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
_GLOB_CHARS = ("*", "?", "[")

# reference shapes (identify-only — no environment reads, no expansion)
_VAR_BRACE_RE = re.compile(r"\$\{([^{}]*)\}")
_VAR_ENV_RE = re.compile(r"\$\{?ENV::([A-Za-z_][A-Za-z0-9_]*)\}?")
_VAR_BARE_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_SECTION_REF_RE = re.compile(r"^@([A-Za-z_][A-Za-z0-9_.\-]*)$")
_VAR_NAME_RE = re.compile(r"^(?:ENV::)?[A-Za-z_][A-Za-z0-9_]*$")
_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f]")

# sensitive/path candidates (findings channel — never enters contract loss)
_PRIVATE_KEY_EXT = (
    ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore",
)
_CERT_EXT = (".crt", ".cer", ".csr", ".pem")
_CRED_URL_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
)


class _Diag:
    """Syntax-level diagnostic counts (contract loss channel)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def hit(self, code: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1

    def reasons(self) -> list:
        return [f"{k}:{v}" for k, v in self.counts.items()]


# ------------------------------------------------------------
# Cross-family handoff (machine-verifiable evidence only; narrowed to three families)
# ------------------------------------------------------------

def _detect_existing_family(text: str):
    """Cross-family handoff decision (JSON/XML/YAML only; returns (family, conf) or None).

    The kv + ``[section]`` shape is native ``.cnf`` syntax — the
    ini/toml/hcl branches do not apply (the key difference from
    the ``.conf`` gate).
    """
    s = text.lstrip()
    if not s:
        return None
    if s[:1] in "{[":
        try:
            json.loads(s)
            return "json", 0.98
        except ValueError:
            pass  # invalid JSON → no guessing, fall through to generic-layer recovery
    if s[:5].lower() == "<?xml" or s[:9].lower() == "<!doctype":
        return "xml", 0.95
    if text.startswith("---"):
        return "yaml", 0.85
    return None


# ------------------------------------------------------------
# Content detector
# ------------------------------------------------------------

def detect_cnf_profile(text: str, file_name: str) -> dict:
    """`.cnf` profile detector → detection result dict (evidence-only).

    Five-way routing: mysql_cnf / mariadb_cnf / openssl_cnf /
    tool_specific_cnf (candidate) / generic_ini_candidate.
    Three evidence sources (filename / section features / syntax
    features) merge by max; a candidate ≥0.7 → resolved; otherwise
    generic_ini_candidate + profile_uncertain (honest degradation —
    a bare ``[section]`` never implies MySQL/OpenSSL).
    """
    family = _detect_existing_family(text)
    if family:
        fam, conf = family
        return {
            "detected_syntax": fam,
            "profile": f"{fam}_family",
            "confidence": conf,
            "alternative_profiles": [],
            "resolution_status": "handed_off",
            "evidence_type": "native_cnf",
            "extension_format_mismatch": True,  # disclosed
        }

    merged: dict[str, tuple[float, list[str]]] = {}

    def add(name: str, conf: float, evidence: str) -> None:
        if name in merged:
            prev_conf, prev_ev = merged[name]
            merged[name] = (
                max(conf, prev_conf),
                prev_ev + [evidence] if evidence not in prev_ev
                else prev_ev,
            )
        else:
            merged[name] = (conf, [evidence])

    # evidence source 1: filename
    hint = _FILENAME_PROFILE_HINTS.get(file_name.lower())
    if hint:
        add(hint[0], hint[1], "filename")
    else:
        lowered = file_name.lower()
        for word in _TOOL_SPECIFIC_WORDS:
            if word in lowered:
                add("tool_specific_cnf", 0.6, "filename")
                break

    # evidence source 2: section features
    section_names = [
        m.group(1).strip()
        for m in re.finditer(r"^\s*\[([^\]\n]+)\]\s*$", text, re.M)
    ]
    for name in section_names:
        if name in _MYSQL_SECTION_HINTS:
            add("mysql_cnf", 0.75, "section")
        if name in _MARIADB_SECTION_HINTS:
            add("mariadb_cnf", 0.75, "section")
        if (
            name in _OPENSSL_SECTION_EXACT
            or any(
                name.startswith(p) for p in _OPENSSL_SECTION_PREFIXES
            )
        ):
            add("openssl_cnf", 0.75, "section")

    # evidence source 3: syntax features
    for rx, name, conf in _SYNTAX_PROFILE_HINTS:
        if rx.search(text):
            add(name, conf, "syntax")

    # mariadb promotion: mysql-family hit plus mariadb evidence
    # (or "mariadb" in the filename) → mariadb_cnf absorbs the
    # mysql_cnf votes (MariaDB is MySQL-syntax-compatible;
    # intra-family refinement)
    if (
        "mysql_cnf" in merged
        and ("mariadb_cnf" in merged or "mariadb" in file_name.lower())
    ):
        conf, ev = merged.pop("mysql_cnf")
        add("mariadb_cnf", conf, ev[0])

    if not merged:
        return {
            "detected_syntax": "cnf",
            "profile": "generic_ini_candidate",
            "confidence": 0.0,
            "alternative_profiles": [],
            "resolution_status": "profile_uncertain",
            "evidence_type": "generic_ini_recovered",
        }

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
            "detected_syntax": "cnf",
            "profile": top_name,
            "confidence": top_conf,
            "alternative_profiles": alternatives,
            "resolution_status": "resolved",
            "evidence_type": (
                "mysql_profile" if top_name.startswith("mysql")
                else "openssl_profile" if top_name.startswith("openssl")
                else "inferred"
            ),
        }
    # below threshold: all candidates (incl. top) go into
    # alternatives (alternative_profiles lists every candidate
    # considered)
    return {
        "detected_syntax": "cnf",
        "profile": "generic_ini_candidate",
        "confidence": top_conf,
        "alternative_profiles": [
            {"profile": n, "confidence": c, "evidence": ev}
            for n, (c, ev) in ranked
        ],
        "resolution_status": "profile_uncertain",
        "evidence_type": "generic_ini_recovered",
    }


# ------------------------------------------------------------
# Statement-level parsing
# ------------------------------------------------------------

class CnfParser(BaseParser):
    """Generic ``.cnf`` content-gated parser (recovery semantics).

    Four generic-layer statement kinds: section / key_value / flag /
    include directives; comments ``#`` and ``;`` (never cut inside
    quotes — ``password = abc#123`` is preserved); backslash
    continuation (physical_lines preserved); duplicate key →
    override_chain, duplicate section → all instances preserved
    (merge/override semantics are adjudicated by the Profile layer).
    """

    def __init__(self, encoding: str = "utf-8-sig"):
        """Init: encoding is only the read-fallback basis (INVALID_ENCODING code on failure)."""
        self.encoding = encoding

    # ==================================
    # BaseParser interface
    # ==================================

    def parse(self, file_path) -> Document:
        """Entry point: content detection → handoff or generic layer, producing a Document + contract."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path, _FORMAT, self.encoding, True,
            )
            text = ctx.text
            metadata = ctx.metadata
            file_name = Path(file_path).name

            # content detection takes precedence over extension semantics
            detection = detect_cnf_profile(text, file_name)

            # machine-verifiable cross-family evidence → hand off and reuse (no second implementation)
            if detection["resolution_status"] == "handed_off":
                doc = self._delegate_parse(
                    detection["detected_syntax"], file_path,
                )
                self._attach_handoff_profile(doc, detection)
                return doc

            # --- generic-layer recovery ---
            stmts, diag = self._parse_statements(text)
            if metadata.get("encoding_fallback"):
                diag.hit("INVALID_ENCODING")
            if detection["resolution_status"] == "profile_uncertain":
                diag.hit("UNKNOWN_CNF_PROFILE")

            metadata["parse_success"] = not any(
                k.startswith(("INVALID", "UNCLOSED", "TRUNCATED"))
                for k in diag.counts
            )
            metadata["top_level_keys"] = [
                s.get("name") or s.get("key")
                for s in stmts
                if s.get("name") or s.get("key")
            ][:200]
            metadata["structured_data"] = {
                "statements": self._flat_statements(stmts),
                "structure": self._structure(stmts),
            }

            contract, loss_codes, security_findings = (
                self._build_contract(
                    stmts, diag, detection,
                    # BIDI scanning uses the raw source (sanitized text already replaced
                    # unsafe invisible chars with U+FFFD; the raw text is the faithful evidence)
                    ctx.source_text or text,
                )
            )
            metadata["config_entries"] = contract

            # object-level unresolved registry (family convention; guarded silent degradation)
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive
                _log.debug("config unresolved registry skipped: %s", exc)

            # cnf_profile disclosure (incl. blocking/finding loss_codes +
            # security_findings — findings are not losses)
            profile = dict(detection)
            if loss_codes:
                profile["loss_codes"] = loss_codes
            if security_findings:
                profile["security_findings"] = security_findings
            metadata["cnf_profile"] = profile

            # six-axis status disclosure
            metadata["cnf_status"] = self._status_of(
                diag, detection, contract, loss_codes, security_findings,
            )

            # Profile-semantics pass (separate module, pure
            # disclosure, zero evaluation, zero IO; guarded hookup,
            # silent degradation never affects the contract chain).
            # Build gate: resolved and profile ∈ {mysql_cnf,
            # mariadb_cnf, openssl_cnf}; semantic-layer codes land in
            # cnf_tool_profile["loss_codes"], contract loss.reasons
            # stays drift-free.
            try:
                from provendoc.core.parsers.config.cnf_tool_profile import (
                    build_cnf_tool_profile,
                )
                tool_profile = build_cnf_tool_profile(
                    contract, detection, file_name, text,
                    file_path=str(file_path),
                )
                if tool_profile is not None:
                    metadata["cnf_tool_profile"] = tool_profile
                    # file_role disclosed alongside (conf-parser precedent)
                    file_role = tool_profile.get("file_role") or {}
                    if file_role.get("file_role"):
                        profile["file_role"] = file_role["file_role"]
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("cnf tool profile pass skipped: %s", exc)

            # schema/security-candidate pass (separate module, pure
            # disclosure, zero evaluation, zero IO; guarded hookup,
            # silent degradation). Same build gate as the tool-profile
            # pass (resolved and one of the three profiles); codes land
            # in cnf_schema_profile["loss_codes"], contract
            # loss.reasons stays drift-free; zero new entry-level
            # keys. The schema_status axis activates when this pass
            # produces output: it flips to version_unknown (constant
            # when there is no version input), otherwise it stays
            # not_applicable (absence-disclosure).
            try:
                from provendoc.core.parsers.config.cnf_schema_profile import (
                    build_cnf_schema_security_profile,
                )
                schema_profile = build_cnf_schema_security_profile(
                    contract, detection, file_name, text,
                    file_path=str(file_path),
                )
                if schema_profile is not None:
                    metadata["cnf_schema_profile"] = schema_profile
                    metadata["cnf_status"]["schema_status"] = (
                        schema_profile.get(
                            "schema_status", "version_unknown",
                        )
                    )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("cnf schema profile pass skipped: %s", exc)

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

    def parse_many_merged(self, file_paths):
        """Multi-file merge: include graph + per-profile merging.

        Follows the conf ``parse_many_merged`` thin-wrapper
        precedent: each file goes through the existing single-file
        contract channel — disk-reading responsibility stays with
        the caller's paths list (the merge-layer module itself does
        zero IO and never reads directories; includedir is only
        string-prefix matched). Contracts + cnf_profile detections +
        cnf_tool_profiles are collected, then
        ``cnf_include_merge.merge_cnf_contracts`` builds the include
        graph (three-tier matching + glob + optional tier), decides
        graph codes (no new vocab), preserves load order, and applies
        per-profile merging (mysql/mariadb later_include_last_wins /
        openssl include-point inline expansion / generic degraded
        tier). The single-file ``parse`` channel stays frozen (purely
        additive method; graph codes land only in the merged
        contract loss.reasons).

        Returns:
            merged contract dict (not a Document — no single raw
            text across files; source attribution lives in
            entry/container source_file / source_file_index, load
            order in include_graph.resolved_file_order and
            directive_order / scope_order).

        Raises:
            ValueError: a resolved profile mixes the mysql family
                with openssl (mixed-profile rejection, following the
                SDK's mixed-family rejection precedent).
        """
        from provendoc.core.parsers.config.cnf_include_merge import (
            merge_cnf_contracts,
        )

        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        detections = []
        tool_profiles = []
        paths = []
        for i, (fp, doc) in enumerate(zip(file_paths, docs)):
            meta = doc.metadata
            contracts.append(
                meta.get("config_entries")
                or cc.build_contract("cnf", cc.DUP_OVERRIDE_CHAIN),
            )
            sources.append({
                "index": i,
                "file_name": meta.get("file_name") or Path(fp).name,
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
            detections.append(meta.get("cnf_profile"))
            tool_profiles.append(meta.get("cnf_tool_profile"))
            paths.append(str(fp))
        return merge_cnf_contracts(
            contracts, sources, detections, tool_profiles, paths,
        )

    # ==================================
    # Cross-family handoff
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
        raise ValueError(f"unknown delegate family: {family!r}")

    @staticmethod
    def _attach_handoff_profile(doc: Document, detection: dict) -> None:
        """Attach additive detection disclosure after handoff (never rewrites existing keys)."""
        try:
            md = doc.metadata
            if isinstance(md, dict) and "cnf_profile" not in md:
                md["cnf_profile"] = {
                    "detected_syntax": detection["detected_syntax"],
                    "profile": detection["profile"],
                    "confidence": detection["confidence"],
                    "alternative_profiles": detection[
                        "alternative_profiles"
                    ],
                    "resolution_status": "handed_off",
                    "evidence_type": "native_cnf",
                    "extension_format_mismatch": True,  # disclosed
                    "delegated_to": (
                        f"{detection['detected_syntax']} parser"
                    ),
                }
        except Exception:  # pragma: no cover - defensive guard
            _log.debug("cnf handoff profile attach skipped")

    # ==================================
    # Statement-level parsing (line-wise + continuation merge)
    # ==================================

    def _parse_statements(self, text: str):
        """Line-wise scan → statement list + diagnostic counts (no section pre-collection here)."""
        diag = _Diag()
        stmts: list[dict] = []
        pending: list[dict] = []  # leading comment group
        lines = text.splitlines()
        i = 0
        n = len(lines)
        while i < n and len(stmts) < _MAX_STATEMENTS:
            raw_line = lines[i]
            line_no = i + 1
            stripped = raw_line.strip()

            # blank line: ends the previous value's continuation chain
            if not stripped:
                i += 1
                continue

            # line comment (# or ; line-start convention) → pending group
            if stripped.startswith("#") or stripped.startswith(";"):
                pending.append({
                    "text": stripped.lstrip("#;").strip(),
                    "raw": raw_line,
                    "line_start": line_no,
                    "line_end": line_no,
                })
                i += 1
                continue

            # include directives (MySQL ! tristate + OpenSSL .include)
            if stripped.startswith("!") or stripped.startswith("."):
                first = stripped.split(None, 1)[0]
                if first in _CNF_INCLUDE_DIRECTIVES:
                    rest = stripped.split(None, 1)[1].strip() \
                        if len(stripped.split(None, 1)) > 1 else ""
                    stmt = {
                        "kind": "include",
                        "directive": first,
                        "include_type": _CNF_INCLUDE_DIRECTIVES[first],
                        "raw_target": rest,
                        "line_start": line_no,
                        "line_end": line_no,
                        "leading_comments": list(pending),
                    }
                    pending.clear()
                    stmts.append(stmt)
                    i += 1
                    continue
                if stripped.startswith("!"):
                    # unknown ! directive (MySQL errors on it) — preserved + coded
                    diag.hit("INVALID_DIRECTIVE")
                    stmts.append({
                        "kind": "invalid",
                        "raw": raw_line,
                        "line_start": line_no,
                        "line_end": line_no,
                        "leading_comments": list(pending),
                    })
                    pending.clear()
                    i += 1
                    continue
                # other dot-prefixed lines → fall through to kv judgment (no over-rejection)

            # section header
            if stripped.startswith("["):
                m = _SECTION_RE.match(stripped)
                if m and m.group(2) is None:
                    stmt = {
                        "kind": "section",
                        "raw_section": stripped,
                        "name": m.group(1).strip(),
                        "line_start": line_no,
                        "line_end": line_no,
                        "leading_comments": list(pending),
                    }
                else:
                    # unclosed (no ] at line end) — INVALID_SECTION with line-boundary recovery
                    diag.hit("INVALID_SECTION")
                    inner = stripped[1:].rstrip()
                    if inner.endswith("]"):
                        inner = inner[:-1]
                    stmt = {
                        "kind": "section",
                        "raw_section": stripped,
                        "name": inner.strip(),
                        "invalid": True,
                        "line_start": line_no,
                        "line_end": line_no,
                        "leading_comments": list(pending),
                    }
                pending.clear()
                stmts.append(stmt)
                i += 1
                continue

            # kv / flag: find the separator outside quotes (three shapes)
            stmt, consumed = self._parse_kv_line(
                lines, i, pending, diag,
            )
            pending.clear()
            if stmt is not None:
                stmts.append(stmt)
            i += consumed

        # trailing comments at EOF → orphan (bound to None)
        if pending:
            stmts.append({
                "kind": "orphan_comment",
                "comments": list(pending),
                "line_start": pending[0]["line_start"],
                "line_end": pending[-1]["line_end"],
            })
        return stmts, diag

    def _parse_kv_line(self, lines, i, pending, diag):
        """Parse one logical kv/flag line + merge backslash continuations.

        Returns (stmt, consumed_physical_lines); stmt=None means a
        blank line was skipped (should not happen — the caller
        filters them).
        """
        raw_line = lines[i]
        line_no = i + 1
        n = len(lines)
        body = raw_line.strip()

        # locate the first = or : separator outside quotes
        sep_pos = -1
        sep_char = ""
        in_quote = ""
        for idx, ch in enumerate(body):
            if in_quote:
                if ch == in_quote:
                    in_quote = ""
                continue
            if ch in "\"'":
                in_quote = ch
                continue
            if ch in ("=", ":") and sep_pos < 0:
                sep_pos = idx
                sep_char = ch
                break

        if sep_pos >= 0:
            raw_key = body[:sep_pos].strip()
            value_part = body[sep_pos + 1:]
            separator = sep_char
        else:
            tokens = body.split(None, 1)
            if len(tokens) > 1:
                raw_key = tokens[0]
                value_part = tokens[1]
                separator = "space"  # MySQL `key value` convention
            else:
                # single token, no separator → flag (flag_entry candidate)
                return {
                    "kind": "flag",
                    "raw_key": body,
                    "key": body.strip(),
                    "line_start": line_no,
                    "line_end": line_no,
                    "leading_comments": list(pending),
                }, 1

        value_raw = value_part
        physical_lines = [line_no]
        # backslash continuation (line-end \ outside quotes)
        continuation = None
        consumed = 1
        while (
            value_raw.rstrip().endswith("\\")
            and not value_raw.rstrip().endswith("\\\\")
            and i + consumed < n
        ):
            nxt = lines[i + consumed]
            nxt_stripped = nxt.strip()
            if not nxt_stripped or nxt_stripped.startswith(("#", ";")):
                break  # continuation stops at a blank line/comment (conservative recovery)
            continuation = "backslash"
            value_raw = value_raw.rstrip()[:-1] + nxt_stripped
            physical_lines.append(i + consumed + 1)
            consumed += 1
        if value_raw.rstrip().endswith("\\") and i + consumed >= n:
            # EOF backslash with no next line — folded into
            # INVALID_CONTINUATION (vocab conservation)
            diag.hit("INVALID_CONTINUATION")
            value_raw = value_raw.rstrip()[:-1]

        # inline comment (outside quotes, whitespace-preceded #/;) + quote-pair check
        value_clean, inline_comment, unclosed = self._split_value(
            value_raw,
        )
        if unclosed:
            diag.hit("UNCLOSED_QUOTE")

        key = raw_key.strip()
        return {
            "kind": "key_value",
            "raw_key": raw_key,
            "key": key,
            "separator": separator,
            "value_raw": value_part.strip(),
            "value_clean": value_clean,
            "inline_comment": inline_comment,
            "continuation": continuation,
            "physical_lines": physical_lines,
            "line_start": line_no,
            "line_end": physical_lines[-1],
            "leading_comments": list(pending),
        }, consumed

    @staticmethod
    def _split_value(value_raw: str):
        """Value-channel three-way split: quote-pair check + inline-comment strip + dequote.

        - ``#``/``;`` inside quotes never cut (``password =
          abc#123`` is preserved);
        - inline comment = cut from a **whitespace-preceded**
          ``#``/``;`` outside quotes;
        - unclosed quote → UNCLOSED_QUOTE (value preserved, never
          swallows the line).
        """
        if not value_raw:
            return "", None, False
        in_quote = ""
        cut = -1
        for idx, ch in enumerate(value_raw):
            if in_quote:
                if ch == in_quote:
                    in_quote = ""
                continue
            if ch in "\"'":
                in_quote = ch
                continue
            if ch in "#;" and idx > 0 and value_raw[idx - 1] in " \t":
                cut = idx
                break
        unclosed = bool(in_quote)
        body = value_raw if cut < 0 else value_raw[:cut]
        inline = value_raw[cut:].strip() if cut >= 0 else None
        body_s = body.strip()
        if (
            len(body_s) >= 2
            and body_s[0] in "\"'"
            and body_s[-1] == body_s[0]
            and not unclosed
        ):
            body_s = body_s[1:-1]
        return body_s, inline, unclosed

    # ==================================
    # Contract building
    # ==================================

    def _build_contract(self, stmts, diag, detection, text):
        contract = cc.build_contract("cnf", cc.DUP_OVERRIDE_CHAIN)
        entries = contract["entries"]
        containers = contract["containers"]
        comments = contract["comments"]
        reasons = contract["loss"]["reasons"]
        includes: list = []
        loss_codes: list = []
        security_findings: list = []
        observed: set = set()

        # two-pass: collect all normalized section names first (@ref basis)
        section_names = {
            s["name"] for s in stmts if s["kind"] == "section" and s["name"]
        }

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

        section_state: dict[str, dict] = {}  # name → container/entry_map
        current_section: dict | None = None  # None = top level
        top_entry_map: dict[str, dict] = {}  # top-level duplicate-key chain

        def emit(stmt):
            nonlocal current_section
            kind = stmt["kind"]

            if kind == "orphan_comment":
                flush_group(stmt["comments"], None, None)
                return

            if kind == "invalid":
                flush_group(
                    stmt.get("leading_comments"), None, None,
                )
                entries.append(cc.make_entry(
                    [f"@line_{stmt['line_start']}"], None,
                    stmt["raw"], stmt["raw"],
                    line_start=stmt["line_start"],
                    line_end=stmt["line_end"],
                ))
                return

            if kind == "include":
                # include zero-IO registration (repeats preserved too)
                flush_group(
                    stmt.get("leading_comments"), None, "include",
                )
                target = stmt["raw_target"].strip()
                if _URL_RE.match(target):
                    t_type = "url"
                    loss_codes.append("EXTERNAL_URL_BLOCKED")
                elif any(c in target for c in _GLOB_CHARS):
                    t_type = "glob"
                elif "$" in target:
                    t_type = "variable"
                else:
                    t_type = stmt["include_type"]
                if _TRAVERSAL_SEG.search(target):
                    loss_codes.append("PATH_TRAVERSAL_BLOCKED")
                inc = {
                    "node_type": "include",
                    "include_type": t_type,
                    "directive": stmt["directive"],
                    "raw_target": stmt["raw_target"],
                    "resolved_files": [],  # zero IO
                    "resolution_status": "not_resolved",
                    # node-level reason value space: uses the "reason"
                    # key, not "reason_code" — the latter is the
                    # literal space of the vocab-conservation scan,
                    # which the node-level value space must never enter
                    "reason": "EXTERNAL_INCLUDE_NOT_READ",
                    "line_start": stmt["line_start"],
                }
                includes.append(inc)
                observed.add("include")
                return

            if kind == "section":
                name = stmt.get("name") or ""
                flush_group(
                    stmt.get("leading_comments"), [name], "section",
                )
                state = section_state.get(name)
                if state is None:
                    container = cc.make_container(
                        [name], "dict",
                        line_start=stmt["line_start"],
                        line_end=stmt["line_end"],
                    )
                    container["raw_section"] = stmt.get("raw_section")
                    container["normalized_section"] = name
                    if stmt.get("invalid"):
                        container["invalid_section"] = True
                    container["occurrences"] = [{
                        "line_start": stmt["line_start"],
                        "line_end": stmt["line_end"],
                    }]
                    containers.append(container)
                    section_state[name] = {
                        "container": container,
                        "entry_map": {},
                    }
                else:
                    # duplicate section: all instances preserved without
                    # adjudication (merge/override/multi-context
                    # semantics decided by the Profile layer)
                    state["container"]["duplicate"] = True
                    state["container"]["occurrences"].append({
                        "line_start": stmt["line_start"],
                        "line_end": stmt["line_end"],
                    })
                    diag.hit("DUPLICATE_SECTION")
                current_section = section_state[name]
                observed.add("section")
                return

            if kind == "flag":
                state = current_section
                sec_name = (
                    state["container"]["path"][0] if state else None
                )
                path = [sec_name, stmt["key"]] if sec_name else [stmt["key"]]
                ps = cc.path_str(path)
                flush_group(
                    stmt.get("leading_comments"), path, "key",
                )
                entry_map = state["entry_map"] if state else top_entry_map
                if ps in entry_map:
                    existing = entry_map[ps]
                    existing["duplicate"] = True
                    if existing["override_chain"] is None:
                        existing["override_chain"] = [{
                            "value": existing["raw_value"],
                            "line": existing["line_start"],
                        }]
                    existing["override_chain"].append({
                        "value": None,
                        "line": stmt["line_start"],
                    })
                    diag.hit("DUPLICATE_KEY")
                    return
                desc, desc_lines = self._desc_of(stmt)
                entry = cc.make_entry(
                    path, stmt["key"], None, None,
                    line_start=stmt["line_start"],
                    line_end=stmt["line_end"],
                    description=desc, description_lines=desc_lines,
                )
                entry["cnf_kind"] = "flag_entry"
                entry["raw_key"] = stmt.get("raw_key")
                entry["value_type"] = "presence_flag_candidate"
                if state:
                    entry["section"] = sec_name
                    entry["raw_section"] = state["container"].get(
                        "raw_section",
                    )
                entries.append(entry)
                entry_map[ps] = entry
                observed.add("flag")
                return

            if kind == "key_value":
                state = current_section
                sec_name = (
                    state["container"]["path"][0] if state else None
                )
                path = [sec_name, stmt["key"]] if sec_name else [stmt["key"]]
                ps = cc.path_str(path)
                flush_group(
                    stmt.get("leading_comments"), path, "key",
                )
                entry_map = state["entry_map"] if state else top_entry_map
                if ps in entry_map:
                    # duplicate key: override_chain fully preserved, no adjudication
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
                    diag.hit("DUPLICATE_KEY")
                    return
                desc, desc_lines = self._desc_of(stmt)
                entry = cc.make_entry(
                    path, stmt["key"], stmt["value_raw"],
                    stmt["value_clean"],
                    line_start=stmt["line_start"],
                    line_end=stmt["line_end"],
                    description=desc, description_lines=desc_lines,
                )
                entry["cnf_kind"] = "config_entry"
                entry["raw_key"] = stmt.get("raw_key")
                entry["separator"] = stmt["separator"]
                if state:
                    entry["section"] = sec_name
                    entry["raw_section"] = state["container"].get(
                        "raw_section",
                    )
                if stmt.get("continuation"):
                    entry["continuation_type"] = stmt["continuation"]
                    entry["physical_lines"] = list(
                        stmt["physical_lines"],
                    )
                    entry["line_breaks_preserved"] = True
                if stmt.get("inline_comment"):
                    comments.append(cc.make_comment(
                        "inline", stmt["inline_comment"].lstrip("#;")
                        .strip(), stmt["inline_comment"],
                        stmt["line_start"], stmt["line_end"],
                        bound_path=path, bound_kind="key",
                    ))
                cand = self._candidate_types(stmt["value_clean"])
                if cand:
                    entry["candidate_types"] = cand
                # references identify-only (incl. @section resolution)
                refs, ref_codes = self._value_references(
                    stmt["value_clean"], section_names, detection,
                )
                if refs:
                    entry["cnf_references"] = refs
                    std_refs = cc.detect_references(stmt["value_raw"])
                    if std_refs:
                        entry["references"] = std_refs
                for code in ref_codes:
                    diag.hit(code)
                # sensitive/path candidates (findings channel)
                self._security_scan(
                    stmt, entry, security_findings, loss_codes,
                )
                entries.append(entry)
                entry_map[ps] = entry
                observed.add("key_value")
                return

        for stmt in stmts:
            emit(stmt)

        cc.fill_child_counts(entries, containers)
        reasons.extend(diag.reasons())
        # family convention: sensitive disclosure (no keys when no hits — zero drift)
        sensitive_count = cc.scan_sensitive_entries(entries)
        if sensitive_count:
            reasons.append(f"sensitive_entry_values:{sensitive_count}")
            loss_codes.append("SENSITIVE_VALUE_DETECTED")
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")

        if includes:
            contract["includes"] = includes

        # BIDI control-character detection: scanned on the
        # newline-normalized text, first hit line number disclosed
        bidi_m = _BIDI_RE.search(text or "")
        if bidi_m:
            line_no = text.count("\n", 0, bidi_m.start()) + 1
            loss_codes.append("BIDI_CONTROL_DETECTED")
            security_findings.append({
                "code": "BIDI_CONTROL_DETECTED",
                "key": None,
                "masked": "****",
                "line_start": line_no,
                "path": [],
            })

        contract["dialect"] = {
            "profile": detection["profile"],
            "syntax_classes": sorted(observed),
            "comments": "#-and-;-line-and-inline",
            "quotes": "paired-single-double",
            "continuation": "backslash",
            "separators": "eq-colon-space",
            "type_inference": "candidate_only",
            "duplicate_policy": "all_occurrences_preserved",
            "include": "registered_not_resolved",
            "variables": "identify_only_not_expanded",
            "section_references": "identify_only",
            "execution": _EXECUTE_SEMANTICS,
        }
        return contract, loss_codes, security_findings

    @staticmethod
    def _desc_of(stmt):
        cs = stmt.get("leading_comments") or []
        if not cs:
            return None, None
        return cs[0]["text"], [c["line_start"] for c in cs]

    @staticmethod
    def _candidate_types(raw):
        """Weak value-type candidate declaration (integer/boolean/duration — disclosure only, no forced conversion, value keeps the original string)."""
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

    @staticmethod
    def _value_references(value, section_names, detection):
        """Reference identification (identify-only — zero environment reads, zero expansion).

        Returns (cnf_references, diag_codes):
        - ``$dir``/``${dir}``/``$ENV::X`` → variable_reference
          (resolved_value always null, not_resolved);
        - ``@target`` → section_reference (resolved when target is
          in this document's section set; otherwise
          MISSING_SECTION_REFERENCE);
        - a bare-name value exactly matching a seen section name →
          section_reference candidate (weak declaration;
          confirmation belongs to the Profile layer).
        """
        if not value or not isinstance(value, str):
            return [], []
        refs: list[dict] = []
        codes: list[str] = []
        # @section reference (prefix form — OpenSSL subjectAltName etc.)
        m = _SECTION_REF_RE.match(value.strip())
        if m:
            target = m.group(1)
            resolved = target in section_names
            if not resolved:
                codes.append("MISSING_SECTION_REFERENCE")
            refs.append({
                "node_type": "section_reference",
                "raw_value": value,
                "target_section": target,
                "resolved": resolved,
                "resolution_status": (
                    "resolved" if resolved else "unresolved"
                ),
            })
            return refs, codes
        # $ variables (incl. $ENV::X)
        seen: set = set()
        for mm in _VAR_ENV_RE.finditer(value):
            seen.add((mm.group(0), f"ENV::{mm.group(1)}", "environment"))
        for mm in _VAR_BRACE_RE.finditer(value):
            inner = mm.group(1)
            if not _VAR_NAME_RE.match(inner):
                # ${} empty/illegal name — INVALID_VARIABLE_REFERENCE
                codes.append("INVALID_VARIABLE_REFERENCE")
                continue
            if inner.startswith("ENV::"):
                key = (mm.group(0), inner, "environment")
                if key not in seen:
                    seen.add(key)
            else:
                seen.add((mm.group(0), inner, "variable"))
        for mm in _VAR_BARE_RE.finditer(value):
            # skip the bare-name sub-match of $ENV::X (already registered by the ENV channel)
            if value[mm.end():].startswith("::"):
                continue
            key = (mm.group(0), mm.group(1), "variable")
            if key not in seen:
                seen.add(key)
        for full, var, rtype in sorted(seen):
            refs.append({
                "node_type": "variable_reference",
                "raw_value": full,
                "variable": var,
                "reference_type": rtype,
                "resolved_value": None,  # no environment reads, no expansion
                "resolution_status": "not_resolved",
            })
        # bare-name section-reference candidate (weak declaration)
        if (
            not refs
            and value.strip() in section_names
            and detection.get("profile") == "openssl_cnf"
        ):
            refs.append({
                "node_type": "section_reference",
                "raw_value": value,
                "target_section": value.strip(),
                "resolved": True,
                "resolution_status": "resolved",
                "candidate_only": True,
            })
        return refs, codes

    @staticmethod
    def _security_scan(stmt, entry, security_findings, loss_codes):
        """Sensitive path/value candidates (findings channel — never enters contract loss)."""
        value = stmt.get("value_clean") or ""
        key = stmt.get("key") or ""
        line = stmt.get("line_start")

        def finding(code):
            security_findings.append({
                "code": code,
                "key": key,
                "masked": "****",  # never store the value (family convention)
                "line_start": line,
                "path": list(entry["path"]),
            })

        if _CRED_URL_RE.match(value):
            loss_codes.append("CREDENTIAL_IN_URL")
            finding("CREDENTIAL_IN_URL")
        lowered = value.lower()
        if any(lowered.endswith(ext) for ext in _PRIVATE_KEY_EXT):
            if lowered.endswith(".pem"):
                # .pem is ambiguous (certificate or private key) — both candidates listed
                loss_codes.append("CERTIFICATE_PATH_DETECTED")
                finding("CERTIFICATE_PATH_DETECTED")
            loss_codes.append("PRIVATE_KEY_PATH_DETECTED")
            finding("PRIVATE_KEY_PATH_DETECTED")
        elif any(lowered.endswith(ext) for ext in _CERT_EXT):
            loss_codes.append("CERTIFICATE_PATH_DETECTED")
            finding("CERTIFICATE_PATH_DETECTED")

    # ==================================
    # structured_data projection
    # ==================================

    @staticmethod
    def _flat_statements(stmts):
        out = []
        scope = None
        for s in stmts:
            kind = s["kind"]
            entry = {
                "kind": kind,
                "scope_path": [scope] if scope else [],
                "line_start": s.get("line_start"),
                "line_end": s.get("line_end"),
            }
            if kind == "section":
                entry["name"] = s.get("name")
                entry["raw_section"] = s.get("raw_section")
                scope = s.get("name")
            elif kind in ("key_value", "flag"):
                entry["name"] = s.get("key")
                entry["raw_key"] = s.get("raw_key")
                if kind == "key_value":
                    entry["separator"] = s.get("separator")
                    entry["raw"] = s.get("value_raw")
                else:
                    entry["raw"] = None
            elif kind == "include":
                entry["name"] = s.get("directive")
                entry["raw"] = s.get("raw_target")
            else:
                entry["name"] = None
                entry["raw"] = s.get("raw")
            out.append(entry)
        return out

    @staticmethod
    def _structure(stmts):
        root: dict = {}
        current = root
        for s in stmts:
            kind = s["kind"]
            if kind == "section":
                current = root.setdefault(s.get("name") or "", {})
            elif kind == "key_value":
                current[s.get("key") or ""] = s.get(
                    "value_clean", s.get("value_raw"),
                )
            elif kind == "flag":
                current[s.get("key") or ""] = None
            elif kind == "include":
                current.setdefault(
                    f"@include_{s.get('line_start')}",
                    s.get("raw_target"),
                )
        return root

    # ==================================
    # tri-state + six-axis status
    # ==================================

    @staticmethod
    def _status_of(diag, detection, contract, loss_codes,
                   security_findings):
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
        # overall status is driven only by the syntax-recovery layer; profile_uncertain is orthogonal disclosure
        overall = (
            "FAILED" if syntax_status == "failed"
            else "PARTIAL" if syntax_status == "partial"
            else "COMPLETE"
        )
        security = (
            "review_required"
            if loss_codes or security_findings
            else "no_candidates"
        )
        return {
            "overall": overall,
            "syntax_status": syntax_status,
            "profile_status": detection["resolution_status"],
            "schema_status": "not_applicable",  # profile validation belongs to the Profile layer
            "include_status": "not_resolved" if includes else "none",
            "runtime_status": "not_executed",
            "security_status": security,
        }
