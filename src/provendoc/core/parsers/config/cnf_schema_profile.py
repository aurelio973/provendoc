# -*- coding: utf-8 -*-
"""MySQL version schema + security rules + effective-value four-way
distinction for cnf files.

Third semantic pass on the single-file cnf channel (syntax →
option form/group semantics → schema/security candidates).
Independent of cnf_tool_profile (a separate semantic layer),
attached at the tail of parse() behind a guard; failures degrade
silently via _log.debug. Build gate: detection resolved and
profile in {mysql_cnf, mariadb_cnf, openssl_cnf} — generic files
get no profile; the absent schema face is itself the honest
disclosure.

Non-negotiables:
- No execution, no IO: restricted external validation is always
disclosed as blocked (external_validation.status == "blocked");
no external source is ever read (the schema tables are pure
static knowledge).
- Candidate semantics: with no version input, schema decisions are
always version_unknown (off-table is never asserted invalid,
on-table never asserted valid — only candidates and counts are
disclosed); resolved_value / effective_value are always unknown
(the parser never evaluates).
- Zero new entry keys: schema/security/four-way semantics aggregate
entirely in the profile dict, leaving the option-form zero-drift
gate whitelist untouched.
- The two codes (UNKNOWN_MYSQL_OPTION / UNKNOWN_OPENSSL_SECTION,
both already registered in the vocab) land only in this
profile's loss_codes channel — never in contract loss.reasons.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------
# Constant tables: MySQL/MariaDB version schema (discrete
# knowledge rows, evidence-only). Version range = mainstream
# 5.7/8.0 coverage (None = always present — no guessing outside
# the range). MariaDB-only sections are listed separately
# (galera / mariabackup etc.).
# ------------------------------------------------------------

# Version range labels (no version input → always version_unknown
# candidates)
_SCHEMA_VERSIONS = ("5.7", "8.0")

_MYSQL_OPTION_SCHEMA: Dict[str, Dict[str, Any]] = {
    # shared / client sections
    "client": {
        "host": None,
        "port": None,
        "socket": None,
        "user": None,
        "password": None,
        "protocol": None,
        "ssl-mode": None,
        "ssl-key": None,
        "ssl-cert": None,
        "ssl-ca": None,
        "ssl-verify-server-cert": None,
        "default-character-set": None,
        "compress": None,
    },
    "client-server": {
        "host": None,
        "port": None,
        "socket": None,
        "user": None,
        "password": None,
        "ssl-ca": None,
    },
    "mysql": {
        "host": None,
        "port": None,
        "user": None,
        "password": None,
        "database": None,
        "default-character-set": None,
        "show-warnings": None,
        "pager": None,
    },
    # server sections
    "mysqld": {
        "port": None,
        "bind-address": None,
        "datadir": None,
        "socket": None,
        "pid-file": None,
        "log-error": None,
        "max_connections": None,
        "skip-name-resolve": None,
        "skip-networking": None,
        "skip-ssl": None,
        "performance_schema": None,
        "character-set-server": None,
        "collation-server": None,
        "slow-query-log": None,
        "slow_query_log": None,
        "log-bin": None,
        "server-id": None,
        "local-infile": None,
        "sql-mode": None,
        "default-authentication-plugin": "8.0",
        "innodb_buffer_pool_size": None,
        "key_buffer_size": None,
        "max_allowed_packet": None,
        "read_only": None,
        "tmpdir": None,
        "secure-file-priv": None,
        "require_secure_transport": None,
        # replication filters (multi-value key family)
        "replicate-do-db": None,
        "replicate-ignore-db": None,
        "replicate-do-table": None,
        "replicate-ignore-table": None,
        "replicate-wild-do-table": None,
        "replicate-wild-ignore-table": None,
        # TLS / authentication
        "ssl": None,
        "ssl-cert": None,
        "ssl-key": None,
        "ssl-ca": None,
        "admin-address": None,
    },
    "mysqldump": {
        "host": None,
        "port": None,
        "user": None,
        "password": None,
        "quick": None,
        "single-transaction": None,
        "default-character-set": None,
        "max_allowed_packet": None,
    },
    "mysqladmin": {"host": None, "port": None, "user": None},
    "mysqlshow": {"host": None, "port": None},
    "server": {"host": None, "port": None, "socket": None},
    # MariaDB dialect rows (dialect-specific static entries)
    "mariadb": {"port": None, "bind-address": None, "datadir": None},
    "mariabackup": {"target-dir": None, "backup": None},
    "galera": {
        "wsrep_on": None,
        "wsrep_cluster_address": None,
        "wsrep_node_address": None,
        "wsrep_sst_method": None,
        "wsrep_provider": None,
    },
}

# Common OpenSSL sections (the known side of
# UNKNOWN_OPENSSL_SECTION; common sections + CA/policy adjacent)
_OPENSSL_SECTION_SCHEMA = frozenset(
    (
        "openssl_init",
        "req",
        "v3_req",
        "v3_ca",
        "v3_intermediate",
        "ca",
        "CA_default",
        "policy_match",
        "policy_anything",
        "req_distinguished_name",
        "alt_names",
        "crl_ext",
        "engine",
        "tsa",
        "proxy_cert_ext",
        "ocsp",
    )
)

# ------------------------------------------------------------
# Constant table: configuration security rules (pure key/value
# matches, no version dependence). A hit appends to
# security_findings (no reason_code key — node-level values stay
# out of the vocab conservation scan space; following the include
# `reason` key precedent, findings use a `code` key with a
# _candidate suffix)
# ------------------------------------------------------------

_LONG_OPTION_RE = re.compile(r"^--([A-Za-z0-9_-]+)")
_SKIP_PREFIX = "skip-"

_SECURITY_RULES = (
    # (section set or None=any, key, value match set or None=any,
    # rule name); value matching must be a set — a bare string
    # would be iterated character by character in membership checks
    ({"mysqld"}, "bind-address", ("0.0.0.0",), "wide_bind_candidate"),
    ({"mysqld"}, "skip-ssl", None, "tls_disabled_candidate"),
    ({"mysqld", "client"}, "ssl", ("0", "off", "disabled"), "tls_disabled_candidate"),
    ({"mysqld"}, "skip-name-resolve", None, "name_resolution_disabled_candidate"),
    ({"mysqld"}, "local-infile", ("1", "on", "enabled", "true"), "local_infile_enabled_candidate"),
)

_SCHEMA_VERSION = "cnf_schema_p3_v1"

# Off-table sentinel: an in-table version value of None (always
# present) must be distinguishable from "not in the table" —
# dict.get's None ambiguity would misjudge the whole table
_NOT_IN_SCHEMA = object()


def _norm_option(entry: Dict[str, Any]) -> str:
    """Normalize an option key: strip the -- prefix (skip- prefix
    stripping is a candidate), trim and lowercase (schema-table
    match basis, same family as the option_form semantics)."""
    raw_key = str(entry.get("raw_key") or "")
    m = _LONG_OPTION_RE.match(raw_key)
    key = m.group(1) if m else str(entry.get("key") or "")
    key = key.strip().lower()
    # skip-x stripping is a candidate, not a fact; the schema
    # lookup tries both forms (skip-networking and networking can
    # both hit)
    return key


def _schema_lookup(section: str, option: str) -> Any:
    """Schema-table lookup (section table → option row);
    _NOT_IN_SCHEMA means off-table (an in-table version value of
    None counts as a hit)."""
    sec = _MYSQL_OPTION_SCHEMA.get(section)
    if sec is None:
        return _NOT_IN_SCHEMA
    if option in sec:
        return sec[option]
    return _NOT_IN_SCHEMA


def _apply_option_schema(
    contract: Dict[str, Any],
    profile_name: str,
    out: Dict[str, Any],
) -> None:
    """MySQL/MariaDB option schema decision (candidate-level
    aggregation).

    In-table hit → counted as known; off-table → disclosed in
    unknown_candidates + UNKNOWN_MYSQL_OPTION in loss_codes
    (candidates — no version input means version_unknown, never
    asserting invalid). The skip- stripped form is tried both ways
    (skip-networking stripped to networking counts as off-table
    only when both keys are off-table).
    """
    known = 0
    unknown: List[Dict[str, Any]] = []
    for e in contract.get("entries") or []:
        section = e.get("section") or ((e.get("path") or [None])[0] if e.get("path") else None)
        if not section:
            # MySQL options without a section are off-table (option
            # file semantics live entirely in groups — top-level keys
            # are unknown candidates)
            opt = _norm_option(e)
            if opt:
                unknown.append(
                    {
                        "key": str(e.get("key") or ""),
                        "section": None,
                        "line_start": e.get("line_start"),
                    }
                )
                continue
            continue
        opt = _norm_option(e)
        if not opt:
            continue
        hit = _schema_lookup(str(section), opt)
        if hit is _NOT_IN_SCHEMA and opt.startswith(_SKIP_PREFIX):
            stripped = opt[len(_SKIP_PREFIX) :]
            hit = _schema_lookup(str(section), stripped)
        if hit is not _NOT_IN_SCHEMA:
            known += 1
        else:
            unknown.append(
                {
                    "key": str(e.get("key") or ""),
                    "section": str(section),
                    "line_start": e.get("line_start"),
                }
            )
    total = known + len(unknown)
    out["option_schema"] = {
        "schema_version": _SCHEMA_VERSION,
        "profile": profile_name,
        "known_options": known,
        "unknown_candidates": unknown,
        "coverage": (round(known / total, 4) if total else None),
        "version_basis": list(_SCHEMA_VERSIONS),
        "version_input": "not_provided",
        "match_status": "version_unknown",
    }
    if unknown:
        out.setdefault("loss_codes", []).append("UNKNOWN_MYSQL_OPTION")


def _apply_openssl_section_schema(
    contract: Dict[str, Any],
    out: Dict[str, Any],
) -> None:
    """OpenSSL section schema decision (candidate-level aggregation)."""
    known = 0
    unknown: List[Dict[str, Any]] = []
    for cont in contract.get("containers") or []:
        name = (cont.get("path") or [None])[0]
        if not name:
            continue
        if str(name).strip().lower() in _OPENSSL_SECTION_SCHEMA:
            known += 1
        else:
            unknown.append(
                {
                    "section": str(name),
                    "line_start": cont.get("line_start"),
                }
            )
    total = known + len(unknown)
    out["option_schema"] = {
        "schema_version": _SCHEMA_VERSION,
        "profile": "openssl_cnf",
        "known_sections": known,
        "unknown_candidates": unknown,
        "coverage": (round(known / total, 4) if total else None),
        "version_input": "not_provided",
        "match_status": "version_unknown",
    }
    if unknown:
        out.setdefault("loss_codes", []).append("UNKNOWN_OPENSSL_SECTION")


def _apply_security_rules(
    contract: Dict[str, Any],
    out: Dict[str, Any],
) -> None:
    """Security rule candidates (pure key/value static matching, no
    version dependence).

    A hit appends to security_findings (code/evidence/line_start —
    no reason_code key, staying out of the vocab conservation
    scan space).
    """
    findings: List[Dict[str, Any]] = []
    for e in contract.get("entries") or []:
        section = e.get("section") or ((e.get("path") or [None])[0] if e.get("path") else None)
        key = _norm_option(e)
        value = e.get("value")
        vstr = str(value).strip().lower() if value is not None else None
        for sec_set, rkey, rvals, rule in _SECURITY_RULES:
            if key != rkey:
                continue
            if sec_set is not None and str(section) not in sec_set:
                continue
            if rvals is not None:
                if vstr is None or vstr not in (str(v).lower() for v in rvals):
                    continue
            findings.append(
                {
                    "code": rule,
                    "evidence": {
                        "key": str(e.get("key") or ""),
                        "value": e.get("value"),
                        "section": section,
                    },
                    "line_start": e.get("line_start"),
                }
            )
            break
    if findings:
        out["security_findings"] = findings


def _apply_value_resolution(
    contract: Dict[str, Any],
    out: Dict[str, Any],
) -> None:
    """Effective-value four-way distinction (pure disclosure on the
    single-file channel).

    raw_value / parsed_reference / resolved_value: unknown /
    effective_value: unknown — the parser never evaluates; the
    four-way distinction is a disclosure structure, not a
    computation. Entries carrying cnf_references (the parsed
    variable-reference face) are aggregated for disclosure.
    """
    resolutions: List[Dict[str, Any]] = []
    for e in contract.get("entries") or []:
        refs = e.get("cnf_references") or []
        parsed = [r for r in refs if r.get("node_type") == "variable_reference"]
        if not parsed:
            continue
        resolutions.append(
            {
                "key": str(e.get("key") or ""),
                "raw_value": e.get("raw_value"),
                "parsed_reference": [
                    r.get("name") or r.get("variable") or r.get("raw") for r in parsed
                ],
                "resolved_value": "unknown",
                "effective_value": "unknown",
            }
        )
    out["value_resolution"] = {
        "count": len(resolutions),
        "entries": resolutions,
        "disclosure": (
            "raw_value preserved verbatim; parsed_reference from P1 "
            "variable-reference face; resolved_value/effective_value "
            "remain unknown (resolver never evaluates - spec batch2 "
            "section 4 wording)"
        ),
    }


def build_cnf_schema_security_profile(
    contract: Dict[str, Any],
    detection: Dict[str, Any],
    file_name: str,
    text: str,
    file_path: str = "",
) -> Optional[Dict[str, Any]]:
    """Main entry: schema candidates + security rules + four-way
    distinction + restricted external validation blocked
    disclosure.

    Build gate: detection resolved and profile in {mysql_cnf,
    mariadb_cnf, openssl_cnf}; returning None is the absent
    disclosure (generic/uncertain files are not built — an absent
    schema face is the honest state).
    """
    status = (detection or {}).get("resolution_status")
    profile_name = (detection or {}).get("profile")
    if status != "resolved" or profile_name not in (
        "mysql_cnf",
        "mariadb_cnf",
        "openssl_cnf",
    ):
        return None
    if not text.strip():
        return None

    out: Dict[str, Any] = {
        "profile_version": _SCHEMA_VERSION,
        "profile": profile_name,
        # restricted external validation is always blocked (zero
        # IO, evidence-only — blocked is a steady-state disclosure,
        # not an anomaly signal)
        "external_validation": {
            "status": "blocked",
            "reason": "zero_io_evidence_only",
        },
    }

    if profile_name in ("mysql_cnf", "mariadb_cnf"):
        _apply_option_schema(contract, profile_name, out)
    else:
        _apply_openssl_section_schema(contract, out)

    _apply_security_rules(contract, out)
    _apply_value_resolution(contract, out)

    # schema_status axis: always version_unknown (no version
    # input; candidate semantics — off-table candidates are never
    # asserted invalid)
    out["schema_status"] = "version_unknown"
    return out
