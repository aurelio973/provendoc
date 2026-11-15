# -*- coding: utf-8 -*-
r"""Config discovery, legacy merge and target matching for the dotfile JSON/YAML family.

Extracted verbatim from dotfile_json_yaml_parser.py (zero-drift
split, 2026-11-14): project-level config discovery tables, ESLint
legacy cascading-config merge with precedence, glob-to-regex target
matching, effective-rule resolution for ESLint and Babel, and
package.json embedded-config registration. Zero execution:
references stay candidates (not_resolved, executed=false).
"""

import os
import re

from provendoc.core.parsers.config.dotfile_semantics import (
    _classify_glob,
    _yaml_token_view,
)
from provendoc.core.parsers.config.dotfile_tool_profiles import (
    _build_eslint_profile,
)

_EMBEDDED_TOOL_KEYS = (
    ("eslintConfig", "eslint", "legacy"),
    ("babel", "babel", "file_relative"),
)


def attach_package_json_embedded_configs(metadata, structured_data,
                                          file_path):
    """Register top-level eslintConfig/babel keys inside
    package.json as embedded configs (identified only, never
    merged or parsed as dotfiles; non-package.json files are
    untouched). Guarded: failures never break the main parse.
    """
    base = str(file_path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base != "package.json":
        return
    if not isinstance(structured_data, dict):
        return
    found = []
    for key, tool, system in _EMBEDDED_TOOL_KEYS:
        value = structured_data.get(key)
        if key in structured_data and isinstance(value, dict):
            found.append({
                "source_file": "package.json",
                "json_path": f"/{key}",
                "tool": tool,
                "config_system": system,
            })
    if not found:
        return
    metadata["embedded_dotfile_configs"] = found
    contract = metadata.get("config_entries")
    if isinstance(contract, dict):
        contract["embedded_dotfile_configs"] = found
        contract["loss"]["reasons"].append(
            f"package_embedded_config: {len(found)} embedded config(s) "
            "identified (embedded_package_config source; not merged, "
            "not parsed as dotfile)"
        )


# Documented ESLint legacy precedence (high to low; JS variants
# hold a slot in the chain but stay unparsed)
_ESLINT_LEGACY_PRECEDENCE = (
    ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs",
    ".eslintrc.yaml", ".eslintrc.yml",
    ".eslintrc.json", ".eslintrc",
    "package.json#eslintConfig",
)


_FLAT_ESLINT_NAMES = frozenset((
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
))


# JS variant names: JavaScript config family, never parsed here
# (kept out of discovery parses to avoid misleading entries)
_ESLINT_JS_VARIANT_NAMES = frozenset((
    ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs",
))


_BABEL_JS_VARIANT_NAMES = frozenset((
    ".babelrc.js", ".babelrc.cjs", ".babelrc.mjs",
))


# Babel project-root configs (JS family; different scope from
# .babelrc*, registered but not parsed or merged)
_BABEL_PROJECT_NAMES = frozenset((
    "babel.config.js", "babel.config.cjs", "babel.config.mjs",
))


# JS family names for the parse-entry guard (file name is
# family identity; these never enter JSON/YAML parsing)
_JS_FAMILY_NAMES = frozenset(
    _ESLINT_JS_VARIANT_NAMES | _BABEL_JS_VARIANT_NAMES
    | _BABEL_PROJECT_NAMES | _FLAT_ESLINT_NAMES
)


_GLOB_REGEX_CACHE: dict[str, re.Pattern[str] | None] = {}


def _glob_to_regex(pattern):
    """Compile a normalized glob to a regex (minimatch-like
    semantics). ``**/`` matches zero or more segments; ``*``
    stays within a segment; ``?`` is one char; ``[...]`` passes
    through; returns None on unbalanced brackets.
    """
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            if j - i >= 2:
                if j < n and pattern[j] == "/":
                    out.append("(?:[^/]+/)*")
                    i = j + 1
                else:
                    out.append(".*")
                    i = j
            else:
                out.append("[^/]*")
                i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                return None
            out.append(pattern[i:close + 1])
            i = close + 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    try:
        return re.compile("".join(out))
    except re.error:
        return None


def _glob_match(pattern, target):
    """Static glob x target match (both posix-normalized)."""
    norm = pattern.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    rx = _GLOB_REGEX_CACHE.get(norm, False)
    if rx is False:
        rx = _glob_to_regex(norm)
        _GLOB_REGEX_CACHE[norm] = rx
    if rx is None:
        return False
    t = target.replace("\\", "/")
    if t.startswith("./"):
        t = t[2:]
    return rx.match(t) is not None


def _parse_config_file(file_path):
    """Route a batch file to an existing parser by name/suffix;
    None if unparseable."""
    name = str(file_path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.startswith(".eslintrc") or name.startswith(".babelrc"):
        # Guarded deferred import: this module is re-exported by
        # dotfile_json_yaml_parser, so a top-level mutual import
        # would create an import cycle.
        from provendoc.core.parsers.config.dotfile_json_yaml_parser import (
            DotfileJsonYamlParser,
        )
        return DotfileJsonYamlParser().parse(file_path)
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    if suffix in ("json", "jsonc"):
        from provendoc.core.parsers.config.json_parser import JsonParser
        return JsonParser().parse(file_path)
    if suffix in ("yaml", "yml"):
        from provendoc.core.parsers.config.yaml_parser import YamlParser
        return YamlParser().parse(file_path)
    return None


def _eslint_precedence_key(name_lower, embedded=False):
    key = name_lower if not embedded else "package.json#eslintConfig"
    return _ESLINT_LEGACY_PRECEDENCE.index(key) \
        if key in _ESLINT_LEGACY_PRECEDENCE else len(_ESLINT_LEGACY_PRECEDENCE)


def build_config_discovery(file_paths):
    """Build the config discovery table (no automatic merging).

    Each file is classified (eslintrc/babelrc dotfiles,
    package.json embedded configs, flat eslint.config.* registered
    as unparsed_javascript); ESLint legacy picks one candidate by
    documented precedence, the rest are ignored; babel candidates
    coexist; legacy and flat together is disclosed as
    LEGACY_FLAT_CONFIG_MIXED. Unrecognized files go to
    ignored_files. Only files listed by the caller are parsed.
    """
    candidates = []
    ignored_files = []
    reasons = []
    legacy_eslint = []
    flat_present = False
    babel_project_present = False

    for fp in file_paths:
        name = str(fp).replace("\\", "/").rsplit("/", 1)[-1]
        lower = name.lower()
        if lower in _FLAT_ESLINT_NAMES:
            candidates.append({
                "source_file": name,
                "tool": "eslint",
                "config_system": "flat",
                "status": "unparsed_javascript",
                "selection": "registered",
                "selection_reason": (
                    "flat config belongs to the JavaScript config "
                    "family; registered, not parsed, not merged"
                ),
            })
            flat_present = True
            continue
        # JS variant names never enter the parser; eslint
        # variants still take part in precedence
        if lower in _ESLINT_JS_VARIANT_NAMES:
            cand = {
                "source_file": name,
                "tool": "eslint",
                "config_system": "legacy",
                "status": "unparsed_javascript",
                "selection": "registered",
                "selection_reason": (
                    "JavaScript config family variant; registered, "
                    "not parsed (zero JS execution)"
                ),
            }
            candidates.append(cand)
            legacy_eslint.append(cand)
            continue
        if lower in _BABEL_JS_VARIANT_NAMES:
            candidates.append({
                "source_file": name,
                "tool": "babel",
                "config_system": "file_relative",
                "status": "unparsed_javascript",
                "selection": "registered",
                "selection_reason": (
                    "JavaScript config family variant; registered, "
                    "not parsed (zero JS execution)"
                ),
            })
            babel_project_present = True
            continue
        # babel.config.* project-root configs: registered, not parsed
        if lower in _BABEL_PROJECT_NAMES:
            candidates.append({
                "source_file": name,
                "tool": "babel",
                "config_system": "project_root",
                "status": "unparsed_javascript",
                "selection": "registered",
                "selection_reason": (
                    "project-wide root config belongs to the JavaScript "
                    "config family; registered, not parsed; scope "
                    "differs from file-relative .babelrc (spec batch2 #3)"
                ),
            })
            babel_project_present = True
            continue
        is_eslint_dotfile = lower.startswith(".eslintrc")
        is_babel_dotfile = lower.startswith(".babelrc")
        if not (is_eslint_dotfile or is_babel_dotfile
                or lower == "package.json"):
            ignored_files.append({
                "source_file": name,
                "reason": (
                    "not recognized as a tool config file "
                    "(may serve as an extends target)"
                ),
            })
            continue

        doc = _parse_config_file(fp)
        md = doc.metadata if doc is not None else {}
        if lower == "package.json":
            for emb in md.get("embedded_dotfile_configs") or []:
                cand = {
                    "source_file": name,
                    "json_path": emb.get("json_path"),
                    "tool": emb.get("tool"),
                    "config_system": emb.get("config_system"),
                    "status": "embedded",
                    "parse_success": bool(md.get("parse_success")),
                    "selection": "registered",
                    "selection_reason": "embedded package config source",
                }
                candidates.append(cand)
                if emb.get("tool") == "eslint":
                    legacy_eslint.append(cand)
            continue

        tool = "eslint" if is_eslint_dotfile else "babel"
        ident = md.get("dotfile_identity") or {}
        cand = {
            "source_file": name,
            "tool": tool,
            "config_system": ident.get("config_system"),
            "status": "parsed",
            "syntax_format": ident.get("detected_syntax"),
            "parse_success": bool(md.get("parse_success")),
            "selection": "registered",
            "selection_reason": "",
        }
        candidates.append(cand)
        if tool == "eslint":
            legacy_eslint.append(cand)

    # ESLint legacy: pick one by documented precedence
    selected = None
    if legacy_eslint:
        ordered = sorted(
            legacy_eslint,
            key=lambda c: _eslint_precedence_key(
                c["source_file"].lower(),
                embedded=c.get("status") == "embedded",
            ),
        )
        selected = ordered[0]
        selected["selection"] = "selected"
        selected["selection_reason"] = (
            "highest precedence in the documented legacy chain ("
            + " > ".join(_ESLINT_LEGACY_PRECEDENCE[:7])
            + " > package.json#eslintConfig)"
        )
        for c in ordered[1:]:
            c["selection"] = "ignored"
            c["selection_reason"] = (
                f"lower precedence than {selected['source_file']} "
                "(legacy eslint loads exactly one config file)"
            )

    if flat_present and legacy_eslint:
        reasons.append(
            "LEGACY_FLAT_CONFIG_MIXED: both legacy (.eslintrc*) and "
            "flat (eslint.config.*) candidates present; strictly "
            "separated, never merged (spec batch2 #2)"
        )

    # .babelrc (file-relative) and babel.config.* (project root)
    # have different scopes; coexistence is disclosed, not merged
    babelrc_present = any(
        c.get("tool") == "babel"
        and c.get("config_system") == "file_relative"
        for c in candidates
    )
    if babelrc_present and babel_project_present:
        reasons.append(
            "BABELRC_PROJECT_CONFIG_COEXIST: both file-relative "
            "(.babelrc*) and project-root (babel.config.*) candidates "
            "present; different scopes, never merged (spec batch2 #3)"
        )

    ignored_configs = [c for c in candidates if c.get("selection") == "ignored"]
    return {
        "candidates": candidates,
        "selected_config": selected,
        "ignored_configs": ignored_configs,
        "ignored_files": ignored_files,
        "reasons": reasons,
    }


def merge_eslint_legacy(file_paths):
    """Expand extends statically within the provided batch and
    merge the legacy cascade.

    local_file extends resolve only against files in the batch
    (shareable/builtin extends stay not_resolved; zero IO);
    local rules shadow extended rules with per-step provenance;
    overrides concatenate along the chain (base first).
    Ancestor-directory search (real eslint semantics) is not
    performed; see reasons.
    """
    # Batch files build eslint profiles from structured_data;
    # extends targets need not be dotfiles (plain JSON counts)
    parsed = {}
    js_variants_present = []
    for fp in file_paths:
        doc = _parse_config_file(fp)
        if doc is None:
            continue
        md = doc.metadata
        if not md.get("parse_success"):
            # JS family variants must be disclosed, not swallowed
            vname = str(fp).replace("\\", "/").rsplit("/", 1)[-1]
            if vname.lower() in _JS_FAMILY_NAMES:
                js_variants_present.append(vname)
            continue
        sd = md.get("structured_data")
        if not isinstance(sd, dict):
            continue
        name = str(fp).replace("\\", "/").rsplit("/", 1)[-1]
        # Pass detected syntax in (needed for YAML reflection);
        # dotfiles use the identity layer, plain files the suffix
        ident = md.get("dotfile_identity") or {}
        detected = ident.get("detected_syntax")
        if detected is None:
            suffix = (
                name.lower().rsplit(".", 1)[-1] if "." in name else ""
            )
            detected = "yaml" if suffix in ("yaml", "yml") else "json"
        # Token view for YAML files (same as the single-file path)
        yaml_view = None
        if detected == "yaml":
            try:
                with open(fp, encoding="utf-8") as f:
                    yaml_view = _yaml_token_view(f.read())
            except OSError:
                yaml_view = None
        parts = _build_eslint_profile(sd, name, detected, yaml_view)
        parsed[os.path.normpath(os.path.abspath(fp))] = (parts, name)

    # root: highest-precedence eslintrc dotfile in the batch
    roots = [
        key for key, (_, name) in parsed.items()
        if name.lower().startswith(".eslintrc")
    ]
    if not roots:
        return {
            "root_config": None,
            "extends_chain": [],
            "unresolved_extends": [],
            "merged_rules": [],
            "merged_overrides": [],
            "reasons": ["no_eslint_legacy_config_in_batch"],
        }
    root = sorted(
        roots,
        key=lambda k: _eslint_precedence_key(parsed[k][1].lower()),
    )[0]

    # Depth-first local extends expansion (visited guards cycles)
    order = []  # root-first; reversed for overlay (base first)
    chain_records = []
    unresolved = []
    visited = set()

    def _extends_refs_of(key):
        return [
            r for r in parsed[key][0].get("tool_references") or []
            if r.get("kind") == "extends"
        ]

    def _visit(key):
        if key in visited:
            return
        visited.add(key)
        order.append(key)
        root_dir = os.path.dirname(key)
        for ref in _extends_refs_of(key):
            raw = ref.get("raw_value")
            target = None
            if ref.get("classification") == "local_file":
                cand = os.path.normpath(
                    os.path.join(root_dir, str(raw))
                )
                if cand in parsed:
                    target = cand
            rec = {"raw_value": raw, "resolved_in_batch": target is not None}
            if target is not None:
                rec["source_file"] = parsed[target][1]
            chain_records.append(rec)
            if target is None:
                unresolved.append(raw)
            else:
                _visit(target)

    _visit(root)
    order.reverse()  # base-first (deepest extends source first)

    # Rule cascade: base first, root last (later writes shadow)
    rules_map = {}
    for key in order:
        parts, name = parsed[key]
        for entry in parts.get("rule_matrix") or []:
            if entry.get("scope") != "global":
                continue
            rname = entry.get("rule_name")
            rec = rules_map.setdefault(rname, {})
            rec.setdefault("provenance", []).append({
                "scope": "global", "defined_in": name,
            })
            for field in ("rule_name", "raw_value", "normalized_severity",
                          "numeric_severity", "severity_source", "options"):
                if field in entry:
                    rec[field] = entry[field]

    merged_overrides = []
    for key in order:
        parts, name = parsed[key]
        for ov in parts.get("tool_overrides") or []:
            rec = dict(ov)
            rec["defined_in"] = name
            merged_overrides.append(rec)

    reasons = [
        "extends_static_expansion: local-file extends resolved only "
        "within the provided batch; shareable/plugin/builtin extends "
        "stay not_resolved (zero IO, zero npm access)",
        "eslint_legacy_merge: local rules shadow extended rules with "
        "per-step provenance; override blocks concatenated in chain "
        "order (batch-local composition; ancestor directory search "
        "not performed)",
    ]
    if js_variants_present:
        reasons.append(
            "js_variant_not_parsed: " + ", ".join(js_variants_present)
            + " present in batch (higher real legacy precedence) but "
            "belongs to the JavaScript config family (zero JS "
            "execution); merge composed from parseable JSON/YAML "
            "candidates only"
        )
    return {
        "root_config": parsed[root][1],
        "extends_chain": chain_records,
        "unresolved_extends": unresolved,
        "merged_rules": list(rules_map.values()),
        "merged_overrides": merged_overrides,
        "reasons": reasons,
    }


def _match_eslint_target(contract, norm_t, skipped):
    per_override = []
    for ov in contract.get("tool_overrides") or []:
        via = None
        for p in ov.get("files") or []:
            if _classify_glob(p.replace("\\", "/")) == "invalid":
                if p not in skipped:
                    skipped.append(p)
                continue
            if _glob_match(p, norm_t):
                via = p
                break
        if via is None:
            continue
        excluded_by = None
        for p in ov.get("excluded_files") or []:
            if _classify_glob(p.replace("\\", "/")) == "invalid":
                if p not in skipped:
                    skipped.append(p)
                continue
            if _glob_match(p, norm_t):
                excluded_by = p
                break
        per_override.append({
            "index": ov.get("index"),
            "via_pattern": via,
            "excluded_by": excluded_by,
        })
    matched = [m for m in per_override if not m["excluded_by"]]
    excluded = [m for m in per_override if m["excluded_by"]]
    if excluded:
        status = "excluded"
    elif matched:
        status = "matched"
    else:
        status = "not_matched"
    return {
        "status": status,
        "matched_overrides": [
            {"index": m["index"], "via_pattern": m["via_pattern"]}
            for m in matched
        ],
        "excluded_by": excluded[0]["excluded_by"] if excluded else None,
    }


def _match_babel_target(contract, norm_t, skipped):
    """babel 7 documented-order evaluation: ignore > only >
    overrides test/exclude. A target matching an override's test
    without being excluded is matched; excluded targets are
    excluded; others are processed.
    """
    fp = contract.get("tool_file_patterns") or {}
    ignore = fp.get("ignore") or []
    only = fp.get("only") or []
    for p in ignore:
        if _classify_glob(p.replace("\\", "/")) == "invalid":
            if p not in skipped:
                skipped.append(p)
            continue
        if _glob_match(p, norm_t):
            return {"status": "ignored", "matched_overrides": [],
                    "excluded_by": p}
    only_hit = not only
    for p in only:
        if _classify_glob(p.replace("\\", "/")) == "invalid":
            if p not in skipped:
                skipped.append(p)
            continue
        if _glob_match(p, norm_t):
            only_hit = True
            break
    if only and not only_hit:
        return {"status": "not_in_only", "matched_overrides": [],
                "excluded_by": None}

    # overrides: test hit without exclude shadow -> matched
    per_override = []
    for ov in contract.get("tool_overrides") or []:
        via = None
        for p in ov.get("test") or []:
            if _classify_glob(p.replace("\\", "/")) == "invalid":
                if p not in skipped:
                    skipped.append(p)
                continue
            if _glob_match(p, norm_t):
                via = p
                break
        if via is None:
            continue
        excluded_by = None
        for p in ov.get("exclude") or []:
            if _classify_glob(p.replace("\\", "/")) == "invalid":
                if p not in skipped:
                    skipped.append(p)
                continue
            if _glob_match(p, norm_t):
                excluded_by = p
                break
        per_override.append({"index": ov.get("index"),
                             "via_pattern": via,
                             "excluded_by": excluded_by})
    matched = [m for m in per_override if not m["excluded_by"]]
    excluded = [m for m in per_override if m["excluded_by"]]
    if excluded:
        return {
            "status": "excluded",
            "matched_overrides": [
                {"index": m["index"], "via_pattern": m["via_pattern"]}
                for m in matched
            ],
            "excluded_by": excluded[0]["excluded_by"],
        }
    if matched:
        return {
            "status": "matched",
            "matched_overrides": [
                {"index": m["index"], "via_pattern": m["via_pattern"]}
                for m in matched
            ],
            "excluded_by": None,
        }
    return {"status": "processed", "matched_overrides": [],
            "excluded_by": None}


def match_target_files(contract, target_paths):
    """Static glob matching of target files (no project walk).
    eslint: per-override files/excludedFiles; babel: ignore/only
    evaluation. Invalid globs are skipped and registered.
    """
    skipped = []
    matches = []
    tool = (contract.get("tool_profile") or {}).get("tool")
    for t in target_paths:
        norm_t = str(t).replace("\\", "/")
        if tool == "babel":
            rec = _match_babel_target(contract, norm_t, skipped)
        else:
            rec = _match_eslint_target(contract, norm_t, skipped)
        rec["target"] = t
        matches.append(rec)
    return {"matches": matches, "invalid_patterns_skipped": skipped}


def effective_rules(contract, target_path):
    """Effective rules from a target file's viewpoint: global
    rules plus matched overrides (overrides win), with per-rule
    provenance; excluded targets skip override rules.
    """
    norm_t = str(target_path).replace("\\", "/")
    skipped = []
    match = _match_eslint_target(contract, norm_t, skipped)
    reasons = []
    if match["status"] == "excluded":
        reasons.append(
            f"override_excluded: target excluded by "
            f"{match['excluded_by']} (override rules not applied)"
        )

    rules_map = {}
    matched_indexes = {m["index"] for m in match["matched_overrides"]}
    matrix = contract.get("rule_matrix") or []
    # Global rules first, matched overrides on top
    for scope_group in ["global"] + [
        f"overrides[{i}]" for i in sorted(matched_indexes)
    ]:
        for entry in matrix:
            if entry.get("scope") != scope_group:
                continue
            rname = entry.get("rule_name")
            rec = rules_map.setdefault(rname, {})
            rec.setdefault("provenance", []).append({
                "scope": scope_group,
                "defined_in": entry.get("defined_in"),
            })
            for field in ("rule_name", "raw_value", "normalized_severity",
                          "numeric_severity", "severity_source", "options"):
                if field in entry:
                    rec[field] = entry[field]

    return {
        "target": target_path,
        "status": match["status"],
        "matched_overrides": match["matched_overrides"],
        "effective_rules": list(rules_map.values()),
        "reasons": reasons,
    }


def babel_env_effective(contract, env_name):
    """Babel env effective configuration (pure static composition).
    Root presets/plugins plus env-level entries (babel 7
    documented semantics: env entries append after root;
    same-name entries merge, env options win). Unknown
    environment names return not_found honestly.
    """
    refs = contract.get("tool_references") or []
    reasons = [
        "babel_env_merge_semantics: babel 7 documented behavior - "
        "env-level presets/plugins appended after root; same-name "
        "entries merge with env-level options taking precedence "
        "(static composition; tool version not inspected)"
    ]
    prefix = f"env.{env_name}."

    def _collect(kind):
        root, env_level = [], []
        for r in refs:
            if r.get("kind") != kind:
                continue
            path = r.get("path") or ""
            entry = {
                "raw_value": r.get("raw_value"),
                "options": r.get("options"),
            }
            if path.startswith("env."):
                if path.startswith(prefix):
                    env_level.append(entry)
            else:
                root.append(entry)
        # Same-name merge: env options win (root position kept);
        # others append after root
        for e in env_level:
            for base in root:
                if base["raw_value"] == e["raw_value"]:
                    base["env_merged"] = True
                    if e["options"] is not None:
                        merged = (
                            dict(base["options"])
                            if isinstance(base["options"], dict)
                            else {}
                        )
                        if isinstance(e["options"], dict):
                            merged.update(e["options"])
                        base["options"] = merged
                    break
            else:
                root.append(e)
        return root

    presets = _collect("preset")
    plugins = _collect("plugin")
    envs = contract.get("tool_environments") or []
    if env_name not in envs:
        return {
            "env": env_name,
            "not_found": True,
            "presets": [],
            "plugins": [],
            "reasons": reasons + [
                f"unknown_environment: env {env_name!r} not declared "
                "in this config (no fabricated environment)"
            ],
        }
    return {
        "env": env_name,
        "not_found": False,
        "presets": presets,
        "plugins": plugins,
        "reasons": reasons,
    }
