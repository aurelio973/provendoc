# -*- coding: utf-8 -*-
r"""Shared semantics primitives for the dotfile JSON/YAML parser family.

Extracted verbatim from dotfile_json_yaml_parser.py (zero-drift
split, 2026-11-14): rule-entry parsing and severity tables, tool /
syntax identity maps, JS-syntax hints, YAML token-view
disambiguation, reference registration (presets, plugins, external
projects, env/template refs), glob classification and
parserOptions assembly. This layer only produces shared values; all
contract decisions land in the parser's five-domain envelope.
"""

import datetime
import re

_TOOL_OF_PREFIX = (
    (".eslintrc", "eslint", "legacy"),
    (".babelrc", "babel", "file_relative"),
)


# Suffix -> declared syntax; bare dotfile -> None (content decides)
_DECLARED_OF_SUFFIX = {
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
}


# json/jsonc are one family (JSONC is a dialect, not a mismatch)
_FAMILY_OF = {"json": "json", "jsonc": "json", "yaml": "yaml"}


# CommonJS/ESM config-file markers; conservative regex so YAML
# key names are not matched by accident.
_JS_HINT_RE = re.compile(
    r"\bmodule\.exports\b|\bexports\.\w+\b|\brequire\s*\("
    r"|\bexport\s+default\b"
)


_SEVERITY_BY_STR = {
    "off": ("off", 0), "warn": ("warn", 1), "error": ("error", 2),
}


_SEVERITY_BY_NUM = {0: ("off", 0), 1: ("warn", 1), 2: ("error", 2)}


_PO_LANGUAGE_KEYS = {"ecmaVersion", "sourceType"}


_PO_FILE_REF_KEYS = {"project", "tsconfigRootDir"}


_PO_CAPABILITY_KEYS = {"ecmaFeatures"}


# ${VAR} and {{template}} reference shapes
_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


_TEMPLATE_REF_RE = re.compile(r"\{\{.*?\}\}")


