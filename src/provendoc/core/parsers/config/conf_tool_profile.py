# -*- coding: utf-8 -*-
"""Tool-profile semantic layer for 8 config tools (nginx, Apache,
sshd, sysctl, resolv.conf, BIND, redis, OpenVPN).

Runs as an independent semantic pass after the generic conf parser:
input = the flat contract + detection result + file identity + raw
text, output = ``metadata["conf_tool_profile"]`` (disclosure-only
structure).

Build gate: the profile is built only when
``conf_profile.resolution_status == "resolved"`` and the tool is one
of the 8 supported ones. uncertain / generic / handed_off files get
no tool profile — when the tool is unknown its semantics are never
guessed, and the absent semantic layer is itself the honest
disclosure.

Non-negotiables:
- No execution, no IO: include targets are never read, no DNS
  lookups, no network, no credential files; blocked objects get a
  code and the raw text is preserved.
- Additive zero drift: existing contract keys, codes and routing are
  never touched; semantic-layer codes land in
  ``conf_tool_profile["loss_codes"]`` (never in contract
  loss.reasons); new entry/container fields
  (match_modifier / inherited_from / value_kinds) are additive only.
- Honest boundaries: effective_value / security_finding / inheritance
  sources are always candidates with context_dependent set; the layer
  never judges "secure/insecure" or "configuration is valid".
- Discovery is not loss: exception-class codes (DUPLICATE_INCLUDE /
  INCLUDE_CYCLE / APACHE_TAG_MISMATCH) go to loss_codes; pure
  discovery codes (CREDENTIAL_IN_URL /
  PRIVATE_RESOURCE_PATH_CANDIDATE / SENSITIVE_DIRECTIVE_CANDIDATE) go
  only to security_findings / sensitive structures (a finding never
  dilutes loss semantics).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.parsers.config import contracts as cc

# ============================================
# Tool profile semantic table; unknown blocks/directives are pure
# disclosure, never a loss code.
# ============================================

_TOOL_PROFILES = frozenset(
    (
        "nginx",
        "apache",
        "sshd",
        "sysctl",
        "resolv",
        "bind",
        "redis",
        "openvpn",
    )
)

# known tables are registered lowercase; the tree/records keep original
# casing, matching uses ``name.lower()`` (sshd/apache directive names are
# case-insensitive).
_KNOWN_BLOCKS: Dict[str, frozenset] = {
    "nginx": frozenset(
        (
            "events",
            "http",
            "server",
            "location",
            "upstream",
            "map",
            "if",
            "limit_req_zone",
            "limit_conn_zone",
            "types",
        )
    ),
    "apache": frozenset(
        (
            "virtualhost",
            "directory",
            "location",
            "ifmodule",
            "ifdefine",
            "files",
            "filesmatch",
            "directorymatch",
            "locationmatch",
            "proxy",
            "ifversion",
        )
    ),
    "sshd": frozenset(("match",)),
    "bind": frozenset(
        (
            "options",
            "zone",
            "acl",
            "view",
            "controls",
            "key",
            "server",
            "logging",
            "masters",
        )
    ),
    "sysctl": frozenset(),
    "resolv": frozenset(),
    "redis": frozenset(),
    "openvpn": frozenset(),
}

_KNOWN_DIRECTIVES: Dict[str, frozenset] = {
    "nginx": frozenset(
        (
            "worker_processes",
            "include",
            "listen",
            "server_name",
            "root",
            "alias",
            "proxy_pass",
            "rewrite",
            "access_log",
            "error_log",
            "ssl_certificate",
            "ssl_certificate_key",
            "ssl_protocols",
            "ssl_ciphers",
            "auth_basic",
            "auth_basic_user_file",
            "allow",
            "deny",
            "return",
            "try_files",
            "fastcgi_pass",
            "uwsgi_pass",
            "proxy_set_header",
            "proxy_redirect",
            "index",
            "sendfile",
            "keepalive_timeout",
            "client_max_body_size",
            "limit_req",
            "limit_conn",
            "error_page",
            "gzip",
            "expires",
            "add_header",
            "set",
            "server_tokens",
            "pid",
            "user",
            "worker_connections",
        )
    ),
    "apache": frozenset(
        (
            "servername",
            "documentroot",
            "loadmodule",
            "include",
            "includeoptional",
            "proxypass",
            "proxypassreverse",
            "rewriterule",
            "rewriteengine",
            "require",
            "allowoverride",
            "options",
            "directoryindex",
            "errorlog",
            "customlog",
            "sslengine",
            "sslcertificatefile",
            "sslcertificatekeyfile",
            "listen",
            "user",
            "group",
            "serveradmin",
            "addtype",
            "sethandler",
            "serverroot",
            "pidfile",
        )
    ),
    "sshd": frozenset(
        (
            "port",
            "permitrootlogin",
            "passwordauthentication",
            "pubkeyauthentication",
            "allowusers",
            "denyusers",
            "allowgroups",
            "denygroups",
            "forcecommand",
            "authorizedkeyscommand",
            "authorizedkeysfile",
            "subsystem",
            "include",
            "allowtcpforwarding",
            "x11forwarding",
            "usedns",
            "loglevel",
            "addressfamily",
            "listenaddress",
            "hostkey",
            "kbdinteractiveauthentication",
            "authenticationmethods",
            "chrootdirectory",
            "banner",
            "maxauthtries",
            "permitemptypasswords",
            "match",
        )
    ),
    "bind": frozenset(
        (
            "directory",
            "recursion",
            "version",
            "listen-on",
            "allow-query",
            "allow-recursion",
            "forwarders",
            "type",
            "file",
            "masters",
            "include",
            "notify",
            "also-notify",
            "allow-transfer",
            "secret",
            "algorithm",
            "statistics",
            "masterfile-format",
            "inline-signing",
            "dnssec-validation",
            "auto-dnssec",
        )
    ),
    "sysctl": frozenset(),  # dotted key 前缀判定（_SYSCTL_PREFIX）
    "resolv": frozenset(
        (
            "nameserver",
            "search",
            "domain",
            "options",
            "sortlist",
            "rotate",
        )
    ),
    "redis": frozenset(
        (
            "bind",
            "port",
            "protected-mode",
            "requirepass",
            "masterauth",
            "dir",
            "dbfilename",
            "appendonly",
            "appendfilename",
            "save",
            "maxmemory",
            "maxmemory-policy",
            "timeout",
            "tcp-keepalive",
            "daemonize",
            "pidfile",
            "logfile",
            "databases",
            "replicaof",
            "loadmodule",
            "aclfile",
            "unixsocket",
            "tls-port",
            "tls-cert-file",
            "tls-key-file",
            "tls-ca-cert-file",
        )
    ),
    "openvpn": frozenset(
        (
            "client",
            "dev",
            "proto",
            "remote",
            "ca",
            "cert",
            "key",
            "dh",
            "tls-auth",
            "auth-user-pass",
            "auth-user-pass-verify",
            "script-security",
            "up",
            "down",
            "plugin",
            "cipher",
            "data-ciphers",
            "comp-lzo",
            "keepalive",
            "persist-key",
            "persist-tun",
            "log",
            "log-append",
            "verb",
            "user",
            "group",
            "tls-client",
            "tls-server",
            "server",
            "ifconfig",
            "route",
            "management",
        )
    ),
}

# sysctl known prefixes (first segment of dotted keys)
_SYSCTL_PREFIX = re.compile(
    r"^(?:net|fs|kernel|vm|user|abi|debug|dev)\.",
)

# Directives whose value is itself a sensitive object (secrets,
# credentials); the value is never stored.
_SENSITIVE_DIRECTIVES: Dict[str, frozenset] = {
    "redis": frozenset(("requirepass", "masterauth")),
    "openvpn": frozenset(("auth-user-pass", "key", "secret")),
    "bind": frozenset(("secret",)),
    "sshd": frozenset(
        (
            "authorizedkeyscommand",
            "forcecommand",
            "authorizedkeysfile",
        )
    ),
    "nginx": frozenset(),
    "apache": frozenset(),
    "sysctl": frozenset(),
    "resolv": frozenset(),
}

# Inheritance policy declaration (single-file candidate semantics;
# cross-file merging is a separate layer).
_INHERITANCE_POLICY: Dict[str, str] = {
    "nginx": "block_inheritance",
    "bind": "block_inheritance",
    "apache": "container_inheritance_sibling_later_wins",
    "sshd": "first_obtained_wins",
    "sysctl": "flat_override",
    "redis": "flat_override",
    "resolv": "multi_value_append_candidate",
    "openvpn": "multi_value_append_candidate",
}
_INHERITANCE_SCOPED = frozenset(
    (
        "block_inheritance",
        "container_inheritance_sibling_later_wins",
    )
)

# sshd Match condition keywords
_MATCH_KEYWORDS = frozenset(
    (
        "User",
        "Group",
        "Address",
        "LocalAddress",
        "Host",
        "LocalPort",
        "RDomain",
    )
)

# nginx location modifier classification. Besides tree-level evidence
# (modifier as the first label), a text-level scan covers the form
# ``location = /exact {`` which the generic layer recovers as a
# key/value pair (``= `` triggers key_value splitting); the tree is
# never rewritten, so the ``=`` modifier survives only via the
# _LOCATION_LINE_RE text evidence.
_LOCATION_MODIFIERS = {
    "=": "exact",
    "^~": "prefix_no_regex",
    "~": "regex",
    "~*": "case_insensitive_regex",
}
_LOCATION_LINE_RE = re.compile(
    r"^[ \t]*location[ \t]+(=|~\*|~|\^~)?[ \t]*([^\s{;]+)",
    re.M,
)

# Apache conditional containers (IfModule/IfDefine → condition
# candidates; matching is lowercase)
_APACHE_CONDITION_CONTAINERS = frozenset(
    (
        "ifmodule",
        "ifdefine",
        "ifversion",
    )
)

# Snippet file patterns (string-only check, zero IO)
_FRAGMENT_DIR_PATTERNS = (
    "conf.d",
    "sshd_config.d",
    "sites-enabled",
    "sites-available",
    "conf-available",
    "conf-enabled",
)
_EXPECTED_PARENT_SCOPE = {
    "sites-enabled": ["http"],
    "sites-available": ["http"],
    "sshd_config.d": [],
    # conf.d / conf-available parent scope varies by tool and snippet
    # content -> None
}

# Value classification (ten candidate kinds; raw is preserved,
# normalized is pure string cleanup — no expansion)
_CRED_URL_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@",
)
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_CERT_EXTS = (".pem", ".crt", ".cer", ".pfx", ".der")
_KEY_EXTS = (".key",)
_GLOB_CHARS = ("*", "?", "[")

# Variable references ($VAR / ${VAR}: registered, never expanded)
_VAR_REF_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")

_SENSITIVE_MASK = "****"

# Apache open/close tag pairing (text-level evidence scan, zero IO)
_ANGLE_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][\w\-]*)")


def _unquote(raw: str) -> str:
    """Strip matching surrounding quotes (tree labels are raw tokens)."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


