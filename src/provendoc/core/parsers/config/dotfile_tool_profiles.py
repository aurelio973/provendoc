# -*- coding: utf-8 -*-
r"""ESLint/Babel tool-profile builders for the dotfile JSON/YAML family.

Extracted verbatim from dotfile_json_yaml_parser.py (zero-drift
split, 2026-11-14). The builders attach additive tool-profile keys
on top of the JSON/YAML family contracts (zero drift); the
disclosure step reports where PyYAML (YAML 1.1) and js-yaml (the
real loader) disagree on scalar lexing. Invalid values are
disclosed raw — never silently fixed or dropped.
"""

import datetime
import re

from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.dotfile_semantics import (
    _build_parser_options,
    _classify_extends,
    _collect_glob_patterns,
    _count_invalid_ref_entries,
    _flatten_str_list,
    _parse_rule_entry,
    _ref,
    _register_preset_or_plugin,
    _scan_external_project_references,
    _sem,
    _sem_key_pairs,
    _view_get,
)


def _build_eslint_profile(sd: dict, file_name: str, detected=None,
                          yaml_view=None):
    """Register the ESLint legacy tool profile (reads structured_data
    only; invalid shapes are counted and disclosed, never silently
    dropped).
    """
    refs = []
    # Non-string list entries are counted, not silently dropped
    bad_entries: list[int] = []
    invalid_ref_entries = 0
    # Invalid container/scalar shapes are counted
    invalid_field_shapes = []
    yaml_bool = detected == "yaml"
    yaml_bool_hits = 0
    # Reference/glob fields consume core-view semantics
    def _tok(*path):
        return _view_get(yaml_view, *path) if yaml_bool else None

    # parser: on is a string reference; parser: 0o2 is a number
    parser_sem = _sem(sd.get("parser"), _tok("parser"))
    if isinstance(parser_sem, str):
        refs.append(_ref(
            "parser", parser_sem, "parser_package", "parser",
        ))
    elif parser_sem is not None:
        # Non-string shape: disclosed
        invalid_field_shapes.append("parser")

    plugins_sem = _sem(sd.get("plugins"), _tok("plugins"))
    if isinstance(plugins_sem, list):
        for i, p in enumerate(plugins_sem):
            if isinstance(p, str):
                refs.append(_ref(
                    "plugin", p, "plugin_package", f"plugins[{i}]",
                ))
            else:
                invalid_ref_entries += 1
    elif isinstance(plugins_sem, str):
        refs.append(_ref(
            "plugin", plugins_sem, "plugin_package", "plugins",
        ))
    elif plugins_sem is not None:
        # Non-list/str scalar: disclosed
        invalid_field_shapes.append("plugins")

    extends_items = _flatten_str_list(
        _sem(sd.get("extends"), _tok("extends")), bad_entries,
    )
    if extends_items:
        for i, x in enumerate(extends_items):
            refs.append(_ref(
                "extends", x, _classify_extends(x), f"extends[{i}]",
            ))

    # Static glob collection (overrides files/excludedFiles)
    file_patterns: list[dict] = []

    overrides = []
    raw_overrides = sd.get("overrides")
    if raw_overrides is not None and not isinstance(raw_overrides, list):
        invalid_field_shapes.append("overrides")
    if isinstance(raw_overrides, list):
        for i, ov in enumerate(raw_overrides):
            if not isinstance(ov, dict):
                continue
            # Override-level parser/plugins references
            ov_parser = _sem(ov.get("parser"),
                             _tok("overrides", i, "parser"))
            if isinstance(ov_parser, str):
                refs.append(_ref(
                    "parser", ov_parser, "parser_package",
                    f"overrides[{i}].parser",
                ))
            elif ov_parser is not None:
                # Non-string scalar: disclosed
                invalid_field_shapes.append(f"overrides[{i}].parser")
            # Same shape as root-level plugins (original indexes;
            # filtering first would shift them)
            ov_plugins = _sem(ov.get("plugins"),
                              _tok("overrides", i, "plugins"))
            if isinstance(ov_plugins, list):
                for j, p in enumerate(ov_plugins):
                    if isinstance(p, str):
                        refs.append(_ref(
                            "plugin", p, "plugin_package",
                            f"overrides[{i}].plugins[{j}]",
                        ))
                    else:
                        invalid_ref_entries += 1
            elif isinstance(ov_plugins, str):
                refs.append(_ref(
                    "plugin", ov_plugins, "plugin_package",
                    f"overrides[{i}].plugins",
                ))
            elif ov_plugins is not None:
                invalid_field_shapes.append(f"overrides[{i}].plugins")
            ov_cf = _sem(ov.get("excludedFiles"),
                         _tok("overrides", i, "excludedFiles"))
            if ov_cf is None:
                # Explicit None check (an or-chain would map [] to
                # the snake_case key)
                ov_cf = _sem(ov.get("excluded_files"),
                             _tok("overrides", i, "excluded_files"))
            ov_files = _sem(ov.get("files"),
                            _tok("overrides", i, "files"))
            rec = {
                "index": i,
                "files": _flatten_str_list(ov_files,
                                           bad_entries) or [],
                "excluded_files": (
                    _flatten_str_list(ov_cf, bad_entries) or []
                ),
            }
            ov_rules = ov.get("rules")
            rec["rule_count"] = len(ov_rules) if isinstance(ov_rules, dict) else 0
            overrides.append(rec)
            _collect_glob_patterns(
                ov_files, f"overrides[{i}].files", file_patterns,
                bad_entries,
            )
            _collect_glob_patterns(
                ov_cf, f"overrides[{i}].excludedFiles", file_patterns,
                bad_entries,
            )

    # rules raw dict plus the structured rule matrix
    rules = sd.get("rules")
    if rules is not None and not isinstance(rules, dict):
        invalid_field_shapes.append("rules")
    tool_rules = dict(rules) if isinstance(rules, dict) else {}
    matrix = []
    invalid_severity = 0
    invalid_shape = 0
    # Key disambiguation: rule_name takes the core key,
    # raw_value keeps the 1.1 value
    for name, value in _sem_key_pairs(tool_rules, _tok("rules")):
        tok = _view_get(yaml_view, "rules", name) if yaml_bool else None
        rec, err = _parse_rule_entry(
            name, value, "global", file_name, yaml_bool, tok,
        )
        if rec.get("severity_source") == "yaml_bool_token":
            yaml_bool_hits += 1
        matrix.append(rec)
        if err == "severity":
            invalid_severity += 1
        elif err == "shape":
            invalid_shape += 1
    if isinstance(raw_overrides, list):
        for i, ov in enumerate(raw_overrides):
            ov_rules = ov.get("rules") if isinstance(ov, dict) else None
            if not isinstance(ov_rules, dict):
                continue
            for name, value in _sem_key_pairs(
                ov_rules, _tok("overrides", i, "rules"),
            ):
                tok = _view_get(
                    yaml_view, "overrides", i, "rules", name,
                ) if yaml_bool else None
                rec, err = _parse_rule_entry(
                    name, value, f"overrides[{i}]", file_name, yaml_bool,
                    tok,
                )
                if rec.get("severity_source") == "yaml_bool_token":
                    yaml_bool_hits += 1
                matrix.append(rec)
                if err == "severity":
                    invalid_severity += 1
                elif err == "shape":
                    invalid_shape += 1

    env = sd.get("env")
    if env is not None and not isinstance(env, dict):
        invalid_field_shapes.append("env")
    # Env name key disambiguation
    env_pairs = _sem_key_pairs(env, _tok("env"))
    envs = [name for name, _ in env_pairs] if isinstance(env, dict) else []

    # Env values: name + enabled boolean as-is; non-bool disclosed
    env_entries = []
    invalid_env_values = 0
    if isinstance(env, dict):
        for name, value in env_pairs:
            entry = {"name": name, "enabled": value}
            tok = _view_get(yaml_view, "env", name) \
                if yaml_bool else None
            if isinstance(value, bool) and isinstance(tok, str):
                # Bare boolean word: a string under core schema
                # -> invalid env value
                entry["valid"] = False
                invalid_env_values += 1
            elif isinstance(value, bool):
                entry["valid"] = True
            else:
                entry["valid"] = False
                invalid_env_values += 1
            env_entries.append(entry)

    profile = {}
    root_value_invalid = False
    if "root" in sd:
        # root preserved as-is (no bool() coercion); non-boolean
        # values are disclosed
        rv = sd.get("root")
        profile["root"] = rv
        r_tok = _view_get(yaml_view, "root") if yaml_bool else None
        root_value_invalid = not isinstance(rv, bool) or (
            isinstance(rv, bool) and isinstance(r_tok, str)
        )

    # parserOptions classification
    po_raw = sd.get("parserOptions")
    if po_raw is not None and not isinstance(po_raw, dict):
        invalid_field_shapes.append("parserOptions")
        po_raw = None
    po, po_file_refs = _build_parser_options(
        po_raw, _sem(po_raw, _tok("parserOptions")),
    )

    # ---- Secondary top-level fields, registered as-is ----
    globals_raw = sd.get("globals")
    if globals_raw is not None and not isinstance(globals_raw, dict):
        invalid_field_shapes.append("globals")
    tool_globals = (
        dict(globals_raw) if isinstance(globals_raw, dict)
        else None
    )
    settings_raw = sd.get("settings")
    if settings_raw is not None and not isinstance(settings_raw, dict):
        # Non-container settings shape: disclosed
        invalid_field_shapes.append("settings")
    tool_settings = (
        {"raw": settings_raw} if isinstance(settings_raw, dict)
        else (None if settings_raw is None else {"raw": settings_raw})
    )
    # Scan the core tree (keys and values aligned); raw evidence
    # stays in tool_settings/structured_data
    settings_sem = _sem(settings_raw, _tok("settings"))
    ext_project_refs: list[dict] = []
    if isinstance(settings_sem, dict):
        _scan_external_project_references(settings_sem, "settings",
                                          ext_project_refs)
    processor_sem = _sem(sd.get("processor"), _tok("processor"))
    if isinstance(processor_sem, str):
        refs.append(_ref(
            "processor", processor_sem, "processor_plugin", "processor",
        ))
    elif processor_sem is not None:
        # Non-string scalar: disclosed
        invalid_field_shapes.append("processor")
    # Core-view semantics for glob fields
    ignore_patterns = _sem(sd.get("ignorePatterns"),
                           _tok("ignorePatterns"))
    if ignore_patterns is None:
        # Explicit None check (an or-chain would map [] to the
        # snake_case key)
        ignore_patterns = _sem(sd.get("ignore_patterns"),
                               _tok("ignore_patterns"))
    if ignore_patterns is not None:
        _collect_glob_patterns(
            ignore_patterns, "ignorePatterns", file_patterns,
            bad_entries,
        )
    rudd = sd.get("reportUnusedDisableDirectives")

    return {
        "tool_references": refs,
        "tool_overrides": overrides,
        "tool_rules": tool_rules,
        "tool_environments": envs,
        "tool_env_entries": env_entries,
        "invalid_env_value_count": invalid_env_values,
        "root_flag": profile,
        "rule_matrix": matrix,
        "invalid_severity_count": invalid_severity,
        "invalid_shape_count": invalid_shape,
        "parser_options": po,
        "parser_options_file_refs": po_file_refs,
        "file_patterns": file_patterns,
        "tool_globals": tool_globals,
        "tool_settings": tool_settings,
        "tool_report_unused_disable_directives": rudd,
        "external_project_references": ext_project_refs,
        "invalid_list_entry_count": sum(bad_entries),
        "invalid_ref_entry_count": invalid_ref_entries,
        "root_value_invalid": root_value_invalid,
        "invalid_field_shapes": invalid_field_shapes,
        "yaml_bool_token_count": yaml_bool_hits,
    }


