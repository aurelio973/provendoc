# -*- coding: utf-8 -*-
# ============================================================
# Structured Config Domain
# `.config` .NET Domain Profile semantic pass (P1)
# ============================================================
"""XML ``.config`` .NET Domain Profile semantic layer.

Once XML ``.config`` files are handed over to XmlParser by the gate,
there is syntax but no semantics — this pass is hooked at the end of
``DotConfigParser.parse()`` and builds a .NET Domain Profile with
pure-disclosure semantics for XML ``.config`` whose root is
``<configuration>``:

- a section semantics table (known / unknown_candidate / third-party
  classification);
- order-preserving add/remove/clear operation modeling for
  appSettings/connectionStrings (not folded into a final map);
- connectionString candidate splitting + raw preservation;
- configSource/file external-reference isolation + self-reference
  cycles;
- configSections registration registry;
- a static XDT transform AST (precise namespace-URI gate);
- protected configuration recognition;
- location scope / IIS-ASP.NET risk candidates;
- refined sensitive aggregation (never store value);
- layered status projection.

Red lines: zero execution and zero IO (no reading external files, no
loading assemblies, no decryption, no applying transforms); purely
additive zero drift (the contract's entries/containers/loss.reasons
are untouched; semantic structures all live under ``metadata
["dotnet_config_profile"]``; semantic codes land in
``profile["loss_codes"]``, never in the contract's loss.reasons);
external references are identify-only; candidates stay honest and
never masquerade as verdicts; the generic layers carry no profile
semantics; findings go through disclosure without diluting loss.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.parsers.config import contracts as cc

_log = logging.getLogger(__name__)

PROFILE_VERSION = "dotnet_config_profile_p4"

# ------------------------------------------------------------
# XDT namespace-URI precise gate (the prefix may be anything;
# same-named attributes without a URI declaration never match,
# preventing misjudgment)
# ------------------------------------------------------------
_XDT_URI = "http://schemas.microsoft.com/XML-Document-Transform"

# ------------------------------------------------------------
# section semantics table
# ------------------------------------------------------------
_KNOWN_SECTIONS: Dict[str, Dict[str, Any]] = {
    "appSettings": {"domain": "dotnet"},
    "connectionStrings": {"domain": "dotnet"},
    "configSections": {"domain": "dotnet"},
    "configProtectedData": {"domain": "dotnet"},
    "system.web": {"domain": "iis"},
    "system.webServer": {"domain": "iis"},
    "location": {"domain": "iis"},
    "runtime": {"domain": "dotnet"},
    "startup": {"domain": "dotnet"},
    "applicationSettings": {"domain": "dotnet"},
    "userSettings": {"domain": "dotnet"},
    "system.serviceModel": {"domain": "wcf"},
    "nlog": {"domain": "logging"},
    "log4net": {"domain": "logging"},
}

# third-party real-name sections (four-way classification:
# syntactically valid / .NET candidate / third-party recognized /
# schema unknown — known_name_no_schema, no semantics built)
_THIRD_PARTY_SECTIONS = {"nlog", "log4net", "system.serviceModel"}

# protected configuration provider vocabulary
_PROTECTED_PROVIDERS = (
    "DataProtectionConfigurationProvider",
    "RsaProtectedConfigurationProvider",
)

# XDT transform operation vocabulary (full table)
_XDT_OPERATIONS = (
    "Replace", "Insert", "InsertBefore", "InsertAfter",
    "Remove", "RemoveAll", "RemoveAttributes", "SetAttributes",
    "Condition",
)

# sensitive word list (candidate marking on key names;
# the value channel is already covered by the contract's sensitive scan)
_SENSITIVE_NAME_RE_WORDS = (
    "apikey", "clientsecret", "privatekey", "certificate",
    "user", "password",
)

# builtin sections (no configSections registration needed;
# a missing registration can never be asserted as a defect)
_BUILTIN_SECTIONS = set(_KNOWN_SECTIONS) | {"configSections", "location"}


# ============================================================
# entry point (guarded semantic pass)
# ============================================================

def apply_dotconfig_profile(doc, detection: Dict[str, Any],
                            file_name: str) -> None:
    """Build gate + semantic pass body (silent degradation, never blocks).

    Gate: detected_syntax == xml and the contract's root_tag ==
    ``configuration`` (MSBuild / custom XML DSLs are not built;
    the absence of the profile layer is itself the honest
    disclosure).
    """
    try:
        _apply(doc, detection, file_name)
    except Exception:  # pragma: no cover - defensive guard
        _log.debug("dotnet config profile pass skipped")


def _apply(doc, detection: Dict[str, Any], file_name: str) -> None:
    md = doc.metadata
    if not isinstance(md, dict) or md.get("parse_failed"):
        return
    contract = cc.get_contract(doc)
    if contract is None:
        return
    if detection.get("detected_syntax") != "xml":
        return
    xml_meta = contract.get("xml") or {}
    if xml_meta.get("root_tag") != "configuration":
        return

    containers: List[Dict[str, Any]] = contract.get("containers") or []
    entries: List[Dict[str, Any]] = contract.get("entries") or []
    attr_index = _attribute_index(entries)

    top = [
        c for c in containers
        if len(c.get("path") or []) == 2
        and (c.get("path") or [None])[0] == "configuration"
    ]

    profile: Dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "sections": _build_sections(top),
        "operations": _build_operations(xml_meta, attr_index),
        "connection_strings": _build_connection_strings(
            xml_meta, attr_index,
        ),
        "external_references": _build_external_references(
            top, attr_index, file_name,
        ),
    }
    profile.update(_build_registrations(xml_meta, top, attr_index))
    profile.update(_build_transforms(entries, xml_meta))
    profile.update(_build_protected(containers, entries))
    profile.update(_build_location_scopes(containers, attr_index))
    profile.update(_build_risk_candidates(containers, attr_index))
    profile.update(_build_sensitive_disclosure(
        entries, profile.get("connection_strings") or [],
    ))
    # local effective view + inheritance layers + lock disclosure
    # + transform application surface
    profile["local_effective_view"] = _build_local_effective_view(
        xml_meta, attr_index, top,
    )
    profile["inheritance_model"] = _build_inheritance_model()
    profile.update(_build_lock_disclosure(xml_meta, attr_index))
    profile["transform_application"] = _build_transform_application(
        file_name, entries, xml_meta,
    )
    profile["inheritance_merge_status"] = "not_performed"
    profile["transform_application_status"] = (
        profile["transform_application"].get("application_status")
    )
    # domain profiles (NuGet / WCF / log4net / NLog — additive)
    profile["domain_profiles"] = _build_domain_profiles(
        xml_meta, attr_index, file_name, top,
    )
    # restricted schema validation (builtin rules — additive)
    profile["schema_validation"] = _build_schema_validation(
        xml_meta, attr_index, containers, top, profile,
    )
    profile["status"] = _build_status(md, profile)
    profile["loss_codes"] = _collect_loss_codes(profile)

    md["dotnet_config_profile"] = profile

    # file_role lifted into config_profile (additive key)
    cp = md.get("config_profile")
    if isinstance(cp, dict) and "file_role" not in cp:
        cp["file_role"] = _detect_file_role(file_name, contract)


# ============================================================
# helpers: contract traversal indexes
# ============================================================

def _attribute_index(entries: List[Dict[str, Any]]) -> Dict[Any, Dict[str, Any]]:
    """Parent-container path tuple → {attr name: value} (first
    occurrence in document order).

    Attribute paths of repeated adds with the same key collide — the
    first-seen value is enough for the semantic layer (the operation
    order is preserved by the containers' document order, not by
    this index).
    """
    index: Dict[Any, Dict[str, Any]] = {}
    for entry in entries:
        path = entry.get("path") or []
        if len(path) >= 2 and isinstance(path[-1], str) and path[-1].startswith("@"):
            key = tuple(path[:-1])
            index.setdefault(key, {})[path[-1][1:]] = entry.get("value")
    return index


def _attrs_of(attr_index, path) -> Dict[str, Any]:
    return attr_index.get(tuple(path), {})


def _op_segment(path: List[Any]) -> Optional[List[str]]:
    """Element path → canonical segments [configuration, section, op].

    XmlParser takes the list branch for repeated same-named children;
    two shapes are normalized:

    - **repeated same-named op elements** (e.g. several add under a
      section): the element's real path has one extra numeric index
      at the tail ``[configuration, section, op, idx]``;
    - **repeated same-named sections** (e.g. two appSettings): the
      container instance path has a numeric index in the middle
      ``[configuration, section, idx, op]``.

    This helper normalizes both shapes into three plain segments so
    single-instance, repeated-op and repeated-section cases share
    the same decision logic.
    """
    if len(path) == 4 and isinstance(path[3], str) and path[3].isdigit():
        seg = path[:3]
    elif (
        len(path) == 4
        and isinstance(path[2], str)
        and path[2].isdigit()
        and isinstance(path[3], str)
    ):
        seg = [path[0], path[1], path[3]]
    elif len(path) == 3:
        seg = list(path)
    else:
        return None
    if (
        len(seg) == 3
        and seg[0] == "configuration"
        and isinstance(seg[1], str)
        and isinstance(seg[2], str)
    ):
        return seg
    return None


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1"):
            return True
        if low in ("false", "0"):
            return False
    return None


# ============================================================
# section semantics table
# ============================================================

def _build_sections(top: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    for c in top:
        name = (c.get("path") or [None, None])[1]
        if not isinstance(name, str):
            continue
        info = _KNOWN_SECTIONS.get(name)
        if info is not None:
            item = {
                "name": name,
                "section_kind": "known",
                "domain": info["domain"],
            }
            if name in _THIRD_PARTY_SECTIONS:
                item["third_party_profile"] = "known_name_no_schema"
        else:
            item = {"name": name, "section_kind": "unknown_candidate"}
        sections.append(item)
    return sections


# ============================================================
# add/remove/clear order-preserving operation modeling
# ============================================================

_OP_ELEMENTS = ("add", "remove", "clear")

# ------------------------------------------------------------
# view sections → (key attr, value attr)
# (key comparison is case-insensitive — appSettings key /
# connectionStrings name are both case-insensitive in .NET)
# ------------------------------------------------------------
_VIEW_SECTIONS: Dict[str, Tuple[str, str]] = {
    "appSettings": ("key", "value"),
    "connectionStrings": ("name", "connectionString"),
}

# ------------------------------------------------------------
# lock attribute family
# ------------------------------------------------------------
_LOCK_ATTRS = (
    "lockItem", "lockElements", "lockAttributes",
    "lockAllAttributesExcept", "lockAllElementsExcept",
)

# transform operation vocabulary (for camel → snake normalization)
_TRANSFORM_OP_NAMES = frozenset(_XDT_OPERATIONS)

# ------------------------------------------------------------
# domain profile vocabulary (schema coverage stays partial;
# schema must not be over-inferred)
# ------------------------------------------------------------
_NUGET_SECTIONS = frozenset({
    "packageSources", "packageSourceCredentials", "disabledSources",
    "activePackageSource", "fallbackPackageFolders", "config",
    "packageSourceMapping", "repositoryPath",
})

_WCF_TOP_ELEMENTS = frozenset({
    "bindings", "client", "services", "behaviors", "extensions",
    "serviceHostingEnvironment", "diagnostics",
})

_LOG4NET_TOP_ELEMENTS = frozenset({
    "appender", "root", "logger", "renderer",
})

_NLOG_TOP_ELEMENTS = frozenset({
    "targets", "rules", "variables", "extensions",
})

_DOMAIN_SECTIONS = frozenset({
    "system.serviceModel", "log4net", "nlog",
})

# ------------------------------------------------------------
# restricted schema rules: section → (allowed child set, required
# add attributes) (zero IO — no external XSD loading; external
# schema access belongs to the protection surface)
# ------------------------------------------------------------
_SCHEMA_RULED_SECTIONS: Dict[str, Tuple[frozenset, Tuple[str, ...]]] = {
    "appSettings": (frozenset({"add", "remove", "clear"}), ("key",)),
    "connectionStrings": (
        frozenset({"add", "remove", "clear"}),
        ("name", "connectionString"),
    ),
}


def _build_operations(
    xml_meta: Dict[str, Any],
    attr_index,
) -> Dict[str, List[Dict[str, Any]]]:
    """Order-preserving add/remove/clear modeling (xml.node_order
    document-order skeleton).

    The skeleton takes the element nodes of node_order (real paths
    incl. list indexes); attributes are fetched via attr_index by
    each element's real path — repeated adds each hit their own
    attributes, with no reliance on the index-less container paths
    (which would collide and lose data). clear is preserved by the
    element node itself even without attributes.
    """
    operations: Dict[str, List[Dict[str, Any]]] = {}
    op_sections = ("appSettings", "connectionStrings")
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        seg = _op_segment(path)
        if (
            seg is None
            or seg[1] not in op_sections
            or seg[2] not in _OP_ELEMENTS
        ):
            continue
        attrs = attr_index.get(tuple(path), {})
        op: Dict[str, Any] = {"op": seg[2]}
        if seg[2] == "clear":
            # clear acts on the inherited configuration layers,
            # not on this file's keys/values
            op["inheritance_operation"] = True
        for src in ("key", "name", "value"):
            if src in attrs:
                op[src] = attrs[src]
        operations.setdefault(seg[1], []).append(op)
    return operations


# ============================================================
# connectionString candidate splitting
# ============================================================

def _build_connection_strings(
    xml_meta: Dict[str, Any], attr_index,
) -> List[Dict[str, Any]]:
    """connectionString element-level scan (node_order skeleton;
    repeated adds each preserved)."""
    result: List[Dict[str, Any]] = []
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        seg = _op_segment(path)
        if (
            seg is None
            or seg[1] != "connectionStrings"
            or seg[2] != "add"
        ):
            continue
        attrs = attr_index.get(tuple(path), {})
        raw = attrs.get("connectionString")
        candidates, confidence = _split_connstr(raw)
        result.append({
            "name": attrs.get("name"),
            "provider": attrs.get("providerName"),
            "raw_connection_string": raw,
            "parsed_candidates": candidates,
            "parse_confidence": confidence,
        })
    return result


def _split_connstr(raw: Optional[str]):
    """Split ``;``-separated kv candidates (key names lowercased,
    values preserved).

    The well-formed kv ratio is parse_confidence; non-standard
    shapes → empty candidates + confidence 0 + raw preserved
    (honest degradation).
    """
    if not isinstance(raw, str) or not raw.strip():
        return {}, 0.0
    tokens = [t.strip() for t in raw.split(";") if t.strip()]
    if not tokens:
        return {}, 0.0
    candidates: Dict[str, Any] = {}
    well_formed = 0
    for token in tokens:
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip()
            if key:
                candidates[key.lower()] = value.strip()
                well_formed += 1
    return candidates, round(well_formed / len(tokens), 3)


# ============================================================
# local effective view (document-order replay, zero IO)
# ============================================================

def _build_local_effective_view(
    xml_meta: Dict[str, Any], attr_index, top: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """add/remove/clear document-order replay → local effective view.

    Repeated adds with the same key (case-insensitive) **do not
    masquerade as last-wins** — within a single file a repeated add
    is a .NET runtime error candidate (System.Configuration throws
    ConfigurationErrorsException; the FileConfigurationProvider
    family throws FormatException). Such a key takes the first-seen
    value for local_value + the ``duplicate_add`` flag, and
    effective_value is set to None (undecidable). Normal keys
    cannot see the inheritance layers → effective_value is always
    ``"unknown"`` (honest degradation, no guessed synthesis).
    """
    views: Dict[str, Dict[str, Any]] = {
        "appSettings": {}, "connectionStrings": {},
    }
    canon: Dict[str, Dict[str, str]] = {
        "appSettings": {}, "connectionStrings": {},
    }
    first_paths: Dict[Tuple[str, str], str] = {}
    live_dups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    dups: List[Dict[str, Any]] = []
    removed_absent: List[Dict[str, Any]] = []
    cleared: List[str] = []

    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        seg = _op_segment(path)
        if (
            seg is None
            or seg[1] not in _VIEW_SECTIONS
            or seg[2] not in _OP_ELEMENTS
        ):
            continue
        section, op = seg[1], seg[2]
        attrs = attr_index.get(tuple(path), {})
        if op == "clear":
            # clear resets the replay map (the inherited layers and
            # all previous adds in this file are wiped)
            views[section] = {}
            canon[section] = {}
            for k in [k for k in live_dups if k[0] == section]:
                del live_dups[k]
            for k in [k for k in first_paths if k[0] == section]:
                del first_paths[k]
            if section not in cleared:
                cleared.append(section)
            continue
        key_field, value_field = _VIEW_SECTIONS[section]
        raw_key = attrs.get(key_field)
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        key = raw_key.strip()
        low = key.lower()
        canonical = canon[section].get(low)
        if op == "remove":
            if canonical is not None:
                del views[section][canonical]
                del canon[section][low]
                first_paths.pop((section, low), None)
            else:
                # remove target absent — a finding, not a loss
                # (candidate disclosure)
                removed_absent.append({
                    "section": section,
                    "key": key,
                    "resolution_status": "absent_at_remove",
                })
            continue
        path_display = ".".join(str(s) for s in path)
        if canonical is None:
            canonical = key
            canon[section][low] = canonical
            views[section][canonical] = {
                "local_value": attrs.get(value_field),
                "source": "local",
                "effective_value": "unknown",
            }
            first_paths[(section, low)] = path_display
        else:
            entry = views[section][canonical]
            entry["duplicate_add"] = True
            entry["effective_value"] = None
            rec = live_dups.get((section, low))
            if rec is None:
                rec = {
                    "section": section,
                    "key": canonical,
                    "name": canonical,
                    "occurrences": [
                        first_paths.get((section, low), ""),
                        path_display,
                    ],
                    "resolution_status": "runtime_error_candidate",
                }
                dups.append(rec)
                live_dups[(section, low)] = rec
            else:
                rec["occurrences"].append(path_display)

    # sections referenced via configSource/file → external_merge_pending
    pending: List[str] = []
    for c in top:
        cpath = c.get("path") or []
        raw_section = cpath[1] if len(cpath) == 2 else None
        if isinstance(raw_section, str) and raw_section in _VIEW_SECTIONS:
            attrs = _attrs_of(attr_index, cpath)
            if (
                isinstance(attrs.get("configSource"), str)
                and attrs.get("configSource")
            ) or (
                isinstance(attrs.get("file"), str) and attrs.get("file")
            ):
                if raw_section not in pending:
                    pending.append(raw_section)

    return {
        "app_settings": views["appSettings"],
        "connection_strings": views["connectionStrings"],
        "duplicate_adds": dups,
        "removed_absent": removed_absent,
        "inherited_cleared": cleared,
        "external_merge_pending": pending,
    }


# ============================================================
# inheritance layer disclosure (zero IO — disclose facts only,
# never guess existence)
# ============================================================

def _build_inheritance_model() -> Dict[str, Any]:
    """machine.config → root Web.config → this file: three-layer disclosure.

    A full hierarchy merge would require reading external files
    (impossible under zero IO) — the inheritance layers only
    register names and the not_read / unknown_presence facts;
    without the full directory environment effective_value stays
    unknown.
    """
    return {
        "layers": [
            {
                "layer": "machine.config",
                "status": "not_read",
                "reason": "external_file_not_readable",
            },
            {"layer": "root_web_config", "status": "unknown_presence"},
            {"layer": "this_file", "status": "parsed"},
        ],
        "effective_value_note": (
            "single-file view: effective values require the full "
            "inheritance chain (machine.config / root web.config), "
            "which is not read under the zero-IO policy"
        ),
    }


# ============================================================
# lock attribute family candidate disclosure
# ============================================================

def _build_lock_disclosure(
    xml_meta: Dict[str, Any], attr_index,
) -> Dict[str, Any]:
    """lock* attribute family scan (node_order skeleton, values passed through)."""
    locked: List[Dict[str, Any]] = []
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        if not path or path[0] != "configuration":
            continue
        attrs = attr_index.get(tuple(path), {})
        if not attrs:
            continue
        for lock_attr in _LOCK_ATTRS:
            if lock_attr in attrs:
                locked.append({
                    "path_display": ".".join(str(s) for s in path),
                    "lock_type": lock_attr,
                    "value": attrs.get(lock_attr),
                })
    return {"locked_sections": locked}


# ============================================================
# domain profiles (NuGet / WCF / log4net / NLog)
# ============================================================

def _names_of(path: List[Any]) -> List[Any]:
    """Element path → plain name sequence (numeric index components
    of repeated same-named elements stripped)."""
    return [
        c for c in path
        if not (isinstance(c, str) and c.isdigit())
    ]


def _build_domain_profiles(
    xml_meta: Dict[str, Any], attr_index, file_name: str,
    top: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """specific_domain_profile channel (distinct from
    dotnet_config_candidate; nuget.config ≠ web.config).

    Each domain is built independently with no cross-dependency;
    schema_coverage=partial (schema must not be over-inferred;
    unknown elements go through candidates, never error verdicts).
    MSBuild (root not configuration) stays honestly absent behind
    the gate.
    """
    sections_present = {
        (c.get("path") or [None, None])[1] for c in top
        if isinstance((c.get("path") or [None, None])[1], str)
    }
    profiles: Dict[str, Any] = {}
    base = (file_name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    nuget_sections = sections_present & _NUGET_SECTIONS
    nuget_by_name = base.lower() == "nuget.config"
    if nuget_sections or nuget_by_name:
        profiles["nuget"] = _build_nuget_profile(
            xml_meta, attr_index, top,
            detected_by=(
                "both" if (nuget_by_name and nuget_sections)
                else ("file_name" if nuget_by_name else "sections")
            ),
        )
    if "system.serviceModel" in sections_present:
        profiles["wcf"] = _build_wcf_profile(xml_meta, attr_index)
    if "log4net" in sections_present:
        profiles["log4net"] = _build_log4net_profile(xml_meta, attr_index)
    if "nlog" in sections_present:
        profiles["nlog"] = _build_nlog_profile(xml_meta, attr_index)
    return profiles


def _build_nuget_profile(
    xml_meta: Dict[str, Any], attr_index, top: List[Dict[str, Any]],
    detected_by: str,
) -> Dict[str, Any]:
    """NuGet.config profile (packageSources family).

    packageSourceCredentials **only registers key names**
    (Username / ClearTextPassword etc.) — credential values never
    land in any channel (never store value). file= references belong
    to the external-reference channel and are not duplicated here.
    """
    sources: List[Dict[str, Any]] = []
    removed: List[str] = []
    cred_map: Dict[str, List[str]] = {}
    disabled: List[Dict[str, Any]] = []
    config_keys: List[Dict[str, Any]] = []
    active: Dict[str, Any] = {}
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        names = _names_of(path)
        if not names or names[0] != "configuration":
            continue
        section = names[1] if len(names) >= 2 else None
        if section not in _NUGET_SECTIONS:
            continue
        attrs = attr_index.get(tuple(path), {})
        elem = names[-1]
        if section == "packageSources":
            if elem == "add":
                sources.append({
                    "name": attrs.get("key"),
                    "value": attrs.get("value"),
                })
            elif elem == "remove" and isinstance(attrs.get("key"), str):
                removed.append(attrs["key"])
        elif section == "packageSourceCredentials":
            if elem == "add" and len(names) >= 4:
                source = names[2]
                if isinstance(attrs.get("key"), str):
                    cred_map.setdefault(str(source), []).append(
                        attrs["key"]
                    )
        elif section == "disabledSources":
            if elem == "add":
                disabled.append({
                    "name": attrs.get("key"),
                    "value": attrs.get("value"),
                })
        elif section == "config":
            if elem == "add":
                config_keys.append({
                    "key": attrs.get("key"),
                    "value": attrs.get("value"),
                })
        elif section == "activePackageSource":
            if elem == "add":
                active = {"name": attrs.get("key")}
    unknown = [
        {"name": s}
        for s in sorted(
            {
                (c.get("path") or [None, None])[1]
                for c in top
                if isinstance((c.get("path") or [None, None])[1], str)
            } - _NUGET_SECTIONS - _BUILTIN_SECTIONS
        )
    ]
    return {
        "detected_by": detected_by,
        "sources": sources,
        "removed_sources": removed,
        "credential_sources": [
            {"source": src, "credential_keys": keys}
            for src, keys in cred_map.items()
        ],
        "disabled_sources": disabled,
        "config_keys": config_keys,
        "active_package_source": active,
        "unknown_elements": unknown,
        "schema_coverage": "partial",
    }


def _build_wcf_profile(
    xml_meta: Dict[str, Any], attr_index,
) -> Dict[str, Any]:
    """WCF system.serviceModel profile (structural disclosure of
    bindings/client/services/behaviors; service attribution follows
    a document-order state machine)."""
    bindings: List[Dict[str, Any]] = []
    client_eps: List[Dict[str, Any]] = []
    service_eps: List[Dict[str, Any]] = []
    behaviors: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    current_service: Optional[str] = None
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        names = _names_of(path)
        if len(names) < 3 or names[0] != "configuration":
            continue
        if names[1] != "system.serviceModel":
            continue
        attrs = attr_index.get(tuple(path), {})
        elem = names[-1]
        if len(names) == 3:
            # direct serviceModel children not in the table →
            # unknown candidates, never error verdicts
            if elem not in _WCF_TOP_ELEMENTS and isinstance(elem, str):
                unknown.append({
                    "name": elem,
                    "path_display": ".".join(str(s) for s in path),
                })
            continue
        group = names[2]
        if group == "bindings" and len(names) >= 5:
            bindings.append({
                "binding_type": names[3],
                "name": attrs.get("name"),
                "attributes": {
                    k: v for k, v in attrs.items() if k != "name"
                },
            })
        elif group == "client" and elem == "endpoint":
            client_eps.append({
                "name": attrs.get("name"),
                "address": attrs.get("address"),
                "binding": attrs.get("binding"),
                "binding_configuration": attrs.get("bindingConfiguration"),
                "contract": attrs.get("contract"),
            })
        elif group == "services":
            if elem == "service":
                current_service = attrs.get("name")
            elif elem == "endpoint":
                service_eps.append({
                    "service": current_service,
                    "address": attrs.get("address"),
                    "binding": attrs.get("binding"),
                    "contract": attrs.get("contract"),
                })
        elif group == "behaviors" and elem == "behavior":
            behaviors.append({
                "behavior_kind": names[3],
                "name": attrs.get("name"),
            })
    return {
        "bindings": bindings,
        "client_endpoints": client_eps,
        "service_endpoints": service_eps,
        "behaviors": behaviors,
        "unknown_elements": unknown,
        "schema_coverage": "partial",
    }


def _build_log4net_profile(
    xml_meta: Dict[str, Any], attr_index,
) -> Dict[str, Any]:
    """log4net profile (appenders / root / loggers — document-order
    state machine)."""
    appenders: List[Dict[str, Any]] = []
    root: Dict[str, Any] = {"level": None, "appender_refs": []}
    loggers: List[Dict[str, Any]] = []
    current_logger: Optional[Dict[str, Any]] = None
    unknown: List[Dict[str, Any]] = []
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        names = _names_of(path)
        if len(names) < 3 or names[0] != "configuration":
            continue
        if names[1] != "log4net":
            continue
        attrs = attr_index.get(tuple(path), {})
        elem = names[-1]
        if len(names) == 3:
            if elem == "appender":
                appenders.append({
                    "name": attrs.get("name"),
                    "type": attrs.get("type"),
                })
                continue
            if elem == "logger":
                current_logger = {
                    "name": attrs.get("name"),
                    "level": None,
                    "appender_refs": [],
                }
                loggers.append(current_logger)
                continue
            if elem not in _LOG4NET_TOP_ELEMENTS and isinstance(elem, str):
                unknown.append({
                    "name": elem,
                    "path_display": ".".join(str(s) for s in path),
                })
            continue
        if names[2] == "appender":
            continue  # inner appender params (file/param etc.) are not flattened
        if names[2] == "root":
            if elem == "level":
                root["level"] = attrs.get("value")
            elif elem == "appender-ref":
                if isinstance(attrs.get("ref"), str):
                    root["appender_refs"].append(attrs["ref"])
        elif names[2] == "logger":
            if current_logger is not None:
                if elem == "level":
                    current_logger["level"] = attrs.get("value")
                elif elem == "appender-ref":
                    if isinstance(attrs.get("ref"), str):
                        current_logger["appender_refs"].append(
                            attrs["ref"]
                        )
    return {
        "appenders": appenders,
        "root": root,
        "loggers": loggers,
        "unknown_elements": unknown,
        "schema_coverage": "partial",
    }


def _build_nlog_profile(
    xml_meta: Dict[str, Any], attr_index,
) -> Dict[str, Any]:
    """NLog profile (targets / rules / variables)."""
    targets: List[Dict[str, Any]] = []
    rules: List[Dict[str, Any]] = []
    variables: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        names = _names_of(path)
        if len(names) < 3 or names[0] != "configuration":
            continue
        if names[1] != "nlog":
            continue
        attrs = attr_index.get(tuple(path), {})
        elem = names[-1]
        if len(names) == 3:
            if elem not in _NLOG_TOP_ELEMENTS and isinstance(elem, str):
                unknown.append({
                    "name": elem,
                    "path_display": ".".join(str(s) for s in path),
                })
            continue
        group = names[2]
        if group == "targets" and elem == "target":
            targets.append({
                "name": attrs.get("name"),
                "type": attrs.get("type"),
            })
        elif group == "rules" and elem == "logger":
            rules.append({
                "name": attrs.get("name"),
                "min_level": attrs.get("minLevel"),
                "max_level": attrs.get("maxLevel"),
                "levels": attrs.get("levels"),
                "write_to": attrs.get("writeTo"),
                "final": attrs.get("final"),
            })
        elif group == "variables" and elem == "variable":
            value = attrs.get("value")
            if isinstance(attrs.get("name"), str) and cc.is_sensitive_key(
                attrs["name"]
            ):
                value = "****"
            variables.append({"name": attrs.get("name"), "value": value})
    return {
        "targets": targets,
        "rules": rules,
        "variables": variables,
        "unknown_elements": unknown,
        "schema_coverage": "partial",
    }


# ============================================================
# restricted schema validation (builtin rules — zero IO, no external XSD)
# ============================================================

def _build_schema_validation(
    xml_meta: Dict[str, Any], attr_index,
    containers: List[Dict[str, Any]],
    top: List[Dict[str, Any]], profile: Dict[str, Any],
) -> Dict[str, Any]:
    """restricted_builtin rule set (all findings are warning candidates).

    External schema access is blocked (XXE/DTD/XInclude/external
    schemas cannot be loaded — protection surface); with no
    findings it honestly reports restricted_pass (restricted pass,
    not a full-validation claim). position_warning and the
    duplicate_adds from earlier stages are **bridged** here as
    findings (no duplicate modeling, original keys untouched).
    """
    findings: List[Dict[str, Any]] = []
    validated: List[str] = []

    def hit(rule: str, path, **extra) -> None:
        item = {
            "rule": rule,
            "severity": "warning",
            "path_display": ".".join(str(s) for s in path),
        }
        item.update(extra)
        findings.append(item)

    # duplicate top-level section declarations (list branch:
    # repeated instances get a numeric index in the middle of the
    # container path [configuration, section, idx] — the first
    # occurrence has len==2 with no index, so a numeric-tailed
    # instance is a duplicate declaration)
    for c in containers:
        cpath = c.get("path") or []
        if (
            len(cpath) == 3
            and cpath[0] == "configuration"
            and isinstance(cpath[1], str)
            and isinstance(cpath[2], str)
            and cpath[2].isdigit()
        ):
            hit("duplicate_section_declaration", cpath, section=cpath[1])

    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        names = _names_of(path)
        if len(names) < 3 or names[0] != "configuration":
            continue
        section = names[1]
        attrs = attr_index.get(tuple(path), {})
        elem = names[-1]
        # ruled sections: allowed children + required add attributes
        ruled = _SCHEMA_RULED_SECTIONS.get(section)
        if ruled is not None and len(names) == 3:
            if section not in validated:
                validated.append(section)
            allowed, required = ruled
            if isinstance(elem, str) and elem not in allowed:
                hit("unexpected_child_element", path, section=section)
            elif elem == "add":
                for req in required:
                    if (
                        not isinstance(attrs.get(req), str)
                        or not attrs.get(req)
                    ):
                        rule = {
                            "appSettings": "appsettings_add_missing_key",
                            "connectionStrings":
                                "connectionstring_add_missing_"
                                + ("name" if req == "name" else "value"),
                        }.get(section, f"{section}_add_missing_{req}")
                        hit(rule, path, section=section)
        # configSections registration required attributes
        elif (
            section == "configSections"
            and elem in ("section", "sectionGroup")
            and len(names) == 3
        ):
            if "configSections" not in validated:
                validated.append("configSections")
            if not isinstance(attrs.get("name"), str) or not attrs.get(
                "name"
            ):
                hit("section_registration_missing_name", path)
            if not isinstance(attrs.get("type"), str) or not attrs.get(
                "type"
            ):
                hit("section_registration_missing_type", path)

    # bridge: configSections must be configuration's first child
    if profile.get("position_warning"):
        findings.append({
            "rule": "configsections_not_first_child",
            "severity": "warning",
            "path_display": "configuration.configSections",
        })
    # bridge: duplicate add declarations (same-source facts reused)
    for d in (profile.get("local_effective_view") or {}).get(
        "duplicate_adds"
    ) or []:
        findings.append({
            "rule": "duplicate_add_declaration",
            "severity": "warning",
            "path_display": (
                "configuration." + str(d.get("section", ""))
            ),
            "section": d.get("section"),
            "key": d.get("key"),
        })

    return {
        "mode": "restricted_builtin",
        "external_schema_access": "blocked",
        "validated_sections": validated,
        "findings": findings,
    }


# ============================================================
# configSource / file external-reference isolation
# ============================================================

def _build_external_references(
    top: List[Dict[str, Any]], attr_index, file_name: str,
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    base_name = (file_name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base_name = base_name.rsplit(":", 1)[-1] if ":" in base_name else base_name
    for c in top:
        path = c.get("path") or []
        section = path[1] if len(path) == 2 else None
        if not isinstance(section, str):
            continue
        attrs = _attrs_of(attr_index, path)
        for attr, ref_type, merge_mode in (
            ("configSource", "configSource", "section_replace"),
            ("file", "file", "key_value_merge"),
        ):
            target = attrs.get(attr)
            if not isinstance(target, str) or not target.strip():
                continue
            target_clean = target.strip().lstrip("./")
            self_reference = target_clean == base_name
            refs.append({
                "reference_type": ref_type,
                "target": target.strip(),
                "section": section,
                "merge_mode": merge_mode,
                "resolved": False,
                "resolution_status": "unresolved",
                "reason_code": (
                    "EXTERNAL_CONFIG_CYCLE" if self_reference
                    else "EXTERNAL_CONFIG_NOT_READ"
                ),
                "source_file": base_name,
            })
    return refs


# ============================================================
# configSections registration registry
# ============================================================

def _build_registrations(
    xml_meta: Dict[str, Any],
    top: List[Dict[str, Any]],
    attr_index,
) -> Dict[str, Any]:
    """configSections registration registry (node_order skeleton;
    repeated sections each preserved)."""
    registrations: List[Dict[str, Any]] = []
    registered_names: set = set()
    for node in xml_meta.get("node_order") or []:
        if node.get("kind") != "element":
            continue
        path = node.get("path") or []
        seg = _op_segment(path)
        if (
            seg is None
            or seg[1] != "configSections"
            or seg[2] not in ("section", "sectionGroup")
        ):
            continue
        attrs = attr_index.get(tuple(path), {})
        type_name, _, assembly = (
            str(attrs.get("type") or "").partition(",")
        )
        registrations.append({
            "section": attrs.get("name"),
            "type_name": type_name.strip() or None,
            "assembly": assembly.strip() or None,
            "allow_definition": attrs.get("allowDefinition"),
            "allow_location": attrs.get("allowLocation"),
            "restart_on_external_changes": _to_bool(
                attrs.get("restartOnExternalChanges"),
            ),
            "loaded": False,
            "executed": False,
        })
        if isinstance(attrs.get("name"), str):
            registered_names.add(attrs["name"])

    # usage-validation candidates: non-builtin top-level sections
    # without a registration (may be registered in an inherited
    # machine.config — undecidable from a single file; pure
    # disclosure, no loss code)
    candidates = [
        {"section": (c.get("path") or [None, None])[1]}
        for c in top
        if (c.get("path") or [None, None])[1] not in _BUILTIN_SECTIONS
        and (c.get("path") or [None, None])[1] not in registered_names
        and isinstance((c.get("path") or [None, None])[1], str)
    ]

    # position validation: configSections must be configuration's
    # first child (static string-surface disclosure)
    position_warning = False
    if registered_names or registrations:
        first = top[0].get("path") if top else None
        position_warning = not (
            first and len(first) == 2 and first[1] == "configSections"
        )
    return {
        "custom_section_registrations": registrations,
        "registration_candidates": candidates,
        "position_warning": position_warning,
    }


# ============================================================
# XDT transform static AST (precise namespace-URI gate)
# ============================================================

def _build_transforms(
    entries: List[Dict[str, Any]], xml_meta: Dict[str, Any],
) -> Dict[str, Any]:
    token: Optional[str] = None
    for ns in xml_meta.get("namespaces") or []:
        if ns.get("uri") == _XDT_URI:
            token = ns.get("prefix")
            break
    if token is None:
        return {"transform_rules": []}

    transform_attr = f"@{token}:Transform"
    locator_attr = f"@{token}:Locator"
    # parent element path → {transform, locator} (document order)
    rules_map: Dict[Any, Dict[str, Any]] = {}
    for entry in entries:
        path = entry.get("path") or []
        if len(path) >= 2 and isinstance(path[-1], str):
            parent = tuple(path[:-1])
            if path[-1] == transform_attr:
                rules_map.setdefault(parent, {})["transform"] = entry.get("value")
            elif path[-1] == locator_attr:
                rules_map.setdefault(parent, {})["locator"] = entry.get("value")

    rules: List[Dict[str, Any]] = []
    for parent, found in rules_map.items():
        operation = found.get("transform")
        if not isinstance(operation, str):
            continue
        rules.append({
            "target_path": ".".join(str(s) for s in parent),
            "operation": operation if operation in _XDT_OPERATIONS
            else f"unknown:{operation}",
            "locator": found.get("locator"),
            "executed": False,
        })
    return {"transform_rules": rules}


# ============================================================
# locator / transform declaration structured parsing
# ============================================================

def _split_top_level(s: str) -> Optional[List[str]]:
    """Comma-separated multi-operations (commas inside parentheses are
    not split); returns None on imbalance."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if depth != 0:
        return None
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts or [""]