def _parse_rule_entry(name, value, scope, defined_in, yaml_bool=False,
                      yaml_token=None):
    """Structure one rule entry (raw preserved; invalid values
    are reported, never silently fixed).

    For YAML, a second core-schema token disambiguates scalars
    where PyYAML 1.1 and js-yaml disagree: bare ``off`` is a
    valid severity (severity_source=yaml_bool_token); bare
    on/yes/no are invalid severities; literal true/false is an
    invalid shape (JSON parity); timestamp words are invalid
    severities; 1.1-only int shapes go through the string path
    and core-only numeric words (0o2, 1e0) through the number
    path. Without a token view, fall back to lexical reflection.
    """
    rec = {
        "rule_name": name,
        "scope": scope,
        "defined_in": defined_in,
        "raw_value": value,
        "normalized_severity": None,
        "numeric_severity": None,
        "severity_source": None,
        "options": [],
    }
    error = None
    if yaml_bool and isinstance(value, bool):
        # Token disambiguation (second core-schema load)
        if isinstance(yaml_token, str):
            # Bare boolean word: a string under core schema
            rec["severity_source"] = "yaml_bool_token"
            if yaml_token == "off":
                rec["normalized_severity"] = "off"
                rec["numeric_severity"] = 0
            else:
                error = "severity"
        elif yaml_token is None:
            # No token view: fall back to lexical reflection
            rec["severity_source"] = "yaml_bool_token"
            if value is False:
                rec["normalized_severity"] = "off"
                rec["numeric_severity"] = 0
            else:
                error = "severity"
        else:
            # Literal true/false: invalid shape (JSON parity)
            rec["severity_source"] = "invalid"
            error = "shape"
    elif isinstance(value, bool) or value is None \
            or isinstance(value, dict):
        # JSON bool / null / dict -> invalid shape
        rec["severity_source"] = "invalid"
        error = "shape"
    elif isinstance(value, str):
        if (yaml_bool and isinstance(yaml_token, (int, float))
                and not isinstance(yaml_token, bool)):
            # 1.1 kept it a string, core reads a number
            rec["severity_source"] = "number"
            sev = _SEVERITY_BY_NUM.get(yaml_token)
            if sev is None:
                error = "severity"
            else:
                rec["normalized_severity"], rec["numeric_severity"] = sev
        else:
            rec["severity_source"] = "string"
            sev = _SEVERITY_BY_STR.get(value)
            if sev is None:
                error = "severity"
            else:
                rec["normalized_severity"], rec["numeric_severity"] = sev
    elif isinstance(value, int):
        if yaml_bool and isinstance(yaml_token, str):
            # 1.1-only int shape: a string under core schema
            rec["severity_source"] = "string"
            sev = _SEVERITY_BY_STR.get(yaml_token)
            if sev is None:
                error = "severity"
            else:
                rec["normalized_severity"], rec["numeric_severity"] = sev
        else:
            rec["severity_source"] = "number"
            sev = _SEVERITY_BY_NUM.get(value)
            if sev is None:
                error = "severity"
            else:
                rec["normalized_severity"], rec["numeric_severity"] = sev
    elif isinstance(value, list) and value:
        head = value[0]
        head_tok = None
        if isinstance(yaml_token, list) and yaml_token:
            head_tok = yaml_token[0]
        if yaml_bool and isinstance(head, bool):
            # Same token disambiguation for the sequence head
            rec["options"] = list(value[1:])
            if isinstance(head_tok, str):
                rec["severity_source"] = "yaml_bool_token"
                if head_tok == "off":
                    rec["normalized_severity"] = "off"
                    rec["numeric_severity"] = 0
                else:
                    error = "severity"
            elif head_tok is None:
                # No token view: lexical reflection fallback
                rec["severity_source"] = "yaml_bool_token"
                if head is False:
                    rec["normalized_severity"] = "off"
                    rec["numeric_severity"] = 0
                else:
                    error = "severity"
            else:
                # Literal true/false head: invalid shape
                rec["severity_source"] = "invalid"
                error = "shape"
        elif not isinstance(head, bool) and isinstance(
                head, (str, int)):
            rec["options"] = list(value[1:])
            if (yaml_bool and isinstance(head, str)
                    and isinstance(head_tok, (int, float))
                    and not isinstance(head_tok, bool)):
                # Core-only numeric word head -> number path
                rec["severity_source"] = "number"
                sev = _SEVERITY_BY_NUM.get(head_tok)
            elif yaml_bool and isinstance(head, int) \
                    and isinstance(head_tok, str):
                # 1.1-only int shape head -> string path
                rec["severity_source"] = "string"
                sev = _SEVERITY_BY_STR.get(head_tok)
            elif isinstance(head, str):
                rec["severity_source"] = "string"
                sev = _SEVERITY_BY_STR.get(head)
            else:
                rec["severity_source"] = "number"
                sev = _SEVERITY_BY_NUM.get(head)
            if sev is None:
                error = "severity"
            else:
                rec["normalized_severity"], rec["numeric_severity"] = sev
        elif yaml_bool and isinstance(head_tok, str):
            # Exotic head the core view reads back as a string
            rec["options"] = list(value[1:])
            rec["severity_source"] = "string"
            sev = _SEVERITY_BY_STR.get(head_tok)
            if sev is None:
                error = "severity"
            else:
                rec["normalized_severity"], rec["numeric_severity"] = sev
        elif yaml_bool and isinstance(head_tok, datetime.date):
            # Timestamp head: invalid severity, options kept
            rec["options"] = list(value[1:])
            rec["severity_source"] = "yaml_timestamp_token"
            error = "severity"
        else:
            # Invalid head -> invalid shape
            rec["severity_source"] = "invalid"
            error = "shape"
    elif yaml_bool and isinstance(yaml_token, str):
        # Exotic scalar the core view reads back as a string
        rec["severity_source"] = "string"
        sev = _SEVERITY_BY_STR.get(yaml_token)
        if sev is None:
            error = "severity"
        else:
            rec["normalized_severity"], rec["numeric_severity"] = sev
    elif yaml_bool and isinstance(yaml_token, datetime.date):
        # Timestamp scalar: ESLint throws on non-string,
        # non-0/1/2 severities -> invalid severity
        rec["severity_source"] = "yaml_timestamp_token"
        error = "severity"
    else:
        # Empty list / invalid scalar / dict -> invalid shape
        rec["severity_source"] = "invalid"
        error = "shape"
    return rec, error