def _build_babel_profile(sd: dict, yaml_view=None):
    """Register the babel file-relative tool profile (core-view
    semantics for YAML; raw evidence preserved)."""
    refs: list[dict] = []
    # Non-string entries and unregistrable shapes are counted
    bad_entries: list[int] = []
    invalid_ref_entries = 0

    def _tok(*path):
        return _view_get(yaml_view, *path) if yaml_view is not None else None

    presets = _sem(sd.get("presets"), _tok("presets"))
    if isinstance(presets, list):
        for i, p in enumerate(presets):
            _register_preset_or_plugin(p, "preset", f"presets[{i}]", refs)
    elif isinstance(presets, str):
        _register_preset_or_plugin(presets, "preset", "presets", refs)
    invalid_ref_entries += _count_invalid_ref_entries(presets)

    plugins = _sem(sd.get("plugins"), _tok("plugins"))
    if isinstance(plugins, list):
        for i, p in enumerate(plugins):
            _register_preset_or_plugin(p, "plugin", f"plugins[{i}]", refs)
    elif isinstance(plugins, str):
        _register_preset_or_plugin(plugins, "plugin", "plugins", refs)
    invalid_ref_entries += _count_invalid_ref_entries(plugins)

    env = sd.get("env")
    # Env name key disambiguation (same as the ESLint side)
    env_pairs = _sem_key_pairs(env, _tok("env"))
    envs = [name for name, _ in env_pairs] if isinstance(env, dict) else []

    # env-embedded presets/plugins references (still zero execution)
    if isinstance(env, dict):
        for env_name, env_cfg in env.items():
            if not isinstance(env_cfg, dict):
                continue
            for key, kind in (("presets", "preset"), ("plugins", "plugin")):
                vals = _sem(env_cfg.get(key),
                            _tok("env", env_name, key))
                if isinstance(vals, list):
                    for j, v in enumerate(vals):
                        _register_preset_or_plugin(
                            v, kind, f"env.{env_name}.{key}[{j}]", refs,
                        )
                elif isinstance(vals, str):
                    _register_preset_or_plugin(
                        vals, kind, f"env.{env_name}.{key}", refs,
                    )
                invalid_ref_entries += _count_invalid_ref_entries(vals)

    # babel overrides: test/exclude patterns plus embedded references
    file_patterns: list[dict] = []
    overrides = []
    raw_overrides = sd.get("overrides")
    if isinstance(raw_overrides, list):
        for i, ov in enumerate(raw_overrides):
            if not isinstance(ov, dict):
                continue
            ov_env = ov.get("env")
            ov_test = _sem(ov.get("test"),
                           _tok("overrides", i, "test"))
            ov_exclude = _sem(ov.get("exclude"),
                              _tok("overrides", i, "exclude"))
            ov_env_names = [
                name for name, _ in _sem_key_pairs(
                    ov_env, _tok("overrides", i, "env"),
                )
            ] if isinstance(ov_env, dict) else []
            rec = {
                "index": i,
                "test": _flatten_str_list(ov_test) or [],
                "exclude": _flatten_str_list(ov_exclude) or [],
                "env": ov_env_names,
            }
            for key, kind in (("presets", "preset"), ("plugins", "plugin")):
                vals = _sem(ov.get(key),
                            _tok("overrides", i, key))
                if isinstance(vals, list):
                    for j, v in enumerate(vals):
                        _register_preset_or_plugin(
                            v, kind, f"overrides[{i}].{key}[{j}]", refs,
                        )
                elif isinstance(vals, str):
                    _register_preset_or_plugin(
                        vals, kind, f"overrides[{i}].{key}", refs,
                    )
                invalid_ref_entries += _count_invalid_ref_entries(vals)
            if isinstance(ov_env, dict):
                for env_name, env_cfg in ov_env.items():
                    if not isinstance(env_cfg, dict):
                        continue
                    for key, kind in (("presets", "preset"),
                                      ("plugins", "plugin")):
                        vals = _sem(
                            env_cfg.get(key),
                            _tok("overrides", i, "env", env_name, key),
                        )
                        if isinstance(vals, list):
                            for j, v in enumerate(vals):
                                _register_preset_or_plugin(
                                    v, kind,
                                    f"overrides[{i}].env.{env_name}"
                                    f".{key}[{j}]", refs,
                                )
                        # Bare string, same as the root/env level
                        elif isinstance(vals, str):
                            _register_preset_or_plugin(
                                vals, kind,
                                f"overrides[{i}].env.{env_name}.{key}",
                                refs,
                            )
                        invalid_ref_entries += _count_invalid_ref_entries(
                            vals
                        )
            overrides.append(rec)
            _collect_glob_patterns(
                ov_test, f"overrides[{i}].test", file_patterns,
                bad_entries,
            )
            _collect_glob_patterns(
                ov_exclude, f"overrides[{i}].exclude",
                file_patterns, bad_entries,
            )

    # babelrcRoots / configFile references (registered, not read)
    babelrc_roots = _sem(sd.get("babelrcRoots"),
                         _tok("babelrcRoots"))
    if isinstance(babelrc_roots, (list, str)):
        refs.append(_ref(
            "babelrc_roots", babelrc_roots, "workspace_root_pattern",
            "babelrcRoots",
        ))
    elif isinstance(babelrc_roots, datetime.date):
        # Date value: not a string, counted
        bad_entries.append(1)
    config_file = _sem(sd.get("configFile"), _tok("configFile"))
    if isinstance(config_file, str):
        refs.append(_ref(
            "config_file", config_file, "external_config",
            "configFile",
        ))
    elif isinstance(config_file, datetime.date):
        # Date value: same as babelrcRoots
        bad_entries.append(1)

    # Static glob collection (ignore/only)
    ignore_sem = _sem(sd.get("ignore"), _tok("ignore"))
    only_sem = _sem(sd.get("only"), _tok("only"))
    _collect_glob_patterns(ignore_sem, "ignore", file_patterns,
                            bad_entries)
    _collect_glob_patterns(only_sem, "only", file_patterns,
                            bad_entries)

    fp = {}
    for key, sem_val in (("ignore", ignore_sem), ("only", only_sem)):
        # Counted above; not repeated here
        patterns = _flatten_str_list(sem_val)
        if patterns is not None:
            fp[key] = patterns

    return {
        "tool_references": refs,
        "tool_overrides": overrides,
        "tool_rules": {},
        "tool_environments": envs,
        "file_patterns": file_patterns,
        "tool_file_patterns": fp,
        "invalid_list_entry_count": sum(bad_entries),
        "invalid_ref_entry_count": invalid_ref_entries,
    }