# ============================================
# Main pass
# ============================================


def build_conf_tool_profile(
    root: Optional[Dict[str, Any]],
    contract: Optional[Dict[str, Any]],
    detection: Optional[Dict[str, Any]],
    file_name: Optional[str],
    text: str,
    file_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build ``metadata["conf_tool_profile"]``.

    Enriches contract entries/containers additively (new keys only);
    existing keys are untouched. Semantic-layer codes land in the
    profile's own loss_codes, leaving contract loss.reasons
    zero-drift. Build gate: see module docstring.
    """
    if not isinstance(detection, dict):
        return None
    tool = detection.get("profile")
    if detection.get("resolution_status") != "resolved":
        return None
    if tool not in _TOOL_PROFILES:
        return None
    if not isinstance(contract, dict):
        return None

    loss: Dict[str, int] = {}

    def hit(code: str) -> None:
        loss[code] = loss.get(code, 0) + 1

    entries = contract.get("entries") or []
    containers = contract.get("containers") or []

    # additive container anchor (location modifiers attach to containers)
    container_by_path: Dict[Tuple[str, ...], Dict[str, Any]] = {
        tuple(c.get("path") or []): c for c in containers
    }

    blocks: List[Dict[str, Any]] = []
    conditional_scopes: List[Dict[str, Any]] = []

    # ---- block walk (classification + tool-specific recovery) ----
    def walk(node: Dict[str, Any], prefix: List[str]) -> None:
        for child in node.get("children") or []:
            kind = child.get("kind")
            if kind not in ("block_open", "angle_open"):
                continue
            name = child.get("name") or ""
            labels = [_unquote(str(lab)) for lab in child.get("labels") or []]
            if kind == "angle_open":
                path = list(prefix) + [name]
            else:
                path = list(prefix) + [name] + labels
            node_kind = (
                "known"
                if name.lower() in _KNOWN_BLOCKS.get(tool, frozenset())
                else "unknown_block_candidate"
            )
            rec: Dict[str, Any] = {
                "node_kind": node_kind,
                "block_type": name,
                "labels": labels,
                "path": path,
                "line_start": child.get("line_start"),
                "line_end": child.get("line_end"),
            }
            # nginx location modifier (routing semantics preserved)
            if tool == "nginx" and name == "location" and labels:
                modifier = "prefix"
                first = labels[0]
                if first in _LOCATION_MODIFIERS:
                    modifier = _LOCATION_MODIFIERS[first]
                    rec["location_pattern"] = " ".join(labels[1:]) or None
                else:
                    rec["location_pattern"] = " ".join(labels)
                rec["match_modifier"] = modifier
                cont = container_by_path.get(tuple(path))
                if isinstance(cont, dict):
                    cont["match_modifier"] = modifier
            # Apache container modeling
            if tool == "apache" and kind == "angle_open":
                args = child.get("args_raw") or ""
                rec["container_type"] = name
                rec["arguments"] = args
                rec["closing_tag"] = f"</{name}>"
                if name.lower() in _APACHE_CONDITION_CONTAINERS:
                    rec["condition"] = {
                        "kind": "if_condition_candidate",
                        "expression": args,
                    }
            # sshd Match conditional scope (block form)
            if tool == "sshd" and name.lower() == "match":
                cond = _parse_match_conditions(" ".join(labels))
                if cond is not None:
                    rec["conditional_scope"] = True
                    conditional_scopes.append(
                        {
                            "path": path,
                            "conditions": cond,
                            "line_start": child.get("line_start"),
                            "block_form": True,
                        }
                    )
            if child.get("unclosed"):
                rec["unclosed"] = True
            blocks.append(rec)
            walk(child, path)

    if isinstance(root, dict):
        walk(root, [])

    # ---- sshd Match (directive form, no brace block): the tree is
    # never rewritten; following directives in the same scope are
    # recorded as candidate members of the Match section ----
    if tool == "sshd":
        for entry in entries:
            name = (entry.get("key") or "").lower()
            if name != "match":
                continue
            raw_args = entry.get("raw_value") or ""
            cond = _parse_match_conditions(raw_args)
            if cond is None:
                continue
            scope_prefix = list(entry.get("path") or [])[:-1]
            line = entry.get("line_start") or 0
            applies = [
                e.get("key")
                for e in entries
                if list(e.get("path") or [])[:-1] == scope_prefix
                and (e.get("line_start") or 0) > line
                and (e.get("key") or "").lower() != "match"
            ]
            conditional_scopes.append(
                {
                    "path": entry.get("path") or [],
                    "conditions": cond,
                    "line_start": entry.get("line_start"),
                    "block_form": False,
                    "following_directives_candidate": applies,
                    "disclosure": (
                        "sshd Match 归属为运行时条件语义；P0 树按语句"
                        "原样恢复（R6 不改树），此处仅披露候选归属"
                    ),
                }
            )

    # ---- directive classification + explicit sensitive set ----
    directives: List[Dict[str, Any]] = []
    sensitive_explicit: List[Dict[str, Any]] = []
    known_set = _KNOWN_DIRECTIVES.get(tool, frozenset())
    sensitive_set = _SENSITIVE_DIRECTIVES.get(tool, frozenset())
    for entry in entries:
        name = entry.get("key") or ""
        name_l = name.lower()
        if tool == "sysctl":
            is_known = bool(_SYSCTL_PREFIX.match(name_l))
        else:
            is_known = name_l in known_set
        directives.append(
            {
                "node_kind": "known" if is_known else "unknown_candidate",
                "name": name,
                "path": entry.get("path") or [],
                "line_start": entry.get("line_start"),
            }
        )
        if name_l in sensitive_set:
            sensitive_explicit.append(
                {
                    "type": "sensitive_directive_candidate",
                    "path": entry.get("path") or [],
                    "line_start": entry.get("line_start"),
                    "masked": _SENSITIVE_MASK,
                }
            )

    # ---- include registry enrichment (single file, zero IO) ----
    include_records: List[Dict[str, Any]] = []
    seen_targets: Dict[str, List[int]] = {}
    base_name = (file_name or "").lower()
    for idx, inc in enumerate(contract.get("includes") or []):
        raw_target = inc.get("raw_target") or ""
        norm = _normalize_target(raw_target)
        rec = {
            "order": idx,
            "raw_target": raw_target,
            "normalized_target": norm,
            "directive": inc.get("directive"),
            "line_start": inc.get("line_start"),
            "target_type": inc.get("target_type"),
            "resolution_status": "not_resolved",
            "resolved_files": "not_resolved_zero_io",
            "include_source_order": idx,
        }
        var_refs = _variable_references(norm)
        if var_refs:
            rec["variable_references"] = var_refs
            rec["resolved_path"] = None
        if norm:
            occurrences = seen_targets.setdefault(norm, [])
            occurrences.append(idx)
            if len(occurrences) > 1:
                hit("DUPLICATE_INCLUDE")
                rec["duplicate_of_order"] = seen_targets[norm][0]
            # decidable without IO: self-referential include (same file name)
            if norm.rstrip("/").rsplit("/", 1)[-1].lower() == base_name:
                hit("INCLUDE_CYCLE")
                rec["self_reference"] = True
        include_records.append(rec)

    # ---- value-classification candidates + security findings ----
    path_url_classification: List[Dict[str, Any]] = []
    security_findings: List[Dict[str, Any]] = []
    for entry in entries:
        value = entry.get("raw_value")
        if not isinstance(value, str) or not value.strip():
            continue
        key = entry.get("key") or ""
        kinds = _classify_value(value, key)
        if not kinds:
            continue
        ref = {
            "path": entry.get("path") or [],
            "line_start": entry.get("line_start"),
        }
        path_url_classification.append(
            {
                "kinds": kinds,
                "raw": value,
                "normalized": _normalize_target(value),
                **ref,
            }
        )
        # additive candidate key (misses leave no key — zero drift)
        entry["value_kinds"] = kinds
        if "credential_in_url" in kinds:
            security_findings.append(
                {
                    "code": "CREDENTIAL_IN_URL",
                    "source_ref": dict(ref),
                }
            )
        if "certificate_path" in kinds or "private_key_path" in kinds:
            security_findings.append(
                {
                    "code": "PRIVATE_RESOURCE_PATH_CANDIDATE",
                    "source_ref": dict(ref),
                }
            )
    # explicit sensitive → findings (discovery class, never a loss code)
    for rec in sensitive_explicit:
        security_findings.append(
            {
                "code": "SENSITIVE_DIRECTIVE_CANDIDATE",
                "source_ref": {
                    "path": rec["path"],
                    "line_start": rec["line_start"],
                },
            }
        )

    # ---- Apache open/close tag pairing (text-level evidence) ----
    if tool == "apache":
        open_counts: Dict[str, int] = {}
        close_counts: Dict[str, int] = {}
        for slash, name in _ANGLE_TAG_RE.findall(text or ""):
            if slash:
                close_counts[name] = close_counts.get(name, 0) + 1
            else:
                open_counts[name] = open_counts.get(name, 0) + 1
        for name, opened in sorted(open_counts.items()):
            if close_counts.get(name, 0) != opened:
                hit("APACHE_TAG_MISMATCH")

    # ---- nginx location text-level evidence scan ----
    # Covers shapes the tree cannot: ``location = /exact {`` is
    # recovered by the generic layer as a key/value pair (``= ``
    # triggers key_value splitting), so the ``=`` (exact) modifier
    # survives only in the text evidence (the tree is never
    # rewritten).
    locations: List[Dict[str, Any]] = []
    if tool == "nginx":
        for m in _LOCATION_LINE_RE.finditer(text or ""):
            modifier_tok = m.group(1) or ""
            modifier = _LOCATION_MODIFIERS.get(modifier_tok, "prefix")
            locations.append(
                {
                    "line_start": text.count("\n", 0, m.start()) + 1,
                    "match_modifier": modifier,
                    "location_pattern": _unquote(m.group(2)),
                }
            )

    # ---- inheritance candidates (scoped policies: same-key
    # ancestor/descendant scope pairs) ----
    inheritance: Dict[str, Any] = {
        "policy": _INHERITANCE_POLICY.get(tool),
        "validated": False,
        "context_dependent": True,
        "scope_tree": _scope_tree(containers),
        "candidates": [],
    }
    if _INHERITANCE_POLICY.get(tool) in _INHERITANCE_SCOPED:
        by_key: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            name = entry.get("key")
            if name:
                by_key.setdefault(name, []).append(entry)
        for name, group in by_key.items():
            if len(group) < 2:
                continue
            for child_e in group:
                child_scope = list(child_e.get("path") or [])[:-1]
                for ancestor_e in group:
                    if ancestor_e is child_e:
                        continue
                    anc_scope = list(ancestor_e.get("path") or [])[:-1]
                    if (
                        len(anc_scope) < len(child_scope)
                        and child_scope[: len(anc_scope)] == anc_scope
                    ):
                        anc_ps = cc.path_str(
                            list(ancestor_e.get("path") or []),
                        )
                        # additive candidate (zero drift: misses leave no key)
                        child_e.setdefault(
                            "inherited_from",
                            {
                                "ancestor_path": anc_ps,
                                "validated": False,
                                "context_dependent": True,
                            },
                        )
                        inheritance["candidates"].append(
                            {
                                "key": name,
                                "ancestor_path": anc_ps,
                                "child_path": cc.path_str(
                                    list(child_e.get("path") or []),
                                ),
                            }
                        )
                        break

    # ---- file_role (main vs snippet, string-only check, zero IO) ----
    file_role = _file_role_of(file_path or file_name or "")

    profile: Dict[str, Any] = {
        "profile_version": "conf_tool_profile_p1",
        "tool": tool,
        "confidence": detection.get("confidence"),
        "blocks": blocks,
        "locations": locations,
        "conditional_scopes": conditional_scopes,
        "directives": directives,
        "includes": include_records,
        "path_url_classification": path_url_classification,
        "security_findings": security_findings,
        "sensitive": {
            "explicit": sensitive_explicit,
            "name_based": "shared_channel_scan_sensitive_entries",
            "schema_sensitive": "not_applicable_p1",
        },
        "inheritance": inheritance,
        "file_role": file_role,
        "disclosures": {
            "static_only_not_validated": True,
            "effective_values_context_dependent": True,
            "security_findings_are_candidates": True,
            "markdown_summary_not_replacement": True,
            "include_not_resolved_zero_io": True,
        },
    }
    if loss:
        profile["loss_codes"] = [f"{code}:{n}" for code, n in sorted(loss.items())]
    return profile


# ============================================
# Helpers (pure functions, zero IO)
# ============================================


def _parse_match_conditions(
    raw_args: str,
) -> Optional[List[List[str]]]:
    """Parse sshd Match conditions into ``[["User", "deploy"], ...]``
    pairs.

    Tokens are consumed in keyword/value pairs; a non-keyword start or
    an odd trailing token yields None (no guessing — honest
    degradation)."""
    tokens = raw_args.split()
    if not tokens:
        return None
    conditions: List[List[str]] = []
    i = 0
    while i < len(tokens):
        kw = tokens[i]
        if kw not in _MATCH_KEYWORDS or i + 1 >= len(tokens):
            return None
        conditions.append([kw, tokens[i + 1]])
        i += 2
    return conditions


def _normalize_target(raw: str) -> str:
    """Pure string normalization: strip quotes and outer whitespace.
    No variable expansion, no resolution."""
    s = (raw or "").strip()
    return _unquote(s)


def _variable_references(target: str) -> List[str]:
    """Collect $VAR / ${VAR} reference names (registered, not expanded)."""
    refs: List[str] = []
    for m in _VAR_REF_RE.finditer(target or ""):
        name = m.group(1) or m.group(2)
        if name and name not in refs:
            refs.append(name)
    return refs


def _classify_value(value: str, key: str) -> List[str]:
    """Ten-kind candidate classification (classify only; no reads,
    no validation)."""
    v = value.strip()
    if not v:
        return []
    kinds: List[str] = []
    v_unq = _unquote(v)
    key_l = (key or "").lower()
    if _CRED_URL_RE.match(v_unq):
        kinds.append("credential_in_url")
    if _URL_RE.match(v_unq):
        if v_unq.lower().startswith("file://"):
            kinds.append("file_url")
        else:
            kinds.append("http_url")
    elif v_unq.startswith(("unix:", "unix://")):
        kinds.append("unix_socket")
    if not kinds:
        if any(c in v_unq for c in _GLOB_CHARS):
            kinds.append("glob_path")
        if v_unq.startswith("/"):
            kinds.append("absolute_path")
        elif "/" in v_unq:
            kinds.append("relative_path")
    low = v_unq.lower()
    if low.endswith(_CERT_EXTS):
        kinds.append("certificate_path")
    if low.endswith(_KEY_EXTS):
        kinds.append("private_key_path")
    if v_unq.startswith("/") and "log" in key_l:
        kinds.append("log_path")
    if v_unq.startswith("/") and "pid" in key_l:
        kinds.append("pid_path")
    # order-preserving dedup
    unique: List[str] = []
    seen: set = set()
    for k in kinds:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _scope_tree(containers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Containers → nested scope tree (pure projection of the
    container list)."""
    roots: List[Dict[str, Any]] = []
    nodes: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for cont in containers:
        path = tuple(cont.get("path") or [])
        node = {
            "name": path[-1] if path else "",
            "path": list(path),
            "line_start": cont.get("line_start"),
            "children": [],
        }
        nodes[path] = node
        parent = path[:-1]
        if parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    return roots


def _file_role_of(path_str: str) -> Dict[str, Any]:
    """Main configuration vs snippet (path pattern match, zero IO)."""
    p = (path_str or "").replace("\\", "/").lower()
    for frag in _FRAGMENT_DIR_PATTERNS:
        if f"/{frag}/" in p:
            expected = _EXPECTED_PARENT_SCOPE.get(frag)
            return {
                "file_role": "included_fragment_candidate",
                "expected_parent_scope": expected,
                "standalone_parse_status": "context_dependent",
                "matched_pattern": frag,
            }
    return {
        "file_role": "main_configuration",
        "expected_parent_scope": None,
        "standalone_parse_status": "standalone",
    }


# ============================================
# Markdown summary (auxiliary view, not a replacement for the .conf)
# ============================================


def render_conf_summary(
    profile: Optional[Dict[str, Any]],
    contract: Optional[Dict[str, Any]],
) -> str:
    """Render conf_summary_md (scope tables + Match section + includes
    + security candidates). Sensitive values are masked (never
    stored).
    """
    if not isinstance(profile, dict) or not isinstance(contract, dict):
        return ""
    entries = contract.get("entries") or []
    if not entries:
        return ""
    tool = profile.get("tool") or "conf"
    lines: List[str] = []
    lines.append(f"# Conf Profile Summary: {tool}")
    lines.append("")
    lines.append(
        "> 辅助视图（Markdown 不能替代作用域/指令重复/include/"
        "变量/命令/plugin/版本约束/原始注释——规格 §四原文口径）。"
    )
    lines.append("")

    def entry_value(entry: Dict[str, Any]) -> str:
        key = entry.get("key") or ""
        if cc.is_sensitive_key(key) or key.lower() in (
            _SENSITIVE_DIRECTIVES.get(tool, frozenset())
        ):
            return _SENSITIVE_MASK
        raw = entry.get("raw_value")
        if isinstance(raw, str):
            return raw if len(raw) <= 60 else raw[:57] + "…"
        return ""

    scopes = list(contract.get("containers") or [])
    for cont in scopes:
        scope_path = list(cont.get("path") or [])
        members = [e for e in entries if list(e.get("path") or [])[:-1] == scope_path]
        if not members:
            continue
        lines.append(f"## Scope: `{cc.path_str(scope_path)}`")
        lines.append("")
        lines.append("| Directive | Value |")
        lines.append("|---|---|")
        for e in members:
            lines.append(f"| `{e.get('key') or ''}` | `{entry_value(e)}` |")
        lines.append("")
    top = [e for e in entries if len(e.get("path") or []) == 1]
    if top:
        lines.append("## Scope: `(top-level)`")
        lines.append("")
        lines.append("| Directive | Value |")
        lines.append("|---|---|")
        for e in top:
            lines.append(f"| `{e.get('key') or ''}` | `{entry_value(e)}` |")
        lines.append("")

    cond_scopes = profile.get("conditional_scopes") or []
    if cond_scopes:
        lines.append("### Conditional Scopes")
        lines.append("")
        for cs in cond_scopes:
            conds = cs.get("conditions") or []
            cond_txt = ", ".join(f"{c[0]} `{c[1]}`" for c in conds)
            lines.append(f"- Match: {cond_txt}（候选归属，运行时条件语义）")
        lines.append("")

    includes = profile.get("includes") or []
    if includes:
        lines.append("### Includes")
        lines.append("")
        for inc in includes:
            lines.append(
                f"- `{inc.get('raw_target') or ''}`（order {inc.get('order')}，not_resolved）"
            )
        lines.append("")

    findings = profile.get("security_findings") or []
    if findings:
        lines.append("### Security Findings (candidates)")
        lines.append("")
        lines.append("| Code | Path |")
        lines.append("|---|---|")
        for f in findings:
            ref = f.get("source_ref") or {}
            lines.append(f"| {f.get('code')} | `{cc.path_str(ref.get('path') or [])}` |")
        lines.append("")
    return "\n".join(lines)