def _tool_of(file_name: str):
    """File name -> (tool, config_system); unknown -> (None, None)."""
    lower = file_name.lower()
    for prefix, tool, system in _TOOL_OF_PREFIX:
        if lower.startswith(prefix):
            return tool, system
    return None, None


def _declared_syntax_of(file_name: str):
    """Variant suffix -> declared syntax; bare dotfile -> None."""
    lower = file_name.lower()
    for suffix, syntax in _DECLARED_OF_SUFFIX.items():
        if lower.endswith(suffix):
            # Guard: stem must still be an eslintrc/babelrc name
            if lower.startswith((".eslintrc", ".babelrc")):
                return syntax
    return None


def _is_jsonc_dialect(contract: dict) -> bool:
    """Detect JSONC dialect from the delegated JsonParser contract."""
    if contract.get("reason_code") == "JSONC_DIALECT_IN_JSON":
        return True
    return any(
        "jsonc_dialect_in_json" in r
        for r in contract.get("loss", {}).get("reasons", [])
    )


def _ref(kind, raw_value, classification, path, options=None):
    """Build one reference_candidate record (never executed)."""
    rec = {
        "kind": kind,
        "raw_value": raw_value,
        "classification": classification,
        "resolution_status": "not_resolved",
        "executed": False,
        "path": path,
    }
    if options is not None:
        rec["options"] = options
    return rec


def _classify_extends(value: str) -> str:
    """Classify an ESLint extends value."""
    if value.startswith("eslint:"):
        return "built_in_config"
    if value.startswith("plugin:"):
        return "plugin_config"
    if value.startswith(("./", "../")):
        return "local_file"
    return "shareable_config"


_JS_CORE_LOADER = None


def _get_js_core_loader():
    """A Loader approximating js-yaml v4 DEFAULT_SCHEMA scalar
    semantics (built lazily).

    ESLint loads YAML with plain ``yaml.load`` (core schema plus
    implicit timestamp/merge). The only real differences from
    PyYAML 1.1 are bool/int/float lexing: bool accepts only
    true/false; int accepts 0b/underscores but no sexagesimal;
    float accepts only dotted/exponent forms. This loader swaps
    just those three resolvers; timestamps lex identically on
    both sides, so they are left untouched.
    """
    global _JS_CORE_LOADER
    if _JS_CORE_LOADER is None:
        import yaml

        class _JsCoreLoader(yaml.SafeLoader):
            pass

        # Timestamps lex identically on both sides; swap only
        # the bool/int/float resolvers, which really differ
        _JsCoreLoader.yaml_implicit_resolvers = {
            ch: [(t, r) for t, r in resolvers
                 if t not in (
                     "tag:yaml.org,2002:bool",
                     "tag:yaml.org,2002:int",
                     "tag:yaml.org,2002:float",
                 )]
            for ch, resolvers
            in yaml.SafeLoader.yaml_implicit_resolvers.items()
        }
        _JsCoreLoader.add_implicit_resolver(
            "tag:yaml.org,2002:bool",
            re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
            list("tTfF"),
        )
        # core-schema int lexing: decimal (underscores allowed),
        # 0x/0o/0b prefixes, no sexagesimal; custom constructor
        # because PyYAML's does not accept every core form
        _JsCoreLoader.add_implicit_resolver(
            "tag:yaml.org,2002:int",
            re.compile(
                r"^[-+]?(?:0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*"
                r"|0[oO][0-7](?:_?[0-7])*"
                r"|0[bB][01](?:_?[01])*"
                r"|[0-9](?:_?[0-9])*)$"),
            list("0123456789+-"),
        )

        def _core_int_constructor(loader, node):
            s = loader.construct_scalar(node)
            sign = 1
            if s and s[0] in "+-":
                if s[0] == "-":
                    sign = -1
                s = s[1:]
            p = s[:2].lower()
            if p in ("0x", "0o", "0b"):
                return sign * int(
                    s[2:].replace("_", ""),
                    {"0x": 16, "0o": 8, "0b": 2}[p],
                )
            return sign * int(s.replace("_", ""), 10)

        _JsCoreLoader.add_constructor(
            "tag:yaml.org,2002:int", _core_int_constructor,
        )
        # core-schema float: dotted/exponent forms and
        # ±.inf/.nan, no sexagesimal; the default constructor
        # already handles these
        _JsCoreLoader.add_implicit_resolver(
            "tag:yaml.org,2002:float",
            re.compile(
                r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)"
                r"(?:[eE][-+]?[0-9]+)?$"
                r"|^[-+]?\.(?:inf|Inf|INF)$"
                r"|^\.(?:nan|NaN|NAN)$"),
            list("0123456789+-."),
        )
        _JS_CORE_LOADER = _JsCoreLoader
    return _JS_CORE_LOADER