def _disclose_js_yaml_load_divergences(reasons, contract, structured):
    """Disclose loader-level divergences between PyYAML and the
    js-yaml consumer for YAML dotfiles.

    js-yaml throws on the second local occurrence of a mapping
    key where PyYAML keeps last-wins; merge sources that are
    not objects throw in both; Date merge sources are accepted
    by js-yaml (zero-key merge) but rejected by PyYAML. Only
    already-registered evidence is consumed (no second parse);
    reasons are appended additively, reusing existing vocab
    codes.
    """
    # ---- Local duplicate keys: js-yaml fails the whole file ----
    dup_groups = []
    seen_dup = set()
    for e in contract.get("entries") or []:
        chain = e.get("override_chain")
        if not isinstance(chain, list):
            continue
        local_occ = sum(
            1 for c in chain
            if isinstance(c, dict) and "merged_from" not in c
        )
        if local_occ < 2:
            continue
        gkey = (
            e.get("doc_index", 0),
            tuple(str(s) for s in (e.get("path") or [])),
        )
        if gkey in seen_dup:
            continue
        seen_dup.add(gkey)
        dup_groups.append(cc.path_str(gkey[1]) or "<root>")
    if dup_groups:
        dup_show = list(dict.fromkeys(dup_groups))
        reasons.append(
            "INVALID_DOTFILE_SYNTAX: "
            f"{len(seen_dup)} duplicated mapping key group(s) ["
            + ", ".join(dup_show)
            + "]: PyYAML loads with duplicate_key_semantics="
            "last_wins, but js-yaml 4.1.0 (yaml.load without "
            "options -- the @eslint/eslintrc loadYAMLConfigFile "
            "form) throws 'duplicated mapping key' on the "
            "second local occurrence in a mapping; the whole "
            "file fails to load for the reference consumer (an "
            "explicit key overriding a merge-sourced key is "
            "exempt via overridableKeys)"
        )

    # ---- Non-object merge sources: both throw / js-yaml swallows ----
    # Gate on real PyYAML errors (recovered _error text contains
    # 'for merging'); a quoted "<<" key is a normal key and never
    # triggers this
    errors = []
    seen = set()
    stack = [structured]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            for k, v in node.items():
                if k == "_error" and isinstance(v, str):
                    errors.append(v)
                else:
                    stack.append(v)
        elif isinstance(node, list):
            if id(node) in seen:
                continue
            seen.add(id(node))
            for v in node:
                stack.append(v)
    merge_errs = [e for e in errors if "for merging" in e]
    if not merge_errs:
        return

    def _merge_value_at(path):
        node = structured
        for seg in path:
            try:
                if isinstance(node, list):
                    node = node[int(seg)]
                elif isinstance(node, dict):
                    node = node[seg]
                else:
                    return None
            except (KeyError, IndexError, TypeError, ValueError):
                return None
        if isinstance(node, dict) and "<<" in node:
            return node["<<"]
        return None

    containers = [
        c for c in (contract.get("containers") or [])
        if isinstance(c, dict) and c.get("line_start") is not None
    ]

    def _path_of_line(lineno):
        best = None
        for c in containers:
            ls = c.get("line_start")
            le = c.get("line_end")
            if ls is None or ls > lineno:
                continue
            if le is not None and le < lineno:
                continue
            key = (ls, len(c.get("path") or []))
            if best is None or key > best[0]:
                best = (key, c.get("path") or [])
        return best[1] if best is not None else []

    merge_lines = set()
    for e in merge_errs:
        for m in re.finditer(
            r"line (\d+), column \d+:\s*\n\s*([^\n]*)", e,
        ):
            if re.search(r"(?<![\w'\"])<<(?![\w'\"])", m.group(2)):
                # PyYAML lines are 1-based, container lines 0-based;
                # the container ranges absorb the small offset
                merge_lines.add(int(m.group(1)) - 1)
    if merge_lines:
        labels = []
        for lineno in sorted(merge_lines):
            labels.append(
                (cc.path_str(_path_of_line(lineno)) or "<root>", lineno)
            )
    else:
        # Fallback when snapshot line extraction fails
        labels = [
            (r.split(":", 1)[1], 0)
            for r in reasons
            if r.startswith("merge_source_not_mapping:")
        ]
    merge_reject = []
    merge_date = []
    for label, _lineno in labels:
        v = _merge_value_at(label.split(".") if label != "<root>" else [])
        if isinstance(v, (datetime.date, datetime.datetime)):
            merge_date.append(label)
        else:
            merge_reject.append(label)
    if merge_reject:
        reasons.append(
            "INVALID_DOTFILE_SYNTAX: non-object merge key "
            "source(s) ["
            + ", ".join(dict.fromkeys(merge_reject))
            + "]: PyYAML raises ConstructorError 'expected a "
            "mapping or list of mappings for merging' (tolerant "
            "recovery, structured_data._error) and js-yaml 4.1.0 "
            "throws 'cannot merge mappings; the provided source "
            "object is unacceptable'; the whole file fails to "
            "load for the reference consumer"
        )
    if merge_date:
        reasons.append(
            "merge_source_not_mapping: timestamp merge source(s) ["
            + ", ".join(dict.fromkeys(merge_date))
            + "] diverge between loaders: PyYAML raises "
            "ConstructorError (tolerant recovery, "
            "structured_data._error) but js-yaml 4.1.0 accepts "
            "Date sources as objects with zero enumerable keys "
            "-- zero-key merge, '<<' evaporates and the load "
            "succeeds; the '<<' INVALID_RULE_SHAPE registration "
            "is recovery-side evidence, the reference consumer "
            "never sees a '<<' rule"
        )
