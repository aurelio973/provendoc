# -*- coding: utf-8 -*-
"""MySQL/OpenSSL profile semantics layer for .cnf files.

Architectural position: the generic CnfParser layer (syntax
contract) is separated from profile semantics (this module).
This module is a standalone semantic pass hooked at the tail
of ``CnfParser.parse()``: input = the syntax contract
(entries/containers/includes) + the detection result + file
identity + original text; output = the pure-disclosure
structure ``metadata["cnf_tool_profile"]``.

Construction gate: the semantic layer is built only when
``cnf_profile.resolution_status == "resolved"`` and the
profile is one of {mysql_cnf, mariadb_cnf, openssl_cnf}.
profile_uncertain / generic_ini_candidate / tool_specific_cnf
(candidate-only by decision) / handed_off never build — no
semantics are guessed when uncertain, and the semantic
layer's absence is itself an honest disclosure.

Hard rules:
- zero execution, zero IO: never read include targets, never
  read environment variables ($ENV::X is never expanded),
  never touch credential/private-key/certificate files;
- additive zero drift: existing contract keys/codes/routing
  stay untouched; semantic-layer codes land in
  ``cnf_tool_profile["loss_codes"]`` (not in the contract's
  loss.reasons); entry/container/include only gain
  whitelisted additive keys; the existing
  ``cnf_references`` only gain ``confirmed_by``;
- honest boundary: group_kind / canonical_name_candidate /
  parsed_candidates / rule_kind / file_role are all
  candidates and never assert "config is valid" /
  "safe/unsafe" / "can issue certificates";
- findings go to security_findings (a finding is not a loss
  and must not dilute loss).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROFILE_VERSION = "cnf_tool_profile_p1"

#: construction gate: the semantic layer builds only when
#: resolved and the profile is one of the three
_CNF_SEMANTIC_PROFILES = frozenset(
    (
        "mysql_cnf",
        "mariadb_cnf",
        "openssl_cnf",
    )
)

# ============================================
# option-group semantics tables (evidence = common section
# names + example JSON structures)
# ============================================

_MYSQL_OPTION_GROUPS: Dict[str, Dict[str, Any]] = {
    "client": {
        "group_kind": "client_option_group",
        "consumer_candidates": [
            "mysql",
            "mysqldump",
            "mysqladmin",
            "mysqlshow",
        ],
    },
    "mysql": {
        "group_kind": "client_option_group",
        "consumer_candidates": ["mysql"],
    },
    "mysqld": {
        "group_kind": "server_option_group",
        "consumer_candidates": ["mysqld"],
    },
    "server": {
        "group_kind": "server_option_group",
        "consumer_candidates": ["mysqld"],
    },
    "mysqldump": {
        "group_kind": "tool_specific_option_group",
        "consumer_candidates": ["mysqldump"],
    },
    "mysqladmin": {
        "group_kind": "tool_specific_option_group",
        "consumer_candidates": ["mysqladmin"],
    },
    "mysqlshow": {
        "group_kind": "tool_specific_option_group",
        "consumer_candidates": ["mysqlshow"],
    },
    "client-server": {
        "group_kind": "shared_group",
        "consumer_candidates": ["client_programs", "mysqld"],
    },
}

#: mariadb extension table (evidence = the mariadb section
#: hints on the detection face)
_MARIADB_OPTION_GROUPS: Dict[str, Dict[str, Any]] = {
    "mariadb": {
        "group_kind": "client_option_group",
        "consumer_candidates": ["mariadb"],
    },
    "mariadbd": {
        "group_kind": "server_option_group",
        "consumer_candidates": ["mariadbd"],
    },
    "mariadb-dump": {
        "group_kind": "tool_specific_option_group",
        "consumer_candidates": ["mariadb-dump"],
    },
    "mariadb-admin": {
        "group_kind": "tool_specific_option_group",
        "consumer_candidates": ["mariadb-admin"],
    },
    "mariadb-backup": {
        "group_kind": "tool_specific_option_group",
        "consumer_candidates": ["mariadb-backup"],
    },
    "galera": {
        "group_kind": "server_option_group",
        "consumer_candidates": ["garbd", "mariadbd"],
    },
}

# ============================================
# option_form shapes
# ============================================

_LONG_OPTION_RE = re.compile(r"^-{1,2}([A-Za-z][A-Za-z0-9_-]*)$")
_SKIP_PREFIX = "skip-"

_BOOL_STYLE_WORDS: Dict[str, frozenset] = {
    "on_off": frozenset(("on", "off")),
    "yes_no": frozenset(("yes", "no")),
    "numeric": frozenset(("1", "0")),
}

# ============================================
# MySQL multi-value key candidate table (family symmetry:
# the replicate-* filtering family + plugin-load-add)
# ============================================

_MYSQL_MULTI_VALUE_KEYS = frozenset(
    (
        "replicate-do-db",
        "replicate-ignore-db",
        "replicate-do-table",
        "replicate-ignore-table",
        "replicate-wild-do-table",
        "replicate-wild-ignore-table",
        "plugin-load-add",
    )
)

# ============================================
# OpenSSL section-reference field-context confirmation gate
# (a bare-name value inside the field table is the only
# thing that confirms a section_reference)
# ============================================

_OPENSSL_SECTION_VALUE_FIELDS = frozenset(
    (
        "distinguished_name",
        "req_extensions",
        "x509_extensions",
        "extensions",
        "copy_extensions",
        "oid_section",
        "policy",
    )
)

#: literal $ shape (no legal variable name or brace after
#: the $ — the four-way classification)
_LITERAL_DOLLAR_RE = re.compile(r"\$(?![A-Za-z_{])")

# ============================================
# OpenSSL indexed keys (DNS.1/IP.1/email.1/URI.n/RID.n →
# base_key/index/raw_key)
# ============================================

_INDEXED_KEY_RE = re.compile(r"^(DNS|IP|email|URI|RID)\.(\d+)$", re.IGNORECASE)

# ============================================
# certificate-extension structured candidate key table
# ============================================

_OPENSSL_EXTENSION_KEYS = frozenset(
    (
        "subjectaltname",
        "keyusage",
        "extendedkeyusage",
        "basicconstraints",
        "authoritykeyidentifier",
        "subjectkeyidentifier",
        "certificatepolicies",
        "crldistributionpoints",
        "authorityinfoaccess",
    )
)

_SAN_PREFIX_RE = re.compile(r"^(DNS|IP|email|URI|RID):(.+)$", re.IGNORECASE)

# extension-candidate deepening static tables (the public
# X.509 standard face — zero version dependency, zero IO,
# discrete evidence-only rows, same family as the P3 schema
# tables)
_KEY_USAGE_BITS = frozenset(
    (
        "digitalsignature",
        "nonrepudiation",
        "keyencipherment",
        "dataencipherment",
        "keyagreement",
        "keycertsign",
        "crlsign",
        "encipheronly",
        "decipheronly",
    )
)
_EXT_KEY_USAGE_NAMES = frozenset(
    (
        "serverauth",
        "clientauth",
        "codesigning",
        "emailprotection",
        "timestamping",
        "ocspsigning",
        "anyextendedkeyusage",
    )
)
# OID shape: >= 3 dot-separated numeric segments
# (2.5.29.31 / 1.3.6.1.5.5.7.3.1)
_OID_RE = re.compile(r"^\d+(\.\d+){2,}$")
# basicConstraints constraint-pair shape: CA:true / pathlen:N
_CONSTRAINT_RE = re.compile(r"^(pathlen|CA):(.+)$", re.IGNORECASE)

# ============================================
# CA policy references and rules
# ============================================

_POLICY_REFERENCE_KEYS = frozenset(("policy",))

_POLICY_RULE_KINDS = frozenset(("match", "supplied", "optional"))

# ============================================
# template tiers (zero-IO string judging)
# ============================================

_EXAMPLE_SUFFIXES = (".example", ".sample", ".dist")
_TEMPLATE_SUFFIXES = (".template", ".tpl", ".in")
_TOKEN_RE = re.compile(r"@([A-Z][A-Z0-9_]+)@")
_TEMPLATE_VAR_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
_GENERATED_HINT_RE = re.compile(r"(?i)\b(auto[- ]?generated|generated by|do not edit)\b")

#: placeholder value list (password = changeme is not
#: judged a real password)
_PLACEHOLDER_VALUES = frozenset(
    (
        "changeme",
        "change_me",
        "change-me",
        "placeholder",
        "your_password",
        "yourpassword",
    )
)

# ============================================
# sensitive-type table (lowercase key matching)
# ============================================

_SENSITIVE_KEY_TYPES: Dict[str, str] = {
    "password": "password",
    "passphrase": "password",
    "input_password": "password",
    "user": "credential_user",
    "username": "credential_user",
    "ssl-key": "private_key_path",
    "ssl_key": "private_key_path",
    "tls-key": "private_key_path",
    "private_key": "private_key_path",
    "ssl-cert": "certificate_path",
    "ssl_cert": "certificate_path",
    "ssl-ca": "certificate_path",
    "certificate": "certificate_path",
    "database": "database_path",
    "new_certs_dir": "certificate_dir_path",
    "socket": "socket_path",
}


# ============================================
# profile construction
# ============================================


def build_cnf_tool_profile(
    contract: Dict[str, Any],
    detection: Dict[str, Any],
    file_name: str,
    text: str,
    file_path: str = "",
) -> Optional[Dict[str, Any]]:
    """Build the profile semantic layer (pure disclosure;
    returns None when the gate is not met).

    Guard-hooked at the tail of ``CnfParser.parse()``; this
    function never raises (the caller has its own defensive
    guard), and every semantic addition is additive — the
    contract's loss.reasons and existing keys stay
    untouched.
    """
    profile_name = detection.get("profile")
    if detection.get("resolution_status") != "resolved":
        return None
    if profile_name not in _CNF_SEMANTIC_PROFILES:
        return None

    entries = contract.get("entries") or []
    containers = contract.get("containers") or []
    includes = contract.get("includes") or []
    section_names = {c["path"][-1] for c in containers if len(c.get("path") or []) == 1}

    loss_codes: List[str] = []
    security_findings: List[Dict[str, Any]] = []
    option_groups: List[Dict[str, Any]] = []
    policy_references: List[Dict[str, Any]] = []

    if profile_name in ("mysql_cnf", "mariadb_cnf"):
        option_groups = _apply_option_group_semantics(
            containers,
            profile_name,
        )
        _apply_option_form_semantics(entries)
        _apply_multi_value_semantics(entries)
    else:
        policy_references = _apply_openssl_semantics(
            entries,
            containers,
            section_names,
            loss_codes,
        )

    # include-registry enhancement (single file, zero IO —
    # shared by both profiles)
    _apply_include_semantics(includes, file_name, file_path, loss_codes)

    # variable/section reference graphs (registration only,
    # zero evaluation)
    var_edges, sec_edges = _build_reference_graphs(entries)

    # sensitive-type refinement (shared by both profiles)
    _apply_sensitive_types(entries, security_findings)

    # template tiers + placeholder-sensitive candidates
    file_role = _detect_file_role(file_name, text)
    _flag_placeholder_sensitive(entries, security_findings)

    out: Dict[str, Any] = {
        "profile_version": _PROFILE_VERSION,
        "profile": profile_name,
        "option_groups": option_groups,
        "policy_references": policy_references,
        "include_registry": {
            "count": len(includes),
            "include_order": [
                inc.get("normalized_target", inc.get("raw_target", "")) for inc in includes
            ],
            "merge_semantics": "not_merged_zero_io",
        },
        "variable_reference_graph": {
            "edges": var_edges,
            "edge_count": len(var_edges),
        },
        "section_reference_graph": {
            "edges": sec_edges,
            "edge_count": len(sec_edges),
        },
        # single-file context — cross-file effective_value
        # adjudication belongs to the multi-file merge layer
        "source_chain_status": "single_file_context",
        "file_role": file_role,
    }
    if loss_codes:
        out["loss_codes"] = loss_codes
    if security_findings:
        out["security_findings"] = security_findings
    return out


# ============================================
# option-group semantics
# ============================================


def _apply_option_group_semantics(
    containers: List[Dict[str, Any]],
    profile_name: str,
) -> List[Dict[str, Any]]:
    """Option-group classification (container additive
    group_kind/consumer/node_kind; off-table →
    option_group_candidate, pure disclosure)."""
    table: Dict[str, Dict[str, Any]] = dict(_MYSQL_OPTION_GROUPS)
    if profile_name == "mariadb_cnf":
        table.update(_MARIADB_OPTION_GROUPS)
    records: List[Dict[str, Any]] = []
    for c in containers:
        path = c.get("path") or []
        if len(path) != 1:
            continue
        name = path[-1]
        known = table.get(name.lower())
        if known:
            c["group_kind"] = known["group_kind"]
            c["consumer_candidates"] = list(known["consumer_candidates"])
            c["node_kind"] = "known"
            records.append(
                {
                    "section": name,
                    "group_kind": known["group_kind"],
                    "consumer_candidates": list(
                        known["consumer_candidates"],
                    ),
                    "node_kind": "known",
                }
            )
        else:
            c["group_kind"] = "option_group_candidate"
            c["node_kind"] = "unknown_candidate"
            records.append(
                {
                    "section": name,
                    "group_kind": "option_group_candidate",
                    "node_kind": "unknown_candidate",
                }
            )
    return records


# ============================================
# option_form shapes
# ============================================


def _apply_option_form_semantics(entries: List[Dict[str, Any]]) -> None:
    """Entry additive option_form/canonical/flag_form/
    explicit_value/value_bool_style (skip- stripping is a
    candidate, not a fact)."""
    for e in entries:
        raw_key = str(e.get("raw_key") or "")
        key = str(e.get("key") or "")
        value = e.get("value")
        value_str = str(value) if value is not None else ""
        low = value_str.strip().lower()
        if low in _BOOL_STYLE_WORDS["on_off"]:
            e["value_bool_style"] = "on_off"
        elif low in _BOOL_STYLE_WORDS["yes_no"]:
            e["value_bool_style"] = "yes_no"
        elif low in _BOOL_STYLE_WORDS["numeric"]:
            e["value_bool_style"] = "numeric"
        if e.get("cnf_kind") == "flag_entry":
            e["option_form"] = "flag_without_value"
            if key.startswith(_SKIP_PREFIX):
                e["flag_form"] = "skip_prefix_disable"
                e["canonical_name_candidate"] = key[len(_SKIP_PREFIX) :]
            continue
        m = _LONG_OPTION_RE.match(raw_key)
        if m:
            e["option_form"] = "long_option_kv"
            e["canonical_name_candidate"] = m.group(1)
        elif key.startswith(_SKIP_PREFIX):
            # skip-x=ON shape (a flag with an explicit value)
            e["option_form"] = "flag_with_value"
            e["flag_form"] = "skip_prefix_disable"
            e["canonical_name_candidate"] = key[len(_SKIP_PREFIX) :]
            e["explicit_value"] = value
        else:
            e["option_form"] = "key_value"


# ============================================
# multi-value key candidates
# ============================================


def _apply_multi_value_semantics(entries: List[Dict[str, Any]]) -> None:
    """A hit in the multi-value table →
    value_semantics="multi_value_candidate" + a values
    aggregate over the whole chain (DUPLICATE_KEY is kept,
    never withdrawn)."""
    for e in entries:
        key = str(e.get("key") or "")
        if key.lower() not in _MYSQL_MULTI_VALUE_KEYS:
            continue
        e["value_semantics"] = "multi_value_candidate"
        chain = e.get("override_chain") or []
        if chain:
            values = [str(o.get("value")) for o in chain]
        else:
            v = e.get("value")
            values = [str(v)] if v is not None else []
        e["values"] = values


# ============================================
# OpenSSL semantics
# ============================================


def _apply_openssl_semantics(
    entries: List[Dict[str, Any]],
    containers: List[Dict[str, Any]],
    section_names: set,
    loss_codes: List[str],
) -> List[Dict[str, Any]]:
    """Indexed keys / extension candidates / policy
    references / field confirmation gate (openssl_cnf
    only).

    Returns the policy_references record list; a missing
    policy target records MISSING_SECTION_REFERENCE (in
    the semantic layer's loss_codes channel).
    """
    policy_references: List[Dict[str, Any]] = []
    referenced_policy_sections: set = set()
    for e in entries:
        key = str(e.get("key") or "")
        key_low = key.lower()
        value = e.get("value")
        value_str = str(value).strip() if value is not None else ""
        section = e.get("section")

        # field-context confirmation gate (only a bare-name
        # candidate inside the field table is confirmed)
        if key_low in _OPENSSL_SECTION_VALUE_FIELDS:
            for r in e.get("cnf_references") or []:
                if r.get("node_type") == "section_reference" and r.get("candidate_only"):
                    r["confirmed_by"] = "field_context"

        # indexed keys (DNS.1 → base_key/index/raw_key)
        m = _INDEXED_KEY_RE.match(key)
        if m:
            e["base_key"] = m.group(1)
            e["index"] = int(m.group(2))

        # certificate-extension structured candidates
        # (validated stays False)
        if key_low in _OPENSSL_EXTENSION_KEYS and value_str:
            e["parsed_candidates"] = _parse_extension_candidates(
                key_low,
                value_str,
            )
            e["validated"] = False

        # policy references
        if key_low in _POLICY_REFERENCE_KEYS and value_str:
            resolved = value_str in section_names
            policy_references.append(
                {
                    "source_section": section,
                    "raw_value": value_str,
                    "target_section": value_str,
                    "resolved": resolved,
                }
            )
            if resolved:
                referenced_policy_sections.add(value_str)
            else:
                loss_codes.append("MISSING_SECTION_REFERENCE")

    # policy-rule sections (referenced, or named with a
    # policy prefix)
    policy_sections: set = set()
    for c in containers:
        path = c.get("path") or []
        if len(path) != 1:
            continue
        name = path[-1]
        if name in referenced_policy_sections or name.lower().startswith("policy"):
            c["policy_section"] = True
            policy_sections.add(name)
    for e in entries:
        section = e.get("section")
        if section not in policy_sections:
            continue
        value = e.get("value")
        value_str = str(value).strip().lower() if value is not None else ""
        if value_str in _POLICY_RULE_KINDS:
            e["rule_kind"] = value_str
        elif value_str:
            e["rule_value_candidate"] = value_str

    # indexed-key aggregation (container additive
    # indexed_keys — grouped by section)
    per_section_indexed: Dict[str, Dict[str, List[int]]] = {}
    for e in entries:
        base, idx = e.get("base_key"), e.get("index")
        section = e.get("section")
        if base is None or idx is None or section is None:
            continue
        per_section_indexed.setdefault(section, {}).setdefault(
            base,
            [],
        ).append(idx)
    for c in containers:
        path = c.get("path") or []
        if len(path) != 1:
            continue
        name = path[-1]
        own = per_section_indexed.get(name)
        if own:
            c["indexed_keys"] = {base: sorted(idxs) for base, idxs in own.items()}
    return policy_references


def _parse_extension_candidates(
    key_low: str,
    value: str,
) -> List[Dict[str, Any]]:
    """Structured candidates for an extension value (comma
    split; SAN prefix shape → type/value).

    validated is always set False by the caller — statically
    never judging issuance outcomes.

    Deepening: critical markers / basicConstraints
    constraint pairs (CA/pathlen) / the keyUsage bit-name
    table / the extendedKeyUsage name table + the OID
    shape. Candidate dicts gain additive keys (the token key
    is always present); SAN prefix shapes keep the plain
    {type, value} unchanged. Off-table tokens get the
    neutral kind "token" (candidates never assert
    invalidity).
    """
    candidates: List[Dict[str, Any]] = []
    tokens = [t.strip() for t in value.split(",") if t.strip()]
    if key_low == "subjectaltname":
        for tok in tokens:
            m = _SAN_PREFIX_RE.match(tok)
            if m:
                candidates.append({"type": m.group(1).upper(), "value": m.group(2)})
            else:
                candidates.append({"token": tok})
        return candidates
    for tok in tokens:
        low = tok.lower()
        if low == "critical":
            candidates.append({"token": tok, "kind": "critical_marker"})
        elif key_low == "basicconstraints":
            m = _CONSTRAINT_RE.match(tok)
            if m:
                candidates.append(
                    {
                        "token": tok,
                        "kind": "constraint",
                        "name": m.group(1).upper(),
                        "value": m.group(2),
                    }
                )
            else:
                candidates.append({"token": tok, "kind": "token"})
        elif key_low == "keyusage":
            candidates.append(
                {
                    "token": tok,
                    "kind": ("known_usage_bit" if low in _KEY_USAGE_BITS else "token"),
                }
            )
        elif key_low == "extendedkeyusage":
            if low in _EXT_KEY_USAGE_NAMES:
                kind = "known_usage_bit"
            elif _OID_RE.match(tok):
                kind = "oid"
            else:
                kind = "token"
            candidates.append({"token": tok, "kind": kind})
        else:
            candidates.append({"token": tok})
    return candidates


# ============================================
# include-registry enhancement (single file, zero IO)
# ============================================


def _apply_include_semantics(
    includes: List[Dict[str, Any]],
    file_name: str,
    file_path: str,
    loss_codes: List[str],
) -> None:
    """include_order/normalized_target/duplicate_of_order/
    self_reference.

    DUPLICATE_INCLUDE / INCLUDE_CYCLE follow the conf
    precedent (existing codes, in the semantic layer's
    loss_codes channel); resolved_files is never read.
    """
    seen: Dict[str, int] = {}
    self_names = {file_name}
    if file_path:
        self_names.add(Path(file_path).name)
    for idx, inc in enumerate(includes):
        inc["include_order"] = idx
        raw_target = str(inc.get("raw_target") or "")
        # pure-string normalization: quote/whitespace
        # stripping; $VAR is registered, never expanded
        norm = raw_target.strip().strip("'\"").strip()
        inc["normalized_target"] = norm
        if norm in seen:
            inc["duplicate_of_order"] = seen[norm]
            loss_codes.append("DUPLICATE_INCLUDE")
        else:
            seen[norm] = idx
        if norm and norm in self_names:
            inc["self_reference"] = True
            loss_codes.append("INCLUDE_CYCLE")


# ============================================
# reference graphs
# ============================================


def _build_reference_graphs(
    entries: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Variable/section reference-graph edges (aggregated
    from cnf_references).

    Four-way classification: section_variable /
    environment_variable / section_reference /
    literal_dollar_expression (no legal name after the $ —
    INVALID_VARIABLE_REFERENCE is already coded by the
    syntax layer; the graph only registers it).
    """
    var_edges: List[Dict[str, Any]] = []
    sec_edges: List[Dict[str, Any]] = []
    for e in entries:
        source = ".".join(str(p) for p in (e.get("path") or []))
        for r in e.get("cnf_references") or []:
            node = r.get("node_type")
            if node == "variable_reference":
                rtype = r.get("reference_type")
                if rtype == "environment":
                    edge_type = "environment_variable"
                else:
                    edge_type = "section_variable"
                var_edges.append(
                    {
                        "from": source,
                        "to": r.get("variable"),
                        "reference_type": edge_type,
                        "resolution_status": r.get("resolution_status"),
                    }
                )
            elif node == "section_reference":
                sec_edges.append(
                    {
                        "from": source,
                        "to": r.get("target_section"),
                        "reference_type": "section_reference",
                        "resolution_status": r.get("resolution_status"),
                    }
                )
        # literal $ shape (no legal name — registered in the
        # graph without re-coding)
        value = e.get("value")
        if isinstance(value, str) and "$" in value:
            for _m in _LITERAL_DOLLAR_RE.finditer(value):
                var_edges.append(
                    {
                        "from": source,
                        "to": "literal:$",
                        "reference_type": "literal_dollar_expression",
                        "resolution_status": "not_resolved",
                    }
                )
    return var_edges, sec_edges


# ============================================
# template tiers + placeholder sensitivity
# ============================================


def _detect_file_role(file_name: str, text: str) -> Dict[str, Any]:
    """file_role tiers (zero-IO string judging; density
    threshold 3 heuristic)."""
    lower = (file_name or "").lower()
    for suf in _EXAMPLE_SUFFIXES:
        if lower.endswith(suf):
            return {
                "file_role": "example_config",
                "evidence": f"filename_suffix:{suf}",
            }
    for suf in _TEMPLATE_SUFFIXES:
        if lower.endswith(suf):
            return {
                "file_role": "template_config",
                "evidence": f"filename_suffix:{suf}",
            }
    if _TOKEN_RE.search(text or ""):
        return {"file_role": "template_config", "evidence": "token_pattern"}
    if len(_TEMPLATE_VAR_RE.findall(text or "")) >= 3:
        return {
            "file_role": "template_config",
            "evidence": "variable_density",
        }
    m = _GENERATED_HINT_RE.search((text or "")[:2000])
    if m:
        return {
            "file_role": "generated_config_candidate",
            "evidence": f"header_hint:{m.group(0).lower()}",
        }
    return {"file_role": "actual_config", "evidence": "default"}


def _flag_placeholder_sensitive(
    entries: List[Dict[str, Any]],
    security_findings: List[Dict[str, Any]],
) -> None:
    """Placeholder-sensitive candidates (changeme etc. /
    unreplaced ${VAR} — a finding, not a loss).

    Reuses the existing SENSITIVE_VALUE_DETECTED code (zero
    new codes) plus a placeholder marker.
    """
    for e in entries:
        key_low = str(e.get("key") or "").lower()
        if key_low not in _SENSITIVE_KEY_TYPES:
            continue
        value = e.get("value")
        value_str = str(value).strip() if value is not None else ""
        if not value_str:
            continue
        is_placeholder = value_str.lower() in _PLACEHOLDER_VALUES or "${" in value_str
        if not is_placeholder:
            continue
        e["placeholder_sensitive_candidate"] = True
        security_findings.append(
            {
                "code": "SENSITIVE_VALUE_DETECTED",
                "key": e.get("key"),
                "masked": "****",  # never store the value (family caliber)
                "line_start": e.get("line_start"),
                "path": list(e.get("path") or []),
                "placeholder": True,
            }
        )


# ============================================
# sensitive-type refinement
# ============================================


def _apply_sensitive_types(
    entries: List[Dict[str, Any]],
    security_findings: List[Dict[str, Any]],
) -> None:
    """Sensitive key hit → entry additive
    sensitive_type/value_present/redaction_applied (never
    store the value — masking lives only in findings)."""
    for e in entries:
        key_low = str(e.get("key") or "").lower()
        stype = _SENSITIVE_KEY_TYPES.get(key_low)
        if not stype:
            continue
        e["sensitive_type"] = stype
        value = e.get("value")
        e["value_present"] = bool(value is not None and str(value).strip())
        e["redaction_applied"] = False


# ============================================
# Markdown summary (an auxiliary view — never a
# replacement for the contract)
# ============================================


def render_cnf_summary(
    profile: Dict[str, Any],
    contract: Dict[str, Any],
) -> str:
    """Render cnf_summary_md (the Markdown output channel —
    an auxiliary view)."""
    lines: List[str] = []
    lines.append(f"# .cnf Profile Summary: {profile.get('profile', 'unknown')}")
    lines.append("")
    lines.append(
        "> 辅助视图（profile_version "
        f"{profile.get('profile_version', _PROFILE_VERSION)}）："
        "Markdown 不能替代 option group 语义、编号键、证书扩展候选、"
        "引用图、include 展开语义与原始注释。"
    )
    lines.append("")

    groups = profile.get("option_groups") or []
    if groups:
        lines.append("## Option Groups")
        lines.append("")
        lines.append("| section | group_kind | consumers | node_kind |")
        lines.append("| --- | --- | --- | --- |")
        for g in groups:
            consumers = ", ".join(g.get("consumer_candidates") or [])
            lines.append(
                f"| {g.get('section')} | {g.get('group_kind')} "
                f"| {consumers or '-'} | {g.get('node_kind')} |"
            )
        lines.append("")

    policy_refs = profile.get("policy_references") or []
    if policy_refs:
        lines.append("## CA Policy References")
        lines.append("")
        for pr in policy_refs:
            status = "resolved" if pr.get("resolved") else "unresolved"
            lines.append(
                f"- `{pr.get('source_section')}.policy` → `{pr.get('target_section')}` ({status})"
            )
        lines.append("")

    includes = contract.get("includes") or []
    if includes:
        lines.append("## Includes (registered, zero-IO)")
        lines.append("")
        for inc in includes:
            lines.append(
                f"- `!{inc.get('directive')} "
                f"{inc.get('normalized_target', inc.get('raw_target'))}`"
                f" (order {inc.get('include_order')}, "
                f"{inc.get('resolution_status')})"
            )
        lines.append("")

    var_graph = profile.get("variable_reference_graph") or {}
    sec_graph = profile.get("section_reference_graph") or []
    if var_graph.get("edge_count") or (isinstance(sec_graph, dict) and sec_graph.get("edge_count")):
        lines.append("## Reference Graph (identify-only)")
        lines.append("")
        for edge in (var_graph.get("edges") or [])[:40]:
            lines.append(
                f"- `{edge.get('from')}` --{edge.get('reference_type')}"
                f"--> `{edge.get('to')}`"
                f" ({edge.get('resolution_status')})"
            )
        sec_edges = (sec_graph.get("edges") if isinstance(sec_graph, dict) else None) or []
        for edge in sec_edges[:40]:
            lines.append(
                f"- `{edge.get('from')}` --{edge.get('reference_type')}"
                f"--> `{edge.get('to')}`"
                f" ({edge.get('resolution_status')})"
            )
        lines.append("")

    findings = profile.get("security_findings") or []
    if findings:
        lines.append("## Security Candidates (masked)")
        lines.append("")
        for f in findings:
            extra = " (placeholder)" if f.get("placeholder") else ""
            lines.append(
                f"- `{f.get('code')}` @ "
                f"`{'.'.join(str(p) for p in (f.get('path') or []))}`"
                f" = {f.get('masked', '****')}{extra}"
            )
        lines.append("")

    role = profile.get("file_role") or {}
    if role:
        lines.append("## File Role")
        lines.append("")
        lines.append(f"- {role.get('file_role')} (evidence: {role.get('evidence')})")
        lines.append("")

    return "\n".join(lines)
