# -*- coding: utf-8 -*-
r"""Parser for the ESLint/Babel dotfile JSON/YAML family.

Identity layer: .eslintrc / .babelrc dotfiles in four file forms
(bare / .json / .yml / .yaml) yield a Dotfile Identity record
(tool / config_system / declared_syntax / detected_syntax /
parser_route / format_mismatch / js_syntax_detected). ESLint here
means the legacy config system (flat eslint.config.js belongs to
the JavaScript config family and is not parsed); .babelrc is
file-relative (babel.config.* is the project-root form).

Content detection routes by content, not file name: strict JSON
first, then JS syntax hints (JS_SYNTAX_DETECTED), then a
tolerant JsonParser pass (JSONC dialect disclosure reused), then
a YamlParser fallback (JSON wins over YAML).

Delegation: parsing is delegated to the existing JsonParser /
YamlParser; this layer only attaches additive keys on top of
their family contracts (zero drift).

Tool profile registry: ESLint extends classification, plugins,
parser, overrides, env, root and the rule severity matrix; Babel
presets/plugins (string or [name, options]), env names, and
ignore/only globs. Invalid values are disclosed (raw preserved),
never silently fixed or dropped.

Zero execution: every reference is registered as
reference_candidate (resolution_status=not_resolved,
executed=false); nothing is read, resolved or executed. Where
PyYAML (YAML 1.1) and js-yaml (the real loader) disagree on
scalar lexing, a second core-schema load provides a token view
for disambiguation, and loader-level divergences are disclosed.
"""

import json
import logging
import time
from pathlib import Path

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.dotfile_discovery import (
    _JS_FAMILY_NAMES,
)
from provendoc.core.parsers.config.dotfile_discovery import (
    attach_package_json_embedded_configs as attach_package_json_embedded_configs,
)
from provendoc.core.parsers.config.dotfile_discovery import (
    babel_env_effective as babel_env_effective,
)
from provendoc.core.parsers.config.dotfile_discovery import (
    build_config_discovery as build_config_discovery,
)
from provendoc.core.parsers.config.dotfile_discovery import (
    effective_rules as effective_rules,
)
from provendoc.core.parsers.config.dotfile_discovery import (
    match_target_files as match_target_files,
)
from provendoc.core.parsers.config.dotfile_discovery import (
    merge_eslint_legacy as merge_eslint_legacy,
)
from provendoc.core.parsers.config.dotfile_semantics import (
    _FAMILY_OF,
    _JS_HINT_RE,
    _declared_syntax_of,
    _is_jsonc_dialect,
    _scan_template_references,
    _sem,
    _tool_of,
    _yaml_token_view,
)
from provendoc.core.parsers.config.dotfile_semantics import (
    _parse_rule_entry as _parse_rule_entry,
)
from provendoc.core.parsers.config.dotfile_semantics import (
    _sem_key_pairs as _sem_key_pairs,
)
from provendoc.core.parsers.config.dotfile_tool_profiles import (
    _build_babel_profile,
    _build_eslint_profile,
    _disclose_js_yaml_load_divergences,
)

_log = logging.getLogger(__name__)


_FORMAT = "dotfile_json_yaml"