def _coerce_view_date_keys(node):
    """Render date/datetime keys in the view tree as ISO strings
    (values untouched).

    js-yaml coerces date keys to locale-dependent Date strings;
    ISO keeps the source shape. If a date key and its ISO string
    twin coexist, lengths mismatch and _sem_key_pairs falls back
    to the raw keys rather than guessing.
    """
    if isinstance(node, dict):
        return {
            (k.isoformat() if isinstance(k, datetime.date) else k):
                _coerce_view_date_keys(v)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_coerce_view_date_keys(x) for x in node]
    return node


def _yaml_token_view(text):
    """Second YAML load with js-yaml core semantics, for 1.1
    lexing disambiguation. Returns None on failure; callers then
    fall back to lexical reflection.
    """
    try:
        import yaml

        return _coerce_view_date_keys(yaml.load(
            text.lstrip("\ufeff"), Loader=_get_js_core_loader(),
        ))
    except Exception:
        return None


def _view_get(view, *path):
    """Fetch a value from the token view by path; None if absent."""
    cur = view
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif (isinstance(cur, list) and isinstance(p, int)
              and 0 <= p < len(cur)):
            cur = cur[p]
        else:
            return None
    return cur


def _sem(sd_value, tok):
    """Semantic value: prefer the core-view token when the
    document is YAML and the view is available, else the main
    load value."""
    return tok if tok is not None else sd_value


def _sem_key_pairs(raw_map, view_map):
    """Key-name disambiguation: yield (core_key, raw_value)
    pairs. PyYAML 1.1 lexes bare scalar keys into bool/number/
    date; core keeps them strings. Falls back to raw keys when
    the view is missing or lengths mismatch.
    """
    if not isinstance(raw_map, dict):
        return []
    if (not isinstance(view_map, dict)
            or len(view_map) != len(raw_map)):
        return list(raw_map.items())
    return [
        (ck, rv) for ck, (_, rv) in
        zip(view_map.keys(), raw_map.items())
    ]