def _split_call(s: str) -> Tuple[str, List[str]]:
    """``Name(arg1,arg2)`` → (name, [args]); no parentheses → (name, [])."""
    if "(" in s:
        name, _, rest = s.partition("(")
        if rest.endswith(")"):
            inner = rest[:-1].strip()
            args = (
                [a.strip() for a in inner.split(",") if a.strip()]
                if inner else []
            )
            return name.strip(), args
        return s.strip(), []
    return s.strip(), []


def _snake_name(name: str) -> str:
    return "".join(
        ("_" + ch.lower()) if ch.isupper() else ch for ch in name
    ).lstrip("_")


def parse_locator(raw: Any) -> Dict[str, Any]:
    """``xdt:Locator`` string → locator_model.

    Absent → ``{"kind": "all"}``; ``Match(a,b)`` → match+attributes;
    ``Condition(xpath)`` / ``XPath(...)`` → kind+xpath preserved.
    """
    if not isinstance(raw, str) or not raw.strip():
        return {"kind": "all"}
    s = raw.strip()
    name, args = _split_call(s)
    if name == "Match":
        return {"kind": "match", "attributes": args}
    if name == "Condition":
        return {"kind": "condition", "xpath": args[0] if args else None}
    if name == "XPath":
        return {"kind": "xpath", "xpath": args[0] if args else None}
    return {"kind": name.lower(), "raw": s}