class DotfileJsonYamlParser(BaseParser):
    """Dotfile JSON/YAML family parser (identity + detection +
    delegation)."""

    def parse(self, file_path) -> Document:
        """Parse an eslintrc/babelrc dotfile."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path, _FORMAT, self.encoding, True,
            )
            text = ctx.text
            file_name = Path(file_path).name
            tool, config_system = _tool_of(file_name)
            # ---- JS-family file-name guard ----
            if file_name.lower() in _JS_FAMILY_NAMES:
                return self._failure_document(
                    text, file_name, tool, config_system,
                    "JS_SYNTAX_DETECTED",
                    "javascript config family variant (by file "
                    "name): belongs to the JavaScript Config "
                    "Parser; not parsed as data",
                    detected="javascript",
                )
            if tool is None:
                # Guard: unrecognized dotfile name
                return self._failure_document(
                    text, file_name, None, None,
                    "INVALID_DOTFILE_SYNTAX",
                    "not an eslintrc/babelrc dotfile name",
                )
            declared = _declared_syntax_of(file_name)

            # ---- Content detection (by content, not file name) ----
            stripped = text.strip()
            if not stripped:
                return self._failure_document(
                    text, file_name, tool, config_system,
                    "INVALID_DOTFILE_SYNTAX",
                    "empty content: no known dotfile syntax detected",
                    declared=declared, detected="unknown",
                )
            if _JS_HINT_RE.search(text):
                return self._failure_document(
                    text, file_name, tool, config_system,
                    "JS_SYNTAX_DETECTED",
                    "javascript syntax detected (module.exports/"
                    "exports/require/export default): belongs to "
                    "the JavaScript config family; not parsed as "
                    "data",
                    declared=declared, detected="javascript",
                )

            try:
                json.loads(stripped)
                strict_json = True
            except (ValueError, RecursionError):
                strict_json = False

            # ---- Delegation to JsonParser / YamlParser ----
            from provendoc.core.parsers.config.json_parser import (
                JsonParser,
            )
            from provendoc.core.parsers.config.yaml_parser import (
                YamlParser,
            )

            doc = None
            detected = None
            route = None
            if strict_json:
                doc = JsonParser().parse(file_path)
                detected, route = "json", "json"
            else:
                # Not strict JSON: tolerant JSON first (JSONC),
                # then YAML fallback
                cand = JsonParser().parse(file_path)
                if cand.metadata.get("parse_success"):
                    doc = cand
                    route = "json"
                    contract = cand.metadata.get("config_entries") or {}
                    detected = (
                        "jsonc" if _is_jsonc_dialect(contract) else "json"
                    )
                else:
                    doc = YamlParser().parse(file_path)
                    detected, route = "yaml", "yaml"

            # ---- Additive keys on top of the delegated contract ----
            md = doc.metadata
            md["syntax_profile"] = _FORMAT
            contract = md.get("config_entries")
            if not isinstance(contract, dict):
                contract = cc.build_contract(_FORMAT, cc.DUP_LAST_WINS)
                md["config_entries"] = contract

            js_flag = False
            mismatch = (
                declared is not None
                and _FAMILY_OF.get(declared)
                != _FAMILY_OF.get(detected)
            )
            md["dotfile_identity"] = {
                "tool": tool,
                "config_system": config_system,
                "source_file": file_name,
                "declared_syntax": declared,
                "detected_syntax": detected,
                "parser_route": route,
                "format_mismatch": mismatch,
                "js_syntax_detected": js_flag,
            }

            reasons = contract["loss"]["reasons"]
            if mismatch:
                reasons.append(
                    f"FORMAT_EXTENSION_MISMATCH: declared {declared} "
                    f"but content detected as {detected}; routed by "
                    "content, not by file name."
                )
            if declared == "json" and detected == "jsonc":
                reasons.append(
                    "JSONC_SYNTAX_IN_JSON_FILE: comments/trailing "
                    "commas tolerated; treated as json-family dialect "
                    "(jsonc_dialect_in_json disclosure from JsonParser "
                    "applies unchanged)"
                )

            # ---- Loader divergence disclosure for the YAML route ----
            if route == "yaml":
                _disclose_js_yaml_load_divergences(
                    reasons, contract, md.get("structured_data"),
                )

            # ---- Tool profile registration (zero execution) ----
            # token view for the YAML route (bool-word disambiguation)
            yaml_view = (
                _yaml_token_view(text) if detected == "yaml" else None
            )
            self._attach_tool_profile(
                md, contract, tool, config_system, detected, file_name,
                yaml_view,
            )

            md["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3
            )
            return doc

        except Exception as e:  # pragma: no cover - defensive guard
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                },
            )

    # ========================================
    # Profile attach (additive, guarded)
    # ========================================

    def _attach_tool_profile(self, md, contract, tool, config_system,
                             detected, file_name, yaml_view=None):
        try:
            raw_sd = md.get("structured_data")
            # Non-mapping top level: disclosed as invalid
            top_level_invalid = (
                raw_sd is not None and not isinstance(raw_sd, dict)
            )
            sd = raw_sd if isinstance(raw_sd, dict) else {}
            if tool == "eslint":
                parts = _build_eslint_profile(
                    sd, file_name, detected, yaml_view,
                )
            else:
                parts = _build_babel_profile(sd, yaml_view)

            refs = parts["tool_references"]
            success = bool(md.get("parse_success"))
            tool_profile = {
                "tool": tool,
                "config_system": config_system,
                "syntax_status": "complete" if success else "failed",
                "tool_profile_status": (
                    "complete" if success else "failed"
                ),
            }
            if top_level_invalid:
                tool_profile["top_level_mapping"] = False
            if tool == "eslint":
                tool_profile.update(parts["root_flag"])
            contract["tool_profile"] = tool_profile
            contract["tool_references"] = refs
            contract["tool_overrides"] = parts["tool_overrides"]
            contract["tool_rules"] = parts["tool_rules"]
            if parts["tool_environments"]:
                contract["tool_environments"] = parts["tool_environments"]

            reasons = contract["loss"]["reasons"]

            # ---- P1 additive keys ----
            if parts.get("rule_matrix"):
                contract["rule_matrix"] = parts["rule_matrix"]
                if parts.get("invalid_severity_count"):
                    reasons.append(
                        f"INVALID_RULE_SEVERITY: "
                        f"{parts['invalid_severity_count']} rule value(s) "
                        "not a valid severity (raw preserved, not fixed)"
                    )
                if parts.get("invalid_shape_count"):
                    reasons.append(
                        f"INVALID_RULE_SHAPE: "
                        f"{parts['invalid_shape_count']} rule value(s) "
                        "not a valid rule shape (raw preserved)"
                    )
            if parts.get("parser_options") is not None:
                contract["tool_parser_options"] = parts["parser_options"]
                if parts.get("parser_options_file_refs"):
                    reasons.append(
                        "parser_options_file_reference: "
                        + ", ".join(parts["parser_options_file_refs"])
                        + " reference external file(s) (registered, "
                        "not read)"
                    )
            if parts.get("file_patterns"):
                contract["file_patterns"] = parts["file_patterns"]
                invalid_globs = sum(
                    1 for f in parts["file_patterns"]
                    if f.get("match_status") == "invalid"
                )
                if invalid_globs:
                    reasons.append(
                        f"INVALID_GLOB_PATTERN: {invalid_globs} "
                        "pattern(s) fail static glob lexing "
                        "(match_status=invalid; not matched)"
                    )
            if tool == "babel" and parts.get("tool_file_patterns"):
                contract["tool_file_patterns"] = parts["tool_file_patterns"]

            # ---- P3 additive keys ----
            if tool == "eslint":
                if parts.get("tool_globals") is not None:
                    contract["tool_globals"] = parts["tool_globals"]
                if parts.get("tool_settings") is not None:
                    contract["tool_settings"] = parts["tool_settings"]
                if parts.get("tool_report_unused_disable_directives") \
                        is not None:
                    contract[
                        "tool_report_unused_disable_directives"
                    ] = parts["tool_report_unused_disable_directives"]
                if parts.get("tool_env_entries"):
                    contract["tool_env_entries"] = parts[
                        "tool_env_entries"
                    ]
                    if parts.get("invalid_env_value_count"):
                        reasons.append(
                            f"INVALID_ENV_VALUE: "
                            f"{parts['invalid_env_value_count']} env "
                            "value(s) not a boolean (raw preserved, "
                            "not coerced)"
                        )
                if parts.get("external_project_references"):
                    contract["external_project_references"] = parts[
                        "external_project_references"
                    ]
                    reasons.append(
                        "external_project_reference: "
                        + ", ".join(
                            p["raw_value"]
                            for p in parts["external_project_references"]
                        )
                        + " registered under settings (resolved=false, "
                        "not read; spec batch2 #11)"
                    )

            # Reference graph projection (no resolution)
            if refs:
                contract["reference_graph"] = {
                    "source_file": file_name,
                    "nodes": [
                        {
                            "id": f"ref:{i}",
                            "kind": r["kind"],
                            "raw_value": r["raw_value"],
                            "path": r.get("path"),
                            "resolution_status": r["resolution_status"],
                        }
                        for i, r in enumerate(refs)
                    ],
                    "edges": [
                        {
                            "from": file_name,
                            "to": f"ref:{i}",
                            "type": r["kind"],
                        }
                        for i, r in enumerate(refs)
                    ],
                }
                reasons.append(
                    f"tool_references_registered: {len(refs)} tool "
                    "reference(s) registered for future resolution "
                    "(not_resolved, zero execution)"
                )

            # Template / environment reference detection
            # (core-tree path keys)
            if sd:
                trefs = _scan_template_references(
                    _sem(sd, yaml_view),
                )
                if trefs:
                    contract["template_references"] = trefs
                    env_count = sum(
                        1 for t in trefs
                        if t["kind"] == "environment_reference"
                    )
                    tpl_count = len(trefs) - env_count
                    if env_count:
                        reasons.append(
                            f"environment_reference: {env_count} "
                            "value(s) reference environment variables "
                            "(registered; not read, not resolved)"
                        )
                    if tpl_count:
                        reasons.append(
                            f"template_reference: {tpl_count} template "
                            "value(s) registered (not executed)"
                        )

            # ---- Audit disclosures (raw always preserved) ----
            if top_level_invalid:
                reasons.append(
                    "INVALID_TOP_LEVEL: config root is not a mapping "
                    f"(type: {type(raw_sd).__name__}); empty tool "
                    "profile registered, structured_data preserved "
                    "as-is"
                )
            if parts.get("invalid_list_entry_count"):
                contract["invalid_list_entry_count"] = parts[
                    "invalid_list_entry_count"
                ]
                reasons.append(
                    f"INVALID_LIST_ENTRY: "
                    f"{parts['invalid_list_entry_count']} non-string "
                    "list entries skipped during registration "
                    "(string entries registered partially; raw "
                    "preserved in parser entries)"
                )
            if parts.get("invalid_ref_entry_count"):
                contract["invalid_ref_entry_count"] = parts[
                    "invalid_ref_entry_count"
                ]
                reasons.append(
                    f"INVALID_REF_ENTRY: "
                    f"{parts['invalid_ref_entry_count']} preset/plugin "
                    "entry(ies) not a registrable shape (skipped; raw "
                    "preserved in parser entries)"
                )
            if parts.get("root_value_invalid"):
                reasons.append(
                    "INVALID_ROOT_VALUE: root is not a boolean (raw "
                    "value preserved in tool_profile.root, not coerced)"
                )
            if parts.get("invalid_field_shapes"):
                contract["invalid_field_shapes"] = parts[
                    "invalid_field_shapes"
                ]
                reasons.append(
                    "INVALID_FIELD_SHAPE: "
                    + ", ".join(parts["invalid_field_shapes"])
                    + " field(s) present but not the expected container"
                    " type (value preserved in structured_data, "
                    "not interpreted)"
                )
            if parts.get("yaml_bool_token_count"):
                contract["yaml_bool_token_count"] = parts[
                    "yaml_bool_token_count"
                ]
                reasons.append(
                    f"YAML_BOOL_TOKEN: "
                    f"{parts['yaml_bool_token_count']} rule "
                    "severity(s) written as bare YAML 1.1 boolean word "
                    "(off/on/yes/no), parsed as boolean by PyYAML; "
                    "token recovered via a second core-schema pass "
                    "(js-yaml v4 parity: off -> off; on/yes/no -> "
                    "invalid severity); literal true/false judged as "
                    "invalid shape (JSON parity)"
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            _log.debug("dotfile tool profile attach skipped: %s", exc)

    # ========================================
    # Failure paths
    # ========================================

    def _failure_document(self, text, file_name, tool, config_system,
                          reason_code, message, declared=None,
                          detected=None):
        identity = {
            "tool": tool,
            "config_system": config_system,
            "source_file": file_name,
            "declared_syntax": declared,
            "detected_syntax": detected,
            "parser_route": (
                "javascript_config_family"
                if reason_code == "JS_SYNTAX_DETECTED" else "none"
            ),
            "format_mismatch": False,
            "js_syntax_detected": reason_code == "JS_SYNTAX_DETECTED",
        }
        contract = cc.build_contract(_FORMAT, cc.DUP_LAST_WINS)
        contract["tool_profile"] = {
            "tool": tool,
            "config_system": config_system,
            "syntax_status": "failed",
            "tool_profile_status": "failed",
        }
        contract["tool_references"] = []
        contract["tool_overrides"] = []
        contract["tool_rules"] = {}
        contract["loss"]["reasons"].append(
            f"{reason_code}: {message}"
        )
        return Document(
            text=text,
            metadata={
                "parse_success": False,
                "reason_code": reason_code,
                "detected_format": _FORMAT,
                "syntax_profile": _FORMAT,
                "warnings": [message],
                "dotfile_identity": identity,
                "config_entries": contract,
                "structured_data": {"_error": message},
            },
        )


def _sev_cell(rule):
    return rule.get("normalized_severity") or "invalid"


def _render_eslint_markdown(contract):
    lines = ["# ESLint Configuration", ""]
    matrix = contract.get("rule_matrix") or []
    global_rules = [r for r in matrix if r.get("scope") == "global"]
    if global_rules:
        lines += [
            "## Global Rules", "",
            "| Rule | Severity | Options |", "|---|---|---|",
        ]
        for r in global_rules:
            opts = ", ".join(
                str(o) for o in (r.get("options") or [])
            )
            lines.append(f"| {r.get('rule_name')} | {_sev_cell(r)} | {opts} |")
        lines.append("")
    overrides = contract.get("tool_overrides") or []
    if overrides:
        lines += ["## Overrides", ""]
        for ov in overrides:
            files = ", ".join(ov.get("files") or [])
            excluded = ", ".join(ov.get("excluded_files") or [])
            lines += [
                f"### overrides[{ov.get('index')}]", "",
                f"Files: {files}", "",
            ]
            if excluded:
                lines += [f"Excluded: {excluded}", ""]
            ov_scope = f"overrides[{ov.get('index')}]"
            ov_rules = [r for r in matrix if r.get("scope") == ov_scope]
            if ov_rules:
                lines += [
                    "| Rule | Severity |", "|---|---|",
                ]
                for r in ov_rules:
                    lines.append(
                        f"| {r.get('rule_name')} | {_sev_cell(r)} |"
                    )
                lines.append("")
    return "\n".join(lines)


def _render_babel_markdown(contract):
    refs = contract.get("tool_references") or []
    presets = [r for r in refs if r.get("kind") == "preset"]
    plugins = [r for r in refs if r.get("kind") == "plugin"]
    lines = ["# Babel Configuration", ""]
    if presets:
        lines += [
            "## Presets", "",
            "| Preset | Options |", "|---|---|",
        ]
        for r in presets:
            opts = (
                json.dumps(r.get("options"), ensure_ascii=False,
                           default=str)
                if r.get("options") is not None else ""
            )
            lines.append(f"| {r.get('raw_value')} | {opts} |")
        lines.append("")
    if plugins:
        lines += [
            "## Plugins", "",
            "| Plugin | Resolution |", "|---|---|",
        ]
        for r in plugins:
            resolution = str(r.get("resolution_status") or "").replace(
                "_", " ",
            )
            lines.append(
                f"| {r.get('raw_value')} | {resolution} |"
            )
        lines.append("")
    return "\n".join(lines)


def build_dotfile_markdown(contract):
    """Render a human-readable Markdown summary of the contract
    (pure projection, zero IO). The rendering cannot replace the
    original JSON/YAML: rule order, override matching conditions,
    options, comments, extends sources and unresolved references
    live only in the raw evidence.
    """
    try:
        tp = contract.get("tool_profile") or {}
        tool = tp.get("tool")
        if tool == "eslint":
            return _render_eslint_markdown(contract)
        if tool == "babel":
            return _render_babel_markdown(contract)
        return ""
    except Exception:  # pragma: no cover - defensive guard
        return ""

# ------------------------------------------------------------
# Back-compat re-exports: external consumers (tests/config,
# json_parser) import these names from this module. The class and
# build_dotfile_markdown above are original residents; everything
# else was extracted to dotfile_semantics / dotfile_tool_profiles /
# dotfile_discovery with zero behavior drift.
# ------------------------------------------------------------