def _flatten_str_list(value, invalid_out=None):
    """str -> [str]; list -> its string elements (non-string
    entries counted into invalid_out); other non-None scalars
    are counted too; None means the key is absent.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items = [x for x in value if isinstance(x, str)]
        if invalid_out is not None:
            bad = len(value) - len(items)
            if bad:
                invalid_out.append(bad)
        return items
    if value is not None and invalid_out is not None:
        # Key present but value is not str/list -> counted
        invalid_out.append(1)
    return None


def _register_preset_or_plugin(value, kind, path, refs):
    """Register a babel preset/plugin: string or [name, options]."""
    if isinstance(value, str):
        refs.append(_ref(
            kind, value, f"{kind}_package", path,
        ))
    elif isinstance(value, list) and value and isinstance(value[0], str):
        options = value[1] if len(value) > 1 else None
        refs.append(_ref(
            kind, value[0], f"{kind}_package", path, options=options,
        ))


def _count_invalid_ref_entries(value):
    """Count unregistrable preset/plugin entries (anything but a
    string or [string, ...])."""
    if value is None or isinstance(value, str):
        return 0
    if isinstance(value, list):
        return sum(
            1 for x in value
            if not (isinstance(x, str)
                    or (isinstance(x, list) and x
                        and isinstance(x[0], str)))
        )
    return 1


def _scan_external_project_references(node, path, out):
    """Scan the settings tree for external project references
    (registered, never read or resolved)."""
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{path}.{k}" if path else str(k)
            if k == "project" and isinstance(v, str):
                out.append({
                    "path": child, "raw_value": v, "resolved": False,
                })
            else:
                _scan_external_project_references(v, child, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_external_project_references(v, f"{path}[{i}]", out)
    elif isinstance(node, str) and node.startswith(("./", "../")):
        out.append({
            "path": path, "raw_value": node, "resolved": False,
        })


def _build_parser_options(raw, sem=None):
    """Classify parserOptions keys into literal / file reference /
    parser capability / language mode. Classification consumes
    the semantic keys; ``raw`` preserves the original evidence.
    """
    if sem is None:
        sem = raw
    if not isinstance(raw, dict):
        return None, []
    po = {
        "raw": raw,
        "language_mode_keys": [],
        "file_reference_keys": [],
        "parser_capability_keys": [],
        "literal_keys": [],
    }
    file_refs = []
    for key in sem if isinstance(sem, dict) else []:
        if key in _PO_LANGUAGE_KEYS:
            po["language_mode_keys"].append(key)
        elif key in _PO_FILE_REF_KEYS:
            po["file_reference_keys"].append(key)
            file_refs.append(key)
        elif key in _PO_CAPABILITY_KEYS:
            po["parser_capability_keys"].append(key)
        else:
            po["literal_keys"].append(key)
    return po, file_refs


def _classify_glob(pattern):
    """Static glob classification (no filesystem access)."""
    if not pattern:
        return "invalid"
    if pattern.count("[") != pattern.count("]"):
        return "invalid"
    if pattern.count("{") != pattern.count("}"):
        return "invalid"
    if not any(ch in pattern for ch in ("*", "?", "[", "{")):
        return "literal"
    if pattern.startswith("**/"):
        return "suffix_glob"
    if "/**/" in pattern:
        return "path_glob"
    if pattern.endswith("/**"):
        return "directory_glob"
    return "glob"


def _collect_glob_patterns(value, scope, out, invalid_out=None):
    """Collect globs into file_patterns (raw, normalized, kind,
    match_status=unknown)."""
    items = _flatten_str_list(value, invalid_out)
    if not items:
        return 0
    for p in items:
        norm = p.replace("\\", "/")
        if norm.startswith("./"):
            norm = norm[2:]
        kind = _classify_glob(norm)
        out.append({
            "raw_pattern": p,
            "normalized_pattern": norm,
            "pattern_kind": None if kind == "invalid" else kind,
            "match_status": "invalid" if kind == "invalid" else "unknown",
            "scope": scope,
        })
    return len(items)


def _scan_template_references(sd):
    """Scan string values for ${VAR} / {{tpl}} references."""
    out = []

    def _walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            if _ENV_REF_RE.search(node):
                out.append({
                    "path": path, "raw_value": node,
                    "kind": "environment_reference",
                })
            elif _TEMPLATE_REF_RE.search(node):
                out.append({
                    "path": path, "raw_value": node,
                    "kind": "template_value",
                })

    _walk(sd, "")
    return out