def parse_transform(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """``xdt:Transform`` string → (transform_model, parse_error).

    Supports comma-separated multi-operations and parameterized
    forms; operation names in the vocabulary are normalized
    camel→snake, unrecognized names follow the ``unknown:`` prefix
    channel; unbalanced parentheses → parse_error registers the
    raw text (never silently dropped).
    """
    if not isinstance(raw, str) or not raw.strip():
        return {"operations": []}, None
    parts = _split_top_level(raw.strip())
    if parts is None:
        return {"operations": [], "parse_error": raw}, raw
    operations: List[Dict[str, Any]] = []
    for part in parts:
        if not part:
            continue
        name, args = _split_call(part)
        if not name:
            continue
        if name in _TRANSFORM_OP_NAMES:
            operations.append({"name": _snake_name(name), "arguments": args})
        else:
            operations.append({"name": f"unknown:{name}", "arguments": args})
    return {"operations": operations}, None


# ============================================================
# XDT transform static application (transform-file four-way view)
# ============================================================

def _xdt_token(xml_meta: Dict[str, Any]) -> Optional[str]:
    for ns in xml_meta.get("namespaces") or []:
        if ns.get("uri") == _XDT_URI:
            return ns.get("prefix")
    return None


def _infer_base_candidate(file_name: str) -> Optional[str]:
    """Pure filename-convention inference (zero IO — the base file
    is never read).

    ``Web.Release.config`` → ``Web.config``; ``app.Debug.config``
    → ``app.config``; ``*.transform`` → the suffix stripped; no
    conventional suffix → None (honest: no convention, no guess).
    """
    base = (file_name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    low = base.lower()
    for seg in (".debug", ".release", ".transform"):
        if low.endswith(seg + ".config"):
            return (
                base[: len(base) - len(seg) - len(".config")] + ".config"
            )
        if low.endswith(seg):
            return base[: len(base) - len(seg)]
    return None


def _projected_effect(
    parent, locator_model: Dict[str, Any],
    transform_model: Dict[str, Any],
) -> str:
    """Static prose describing which base nodes the transform would
    target (never executed)."""
    target = ".".join(str(s) for s in parent)
    kind = (locator_model or {}).get("kind")
    if kind == "match":
        attrs = ", ".join(
            (locator_model or {}).get("attributes") or [],
        )
        match_desc = f"matching attributes ({attrs})"
    elif kind in ("condition", "xpath"):
        match_desc = f"matching {kind} {(locator_model or {}).get('xpath')}"
    else:
        match_desc = "matching all nodes"
    names = ", ".join(
        str(o.get("name"))
        for o in (transform_model or {}).get("operations") or []
    ) or "no parsed operation"
    return (
        f"would target nodes at '{target}' ({match_desc}) "
        f"and apply: {names}; not executed (static disclosure only)"
    )


def _build_transform_application(
    file_name: str,
    entries: List[Dict[str, Any]],
    xml_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """Transform-file four-way view.

    base_config (not_read) / transform_config (this file parsed) /
    transformation_rule (node_type=config_transform, executed:
    false) / effective_config ({} — transforms not applied by
    default). No xdt namespace → file_role=actual_config,
    not_applicable.
    """
    token = _xdt_token(xml_meta)
    is_transform = token is not None
    transform_attr = f"@{token}:Transform" if token else None
    locator_attr = f"@{token}:Locator" if token else None

    rules_map: Dict[Any, Dict[str, Any]] = {}
    if is_transform:
        for entry in entries:
            path = entry.get("path") or []
            if len(path) >= 2 and isinstance(path[-1], str):
                parent = tuple(path[:-1])
                if path[-1] == transform_attr:
                    rules_map.setdefault(parent, {})["transform"] = (
                        entry.get("value")
                    )
                elif path[-1] == locator_attr:
                    rules_map.setdefault(parent, {})["locator"] = (
                        entry.get("value")
                    )

    rules: List[Dict[str, Any]] = []
    parse_errors: List[str] = []
    for parent, found in rules_map.items():
        operation = found.get("transform")
        if not isinstance(operation, str):
            continue
        locator_model = parse_locator(found.get("locator"))
        transform_model, parse_error = parse_transform(operation)
        if parse_error:
            parse_errors.append(parse_error)
        rules.append({
            "node_type": "config_transform",
            "target_path": ".".join(str(s) for s in parent),
            "transform": operation,
            "transform_model": transform_model,
            "locator_model": locator_model,
            "projected_effect": _projected_effect(
                parent, locator_model, transform_model,
            ),
            "executed": False,
        })

    return {
        "file_role": "transform_config" if is_transform else "actual_config",
        "base_config_candidate": (
            _infer_base_candidate(file_name) if is_transform else None
        ),
        "base_config_status": "not_read" if is_transform else None,
        "transformation_rules": rules,
        "parse_errors": parse_errors,
        "effective_config": {},
        "application_status": (
            "not_applied" if is_transform else "not_applicable"
        ),
    }


# ============================================================
# protected configuration recognition
# ============================================================

def _build_protected(
    containers: List[Dict[str, Any]], entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    encrypted: List[Dict[str, Any]] = []
    provider: Optional[str] = None
    cipher_paths = {
        tuple(c.get("path") or [])
        for c in containers
        if (c.get("path") or [None])[-1] in ("CipherData", "CipherValue")
    }
    for c in containers:
        path = c.get("path") or []
        if (path or [None])[-1] == "EncryptedData":
            has_cipher = any(
                p[: len(path)] == tuple(path) for p in cipher_paths if p
            )
            encrypted.append({
                "path_display": ".".join(str(s) for s in path),
                "ciphertext_present": has_cipher,
                "decryption_status": "not_attempted",
            })
    for entry in entries:
        value = entry.get("value")
        if isinstance(value, str) and value in _PROTECTED_PROVIDERS:
            provider = value
            break
    for item in encrypted:
        item["provider_candidate"] = provider
    return {
        "protected_configurations": [
            dict(item, provider_candidate=provider) for item in encrypted
        ],
    }


# ============================================================
# D9 location scopes
# ============================================================

def _build_location_scopes(
    containers: List[Dict[str, Any]], attr_index,
) -> Dict[str, Any]:
    scopes: List[Dict[str, Any]] = []
    for c in containers:
        path = c.get("path") or []
        if (
            len(path) == 2
            and path[0] == "configuration"
            and path[1] == "location"
        ):
            attrs = _attrs_of(attr_index, path)
            wrapped = {
                (cc_.get("path") or [None, None, None])[2]
                for cc_ in containers
                if len(cc_.get("path") or []) == 3
                and (cc_.get("path") or [])[:2] == ["configuration", "location"]
            }
            wrapped.discard(None)
            scopes.append({
                "path": attrs.get("path"),
                "inherit_in_child_applications": _to_bool(
                    attrs.get("inheritInChildApplications"),
                ),
                "sections": sorted(wrapped),
                "inheritance_boundary": True,
            })
    return {"location_scopes": scopes}


# ============================================================
# IIS / ASP.NET risk candidates
# ============================================================

def _build_risk_candidates(
    containers: List[Dict[str, Any]], attr_index,
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []

    def add(kind: str, path) -> None:
        candidates.append({
            "kind": kind,
            "path_display": ".".join(str(s) for s in path),
        })

    for c in containers:
        path = c.get("path") or []
        name = (path or [None])[-1]
        if not isinstance(name, str) or len(path) < 3:
            continue
        attrs = _attrs_of(attr_index, path)
        if "system.webServer" in path:
            if name == "handlers":
                add("handler_candidate", path)
            elif name == "rewrite":
                add("rewrite_rule_candidate", path)
            elif name == "modules":
                add("module_candidate", path)
            elif name == "proxy":
                add("proxy_candidate", path)
            elif name == "requestFiltering" and str(
                attrs.get("allowDoubleEscaping", "")
            ).lower() == "true":
                add("request_filtering_candidate", path)
        elif "system.web" in path:
            if name == "compilation" and str(
                attrs.get("debug", "")
            ).lower() == "true":
                add("debug_enabled_candidate", path)
            elif name == "customErrors" and str(
                attrs.get("mode", "")
            ).lower() == "off":
                add("custom_errors_candidate", path)
            elif name == "authentication" and str(
                attrs.get("mode", "")
            ).lower() in ("none", "forms"):
                add("authentication_mode_candidate", path)
    return {"risk_candidates": candidates}


# ============================================================
# refined sensitive aggregation (existing candidates +
# connection-string candidates)
# ============================================================

def _build_sensitive_disclosure(
    entries: List[Dict[str, Any]],
    connection_strings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    types: Dict[str, int] = {}
    for entry in entries:
        for cand in entry.get("sensitive_candidates") or []:
            ctype = cand.get("type")
            if isinstance(ctype, str):
                types[ctype] = types.get(ctype, 0) + 1
    connstr_hits = 0
    for cs in connection_strings:
        pc = cs.get("parsed_candidates") or {}
        if any(k in pc for k in ("password", "user id", "pwd", "user")):
            connstr_hits += 1
    if connstr_hits:
        types["credential_in_connection_string"] = connstr_hits
    for entry in entries:
        key = str(entry.get("key") or "")
        low = key.lower()
        if any(w in low for w in _SENSITIVE_NAME_RE_WORDS):
            types["sensitive_name_candidate"] = (
                types.get("sensitive_name_candidate", 0) + 1
            )
    return {
        "sensitive_disclosure": {
            "types": types,
            "note": "detection/marking only; values are never stored",
        },
    }


# ============================================================
# layered status projection
# ============================================================

def _build_status(md: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    xml_meta = md.get("xml") or {}
    parse_status = xml_meta.get("parse_status")
    has_refs = bool(profile.get("external_references"))
    has_rules = bool(profile.get("transform_rules"))
    has_protected = bool(profile.get("protected_configurations"))
    has_findings = bool(
        (profile.get("sensitive_disclosure") or {}).get("types")
    ) or bool(profile.get("risk_candidates"))
    # restricted schema validation status (additive new key; the
    # existing six keys untouched)
    schema_findings = (profile.get("schema_validation") or {}).get(
        "findings"
    )
    return {
        "syntax_status": "complete" if parse_status == "ok" else "partial",
        "schema_status": (
            "warning" if schema_findings else "restricted_pass"
        ),
        "external_reference_status": (
            "not_resolved" if has_refs else "not_applicable"
        ),
        "transform_status": (
            "not_executed" if has_rules else "not_applicable"
        ),
        "decryption_status": (
            "not_attempted" if has_protected else "not_applicable"
        ),
        # XML parsing success does not imply valid application config
        "runtime_status": "unknown",
        "security_status": (
            "review_required" if has_findings else "none"
        ),
    }


# ============================================================
# loss_codes collection (never enters the contract's loss.reasons)
# ============================================================

def _collect_loss_codes(profile: Dict[str, Any]) -> List[str]:
    codes: List[str] = []
    for ref in profile.get("external_references") or []:
        code = ref.get("reason_code")
        if isinstance(code, str) and code not in codes:
            codes.append(code)
    if profile.get("transform_rules"):
        codes.append("TRANSFORM_NOT_EXECUTED")
    if profile.get("protected_configurations"):
        codes.append("PROTECTED_CONFIG_NOT_DECRYPTED")
    return codes


# ============================================================
# file_role (zero-IO string decision)
# ============================================================

def _detect_file_role(file_name: str, contract: Dict[str, Any]) -> str:
    low = (file_name or "").lower()
    # nuget.config ≠ web.config (domain-specific naming)
    if low.endswith("nuget.config"):
        return "nuget_config"
    if low.endswith((".debug.config", ".release.config", ".transform")):
        return "transform_config_candidate"
    if low.endswith((".example.config", ".sample.config", ".dist.config")):
        return "example_config"
    if low.endswith((".template.config", ".tpl.config", ".in.config")):
        return "template_config"
    for comment in (contract.get("comments") or [])[:3]:
        text = str(comment.get("text") or "").lower()
        if any(
            marker in text
            for marker in ("auto-generated", "do not edit", "generated by")
        ):
            return "generated_config_candidate"
    return "actual_config"


# ============================================================
# Markdown summary (guarded attachment — values masked, never stored)
# ============================================================

_DISCLOSURE_NOTE = (
    "Markdown summary cannot replace: XML attributes, duplicate "
    "elements, clear/remove operations, transforms, external config, "
    "ciphertext, namespaces, original ordering, or source mapping."
)


def render_dotconfig_summary(
    profile: Dict[str, Any],
    contract: Dict[str, Any],
    config_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Markdown summary (aligned with the output-policy samples)."""
    lines: List[str] = [
        "# .config summary",
        "",
        f"> {_DISCLOSURE_NOTE}",
        "",
    ]

    operations = profile.get("operations") or {}
    app_ops = operations.get("appSettings") or []
    if app_ops:
        lines.append("## configuration.appSettings")
        lines.append("")
        lines.append("| Key | Value |")
        lines.append("|---|---|")
        for op in app_ops:
            if op.get("op") == "add":
                key = str(op.get("key") or "")
                value = str(op.get("value") or "")
                if cc.is_sensitive_key(key):
                    value = "****"
                lines.append(f"| `{key}` | `{value}` |")
        lines.append("")

    for cs in profile.get("connection_strings") or []:
        if cs.get("name"):
            lines.append("## configuration.connectionStrings")
            lines.append("")
            lines.append("| Name | Provider | Connection String |")
            lines.append("|---|---|---|")
            lines.append(
                f"| `{cs.get('name')}` | `{cs.get('provider') or ''}` | `****` |"
            )
            lines.append("")

    refs = profile.get("external_references") or []
    if refs:
        lines.append("## External References")
        lines.append("")
        for ref in refs:
            lines.append(
                f"- `{ref.get('section')}` ({ref.get('reference_type')}): "
                f"`{ref.get('target')}` - Status: not resolved"
            )
        lines.append("")

    # ---- local effective view / duplicate warnings / inheritance
    # disclosure / lock sections ----
    lev = profile.get("local_effective_view") or {}
    app_view = lev.get("app_settings") or {}
    if app_view:
        lines.append("## Local Effective View (appSettings)")
        lines.append("")
        lines.append("| Key | Local Value | Effective |")
        lines.append("|---|---|---|")
        for key, entry in app_view.items():
            value = str((entry or {}).get("local_value") or "")
            if cc.is_sensitive_key(str(key)):
                value = "****"
            if (entry or {}).get("duplicate_add"):
                value = f"{value} (duplicate)" if value else "(duplicate)"
            lines.append(f"| `{key}` | `{value}` | `unknown` |")
        lines.append("")
    cs_view = lev.get("connection_strings") or {}
    if cs_view:
        lines.append("## Local Effective View (connectionStrings)")
        lines.append("")
        lines.append("| Name | Local Value | Effective |")
        lines.append("|---|---|---|")
        for name in cs_view:
            dup_flag = ""
            if (cs_view.get(name) or {}).get("duplicate_add"):
                dup_flag = " (duplicate)"
            lines.append(f"| `{name}` | `****{dup_flag}` | `unknown` |")
        lines.append("")
    dups = lev.get("duplicate_adds") or []
    if dups:
        lines.append("## Duplicate Add Candidates")
        lines.append("")
        lines.append(
            "- duplicate keys in a single file are a .NET runtime "
            "error candidate (not last-wins); effective value null"
        )
        for d in dups:
            lines.append(
                f"- `{d.get('key')}` in `{d.get('section')}`: "
                f"{len(d.get('occurrences') or [])} occurrences "
                f"({d.get('resolution_status')})"
            )
        lines.append("")
    inh = profile.get("inheritance_model") or {}
    if inh.get("layers"):
        lines.append("## Inheritance")
        lines.append("")
        for layer in inh.get("layers") or []:
            reason = f" ({layer.get('reason')})" if layer.get("reason") else ""
            lines.append(
                f"- {layer.get('layer')}: {layer.get('status')}{reason}"
            )
        lines.append(
            "- effective values: unknown (single-file view; "
            "inheritance merge not performed)"
        )
        lines.append("")
    locked = profile.get("locked_sections") or []
    if locked:
        lines.append("## Locked Sections")
        lines.append("")
        for item in locked:
            lines.append(
                f"- `{item.get('path_display')}`: "
                f"{item.get('lock_type')}={item.get('value')}"
            )
        lines.append("")

    # ---- domain profiles section (additive) ----
    domains = profile.get("domain_profiles") or {}
    if domains:
        lines.append("## Domain Profiles")
        lines.append("")
        np = domains.get("nuget") or {}
        if np:
            for src in np.get("sources") or []:
                lines.append(
                    f"- NuGet source `{src.get('name')}`: "
                    f"`{src.get('value')}`"
                )
            creds = np.get("credential_sources") or []
            if creds:
                names = ", ".join(str(c.get("source")) for c in creds)
                lines.append(
                    f"- NuGet credentials present for: {names} "
                    "(values never stored)"
                )
            for d in np.get("disabled_sources") or []:
                lines.append(
                    f"- NuGet disabled source: `{d.get('name')}`"
                )
        wp = domains.get("wcf") or {}
        for ep in (wp.get("client_endpoints") or []):
            lines.append(
                f"- WCF client endpoint `{ep.get('name')}`: "
                f"`{ep.get('address')}` ({ep.get('binding')})"
            )
        for ep in (wp.get("service_endpoints") or []):
            lines.append(
                f"- WCF service endpoint for `{ep.get('service')}`: "
                f"`{ep.get('address')}` ({ep.get('binding')})"
            )
        lp = domains.get("log4net") or {}
        if lp:
            root = lp.get("root") or {}
            apps = ", ".join(
                str(a.get("name")) for a in lp.get("appenders") or []
            )
            lines.append(
                f"- log4net root level: {root.get('level')}; "
                f"appenders: {apps or 'none'}"
            )
        npr = domains.get("nlog") or {}
        if npr:
            tgs = ", ".join(
                str(t.get("name")) for t in npr.get("targets") or []
            )
            rules = npr.get("rules") or []
            lines.append(
                f"- NLog targets: {tgs or 'none'}; "
                f"rules: {len(rules)}"
            )
        lines.append("")

    # ---- schema validation section (additive — restricted builtin rules) ----
    sv = profile.get("schema_validation") or {}
    if sv.get("validated_sections") or sv.get("findings"):
        lines.append("## Schema Validation (restricted)")
        lines.append("")
        lines.append(
            "- mode: restricted builtin rules; external schema "
            "access blocked"
        )
        fnd = sv.get("findings") or []
        if fnd:
            for item in fnd:
                lines.append(
                    f"- warning `{item.get('rule')}` at "
                    f"`{item.get('path_display')}`"
                )
        else:
            lines.append(
                "- restricted pass: no findings under the built-in "
                "rule set (not a full schema validation)"
            )
        lines.append("")

    if config_profile:
        lines.append("## Format Detection Report")
        lines.append("")
        lines.append("| Property | Value |")
        lines.append("|---|---|")
        lines.append("| Extension | `.config` |")
        lines.append(
            f"| Detected Format | {config_profile.get('detected_syntax')} |"
        )
        lines.append(f"| Profile | {config_profile.get('profile')} |")
        lines.append(
            f"| Confidence | {config_profile.get('confidence')} |"
        )
        lines.append("| External Access | blocked |")
        lines.append("")

    return "\n".join(lines)
