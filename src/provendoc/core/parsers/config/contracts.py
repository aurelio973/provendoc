# ============================================================
# Structured Config Domain
# Config Entry Contract (parser → representation → quality evaluation)
# ============================================================
"""ConfigEntry standard contract — the fact layer of the config domain (contract v1).

Constitutional principle:
    ConfigQuality does not judge whether a configuration is
    business-correct; it judges whether configuration facts are
    correctly parsed, preserved, represented, and traceable.

This module defines the `metadata["config_entries"]` contract structure
produced by parsers and consumed by the representation layer
(three-granularity chunking) and ConfigQuality (two-layer quality
evaluation).

Contract structure (mounted at Document.metadata["config_entries"]):
    {
      "contract_version": "config_entry_v1",
      "format": "yaml" | "json" | "toml" | "ini",
      "duplicate_key_semantics": "last_wins" | "rejected" | "override_chain",
      "entries":     [entry_dict, ...],     # scalar leaves + explicit null
      "containers":  [container_dict, ...], # dict/list containers (incl. empty)
      "comments":    [comment_dict, ...],   # comments (with binding info)
      "references":  [ref_dict, ...],       # all references (with five-state resolution)
      "loss": {
          "line_numbers": bool,             # whether line numbers are provided
          "comments": bool,                 # whether comments are provided
          "reasons": [str, ...],            # missing/degraded reasons
      },
    }

entry_dict:
    {
      "path": ["server", "port"],       # path segments (list indices are decimal strings)
      "path_str": "server.port",
      "key": "port",
      "kind": "entry",
      "raw_value": "8080",              # source text (scalar lexeme)
      "value": 8080,                     # parsed value (JSON-safe)
      "value_state": "scalar",           # see VALUE_STATE_*
      "line_start": 2, "line_end": 2,    # 0-based; None when missing
      "description": "HTTP port",       # bound comment text; None if unbound
      "description_lines": [1, 1],
      "duplicate": False,
      "override_chain": None,            # duplicate key: [{"value":..., "line":...}, ...]
      "references": [ref_dict, ...],     # references detected in raw_value (with status)
      "doc_index": 0,                    # document ordinal in multi-document files
      "source_precision": None,          # v1.1: None = source fact; "line_only" = derived projection
    }

container_dict:
    {
      "path": ["server"],
      "path_str": "server",
      "kind": "container",
      "container_type": "dict" | "list",
      "value_state": "container" | "empty_dict" | "empty_list",
      "child_count": 3,
      "implicit": False,                 # implicit: created by dotted keys / nested sections
      "line_start": 1, "line_end": 4,
      "description": ...,
      "duplicate": False,
      "override_chain": None,
      "doc_index": 0,
    }

comment_dict:
    {
      "kind": "block" | "inline",
      "text": "...",                     # text with comment markers stripped
      "raw": "# ...",
      "line_start": 1, "line_end": 1,
      "bound_path": ["server"] | None,   # bound key/section path; None = orphan comment
      "bound_kind": "key" | "section" | None,
      "doc_index": 0,
    }

ref_dict:
    {
      "syntax": "${db.host}",            # full reference syntax as written in the source
      "target": "db.host",               # extracted reference target
      "resolution_status": "resolved" | "unresolved" | "cyclic"
                            | "external" | "dynamic" | "not_applicable",
    }

Empty-container states:
    - "explicit_null"  — explicit null (YAML `plugins:`, JSON null; INI has no such state)
    - "empty_list"     — empty list (`[]`)
    - "empty_dict"     — empty mapping (`{}`, an INI section with no keys)
    - key absent = the entry does not appear in the contract (strictly
      distinguished from empty containers)

Inline-container expansion (v1.1, TOML):
    `x = {a = 1, b = [2, 3]}` yields:
    - entry x (source fact, value_state=container, value = the whole container)
    - derived entries x.a / x.b / x.b.0 / x.b.1
      (source_precision="line_only", line numbers inherited from the parent)
    - a derived container record x.b (connectivity preserved)
    Double-counting guard: value_fidelity's correctness does not count
    container-valued entries; their value fidelity is carried by the
    scalar children (see ConfigQuality v1.1).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence

from provendoc.core.diagnostics import sensitive_signals

# ============================================
# contract version
# ============================================

CONFIG_ENTRY_CONTRACT_VERSION = "config_entry_v1.1"

# ============================================
# entry source precision (v1.1: inline-container expansion)
# ============================================

SOURCE_PRECISION_LINE_ONLY = "line_only"
"""派生投影 entry（derived projection）的行号精度。

TOML 内联容器（`x = {a = 1, b = 2}` / `x = [1, 2]`）在 v1.1 展开:
    - parent entry = source fact（扫描器直接产出，行号真实）
    - child entries = derived projection（值取自权威解析结构，
      行号继承父 entry，`source_precision="line_only"`）

源事实 entry 无 source_precision 字段值（None）——行号与值均直接
来自源文件扫描。消费方（如 value_fidelity）据此区分事实层级。"""

# ============================================
# duplicate key semantics (declared by the parser)
# ============================================

DUP_LAST_WINS = "last_wins"
"""YAML/JSON：同层重复键静默覆盖，后者生效。"""

DUP_REJECTED = "rejected"
"""TOML / strict INI：重复键是解析错误（检测到 = 源文件非法）。"""

DUP_OVERRIDE_CHAIN = "override_chain"
"""INI recovery：出现次序全部保留为有序覆盖链。"""

DUPLICATE_KEY_SEMANTICS = {DUP_LAST_WINS, DUP_REJECTED, DUP_OVERRIDE_CHAIN}

# ============================================
# entry value states (empty-container tri-state)
# ============================================

VALUE_STATE_SCALAR = "scalar"
VALUE_STATE_EXPLICIT_NULL = "explicit_null"
VALUE_STATE_EMPTY_LIST = "empty_list"
VALUE_STATE_EMPTY_DICT = "empty_dict"
VALUE_STATE_CONTAINER = "container"

# ============================================
# scalar value types (layered type disclosure)
# ============================================

VALUE_TYPE_STRING = "string"
VALUE_TYPE_INT = "int"
VALUE_TYPE_FLOAT = "float"
VALUE_TYPE_BOOL = "bool"
VALUE_TYPE_NULL = "null"
# dotenv empty-value disclosure: additive enumeration for the bare
# declaration forms (no '=' → missing_value; '=' present but empty →
# empty_string). The value channel keeps both states shaped as "" for
# compatibility; this field carries the distinction.
VALUE_TYPE_MISSING_VALUE = "missing_value"
VALUE_TYPE_EMPTY_STRING = "empty_string"
# HCL static-unknown value state for references / complex expressions
# (additive; only the HCL channel fills it). Strictly separated from
# explicit null: value=None + value_type="null" = known null; value=None
# + value_type="unknown" = statically unknowable (var.* references,
# function calls, operator expressions — runtime-evaluated forms).
VALUE_TYPE_UNKNOWN = "unknown"
VALUE_TYPE_TIMESTAMP = "timestamp"
VALUE_TYPE_HEX_INT = "hex_int"
VALUE_TYPE_OCTAL_INT = "octal_int"
VALUE_TYPE_BINARY_INT = "binary_int"
"""config_toml_p0_prereg T1：0b 前缀二进制整数（additive，仅 TOML 通道填充）。"""
VALUE_TYPE_DATE = "date"
VALUE_TYPE_TIME = "time"
"""config_toml_p0_prereg T1：TOML 原生 local_date / local_time
（additive，仅 TOML 通道填充；datetime 用 TIMESTAMP + datetime_kind）。"""
VALUE_TYPE_FLOAT_INFINITY = "float_infinity"
VALUE_TYPE_FLOAT_NEG_INFINITY = "float_neg_infinity"
VALUE_TYPE_FLOAT_NAN = "float_nan"
VALUE_TYPE_BLOCKED_TAG = "blocked_tag"
VALUE_TYPE_CUSTOM_TAG = "custom_tag"
VALUE_TYPE_ALIAS_BUDGET = "alias_budget_exhausted"
"""alias 物化预算耗尽的 sentinel entry 类型（config_yaml_p1_prereg P4：
YAML billion-laughs 向量防护——组合引用可指数级膨胀 entry 数，
超限子树停止展开、reason 登记、整体 PARTIAL，安全但不完整）。"""

#: type_source enumeration: where the type fact comes from (decides arbitrability)
TYPE_SOURCE_TAG = "tag"
"""YAML node tag（tag:yaml.org,2002:*）——语法层事实，权威。"""

TYPE_SOURCE_IMPLICIT = "implicit"
"""隐式 resolver 推断（PyYAML 1.1 resolver 实测语义，D1=A+ 保兼容）。"""

TYPE_SOURCE_SOURCE = "source"
"""值由扫描器从源文本直接产出（非构造推断）。"""

# ============================================
# reference resolution status (five states + N/A)
# ============================================

REF_RESOLVED = "resolved"
"""目标在本文件内且引用链无环。"""

REF_UNRESOLVED = "unresolved"
"""目标在本文件内找不到，且不像环境变量/外部资源。"""

REF_CYCLIC = "cyclic"
"""沿引用链 DFS 回到自身（自引用死循环）。"""

REF_EXTERNAL = "external"
"""显式外部资源（file:/json:/yaml:/http 等）——include 不展开，只标记。"""

REF_DYNAMIC = "dynamic"
"""运行时决定（环境变量、$(command)、条件表达式）——不可静态解析。"""

REF_NOT_APPLICABLE = "not_applicable"

REFERENCE_RESOLUTION_STATUSES = {
    REF_RESOLVED, REF_UNRESOLVED, REF_CYCLIC,
    REF_EXTERNAL, REF_DYNAMIC, REF_NOT_APPLICABLE,
}

# ============================================
# shared interpolation patterns
# ============================================

INTERPOLATION_PATTERNS = [
    (re.compile(r"\$\{([^{}]+)\}"), "${...}"),
    (re.compile(r"\{\{\s*([\w.]+)\s*\}\}"), "{{...}}"),
    (re.compile(r"\%\(([^)]+)\)s"), "%(...)s"),
    (re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)"), "$VAR"),
    (re.compile(r"\$\(([^)]+)\)"), "$(...)"),
]

_EXTERNAL_PREFIXES = ("file:", "json:", "yaml:", "env:", "http://", "https://", "int:", "float:", "bool:")

_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Helm render-time built-in objects (leading-dot family, injected at
# render time → dynamic)
_HELM_BUILTIN_ROOTS = frozenset((
    "Chart", "Release", "Files", "Template", "Subcharts",
))
# GHA context namespaces (only when gated by {{...}} syntax → dynamic)
_GHA_CONTEXTS = frozenset((
    "github", "env", "secrets", "vars", "inputs", "matrix",
    "needs", "steps", "runner", "job", "strategy",
))

# ============================================
# placeholder default split
# ============================================

#: Name-field pattern for the Spring single-colon default form `${NAME:default}`
_NAME_COLON_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.+)$")

#: Scheme guard list — no single-colon split when the name field is
#: one of these scheme names (preserves the external classification of
#: `env:VAR` / `file:x`-style prefixes)
_SCHEME_NAMES = frozenset((
    "http", "https", "file", "json", "yaml", "env",
    "int", "float", "bool",
))


def _target_base_default(target: str) -> tuple:
    r"""Reference target → (base, default) unified stripping.

    Strip precedence (existing behavior + single-colon extension):
        1. ``:-`` (bash/env form) → default is the tail;
        2. ``?`` (Docker Compose form) → strip the base only
           (default=None, existing semantics unchanged);
        3. single colon ``NAME:default`` (Spring form) → split when the
           name field matches ``[A-Za-z_][\w.-]*`` and does not hit the
           scheme guard list (avoids mangling ``env:VAR``-style prefixes).
    Everything else returns (target, None).
    """
    for sep in (":-", "?"):
        if sep in target:
            head, _, tail = target.partition(sep)
            return head, (tail if sep == ":-" else None)
    m = _NAME_COLON_RE.match(target)
    if m and m.group(1) not in _SCHEME_NAMES:
        return m.group(1), m.group(2)
    return target, None


# Helm values namespace prefix (.Values.a.b → in-document a.b)
_HELM_VALUES_PREFIX = ".Values."


def _doc_target_base(target: str) -> tuple:
    """Reference target → (doc_base, default): strips the Helm values
    namespace prefix (``.Values.`` only) on top of default stripping.

    All in-document addressing in resolve_references (path_set hits /
    cycle-detection next hops / resolved_value / deep-chain follow)
    goes through this mapping; graph nodes keep the original target
    text. Non-Helm forms match ``_target_base_default`` verbatim.
    """
    base, default = _target_base_default(target)
    if base.startswith(_HELM_VALUES_PREFIX):
        base = base[len(_HELM_VALUES_PREFIX):]
    return base, default

# ============================================
# sensitive key masking (retrieval text only)
# ============================================

SENSITIVE_KEY_RE = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|credential|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)

# ============================================
# basic helpers
# ============================================


def json_safe(value: Any) -> Any:
    """Coerce a python value into a JSON-serializable native.

    YAML can construct date/datetime/set and other non-JSON types; the
    contract must stay serializable as a whole. Float specials
    (inf/-inf/nan) follow the proto3 JSON mapping precedent and become
    "Infinity"/"-Infinity"/"NaN" strings (json.dumps emits bare
    Infinity, which is invalid JSON, and allow_nan=False raises); the
    type fact is disclosed by the entry's value_type field — the
    string is only a legal carrier.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if math.isnan(value):
            return "NaN"
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {json_safe(k) if not isinstance(k, str) else k: json_safe(v)
                for k, v in value.items()}
    return str(value)


def path_str(segments: Sequence[str]) -> str:
    """Path segment list → dot-joined string. List index segments are decimal strings."""
    return ".".join(str(s) for s in segments)


def is_sensitive_key(key: str) -> bool:
    """Whether the key name hits the sensitive-word pattern (secrets/credentials, etc.)."""
    return bool(SENSITIVE_KEY_RE.search(key or ""))


# Sensitive-disclosure mask (same constant across channels — never
# store the value; the disclosure carries only the type name + a
# masked placeholder)
_SENSITIVE_MASK = "****"


def scan_sensitive_entries(entries: List[Dict[str, Any]]) -> int:
    """Two-channel sensitive disclosure.

    Scans the contract entry list on both the key-name and value-pattern
    channels (key name first):
    - Key channel: entry.key plus the last effective path segment
      (skipping ``#``-prefixed special segments and pure numeric index
      segments — covers element-name sensitivity of XML ``#text``
      entries); a hit adds type ``sensitive_key`` (checked first).
    - Value channel: types hit by
      ``sensitive_signals.scan_sensitive_types(raw_value)`` are merged
      in, deduplicated.
    Hit entries gain the additive ``sensitive_candidates=[{type, masked}]``
    (never the value); non-hits get no keys (zero drift).

    Returns the number of hit entries (callers build the
    ``sensitive_entry_values:N`` reason from it — count and keys stay
    consistent via a single loop). Idempotent: rescanning resets the
    candidate keys.
    """
    count = 0
    for entry in entries:
        names: List[str] = []
        key = entry.get("key")
        if key:
            names.append(key)
        # Last effective path segment: skip #-prefixed special segments
        # (XML #text/#comment etc.) and pure numeric index segments
        # (list positions); take the last real segment
        for seg in reversed(entry.get("path") or []):
            text = str(seg)
            if text.startswith("#") or text.isdigit():
                continue
            if text not in names:
                names.append(text)
            break
        types: List[str] = []
        for name in names:
            if is_sensitive_key(name):
                types.append("sensitive_key")
                break
        raw = entry.get("raw_value")
        if isinstance(raw, str) and raw:
            for t in sensitive_signals.scan_sensitive_types(raw):
                if t not in types:
                    types.append(t)
        if types:
            entry["sensitive_candidates"] = [
                {"type": t, "masked": _SENSITIVE_MASK}
                for t in types
            ]
            count += 1
        else:
            entry.pop("sensitive_candidates", None)
    return count


def infer_scalar(value: str) -> Any:
    """Shared loose scalar inference for flat config formats.

    Single source for the bool → int → float → passthrough heuristic
    previously copied verbatim into env_parser / properties_parser
    (and, with extra null handling, ini_parser).
    """
    if value.lower() in ("true", "yes", "on"):
        return True
    if value.lower() in ("false", "no", "off"):
        return False
    try:
        return int(value, 0)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# ============================================
# contract factories
# ============================================


def build_contract(
    fmt: str,
    duplicate_key_semantics: str,
    line_numbers: bool = True,
    comments: bool = True,
) -> Dict[str, Any]:
    """Create the contract skeleton."""
    assert duplicate_key_semantics in DUPLICATE_KEY_SEMANTICS, (
        f"unknown duplicate_key_semantics: {duplicate_key_semantics!r}"
    )
    return {
        "contract_version": CONFIG_ENTRY_CONTRACT_VERSION,
        "format": fmt,
        "duplicate_key_semantics": duplicate_key_semantics,
        "entries": [],
        "containers": [],
        "comments": [],
        "references": [],
        "loss": {
            "line_numbers": line_numbers,
            "comments": comments,
            "reasons": [],
        },
    }


def make_entry(
    path: Sequence[str],
    key: Optional[str],
    raw_value: Optional[str],
    value: Any,
    value_state: str = VALUE_STATE_SCALAR,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    description: Optional[str] = None,
    description_lines: Optional[List[int]] = None,
    duplicate: bool = False,
    override_chain: Optional[List[Dict[str, Any]]] = None,
    doc_index: int = 0,
    source_precision: Optional[str] = None,
    value_type: Optional[str] = None,
    type_source: Optional[str] = None,
    numeric_precision: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an entry contract dict.

    value_type / type_source are additive fields (default None: zero
    drift for existing formats; the YAML channel fills them —
    type_source=tag is a syntax-layer authoritative fact, implicit is
    PyYAML 1.1 resolver semantics as observed).

    numeric_precision is an additive field (default None: zero drift;
    the JSON channel fills "exact" (arbitrary-precision Python int) /
    "double" (IEEE 754) — disclosing how the raw source lexeme relates
    to the parsed value, e.g. 1.50 → 1.5 is double rounding).
    """
    entry = {
        "path": [str(s) for s in path],
        "path_str": path_str(path),
        "key": key,
        "kind": "entry",
        "raw_value": raw_value,
        "value": json_safe(value),
        "value_state": value_state,
        "value_type": value_type,
        "type_source": type_source,
        "line_start": line_start,
        "line_end": line_end,
        "description": description,
        "description_lines": description_lines,
        "duplicate": duplicate,
        "override_chain": override_chain,
        "references": [],
        "doc_index": doc_index,
        "source_precision": source_precision,
        "numeric_precision": numeric_precision,
    }
    return entry


def make_container(
    path: Sequence[str],
    container_type: str,
    child_count: int = 0,
    value_state: Optional[str] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    description: Optional[str] = None,
    description_lines: Optional[List[int]] = None,
    duplicate: bool = False,
    override_chain: Optional[List[Dict[str, Any]]] = None,
    implicit: bool = False,
    doc_index: int = 0,
) -> Dict[str, Any]:
    """Build a container contract dict; value_state defaults from the child count."""
    if value_state is None:
        value_state = (
            VALUE_STATE_EMPTY_DICT if child_count == 0 and container_type == "dict"
            else VALUE_STATE_EMPTY_LIST if child_count == 0 and container_type == "list"
            else VALUE_STATE_CONTAINER
        )
    return {
        "path": [str(s) for s in path],
        "path_str": path_str(path),
        "kind": "container",
        "container_type": container_type,
        "value_state": value_state,
        "child_count": child_count,
        "implicit": implicit,
        "line_start": line_start,
        "line_end": line_end,
        "description": description,
        "description_lines": description_lines,
        "duplicate": duplicate,
        "override_chain": override_chain,
        "doc_index": doc_index,
    }


def make_comment(
    kind: str,
    text: str,
    raw: str,
    line_start: int,
    line_end: int,
    bound_path: Optional[Sequence[str]] = None,
    bound_kind: Optional[str] = None,
    doc_index: int = 0,
) -> Dict[str, Any]:
    """Build a comment contract dict."""
    return {
        "kind": kind,  # "block" | "inline"
        "text": text,
        "raw": raw,
        "line_start": line_start,
        "line_end": line_end,
        "bound_path": [str(s) for s in bound_path] if bound_path else None,
        "bound_kind": bound_kind,  # "key" | "section" | None
        "doc_index": doc_index,
    }


# ============================================
# reference detection & resolution
# ============================================


def detect_references(raw_value: Optional[str]) -> List[Dict[str, Any]]:
    """Detect interpolation references in raw scalar text (no resolution decisions)."""
    if not raw_value or not isinstance(raw_value, str):
        return []
    refs: List[Dict[str, Any]] = []
    seen = set()
    for pattern, syntax in INTERPOLATION_PATTERNS:
        for match in pattern.finditer(raw_value):
            target = match.group(1).strip()
            full = match.group(0)
            key = (full, target)
            if key in seen:
                continue
            seen.add(key)
            refs.append({
                "syntax": full,
                "target": target,
                "resolution_status": REF_NOT_APPLICABLE,  # 由 resolve_references 填充
            })
    return refs


def _classify_target(
    target: str, path_set: set, ref_syntax: Optional[str] = None
) -> Optional[str]:
    """Statically classify a reference target; returns REF_* or None
    (an in-document candidate, needs cycle detection).

    Additional coverage:
    - Helm leading-dot family (.Chart and other built-ins → dynamic;
      .Values.x → namespace mapping at the resolve layer)
    - GHA context namespaces → dynamic (only when ``ref_syntax`` is
      the ``{{...}}`` form, so shell shapes are not misclassified)
    - single-token context variables (no dot / no colon residue) →
      dynamic.
    Omitting ``ref_syntax`` disables the GHA gate (backwards-compatible
    for external callers); all other criteria keep their order and
    semantics unchanged.
    """
    # Strip default-value syntax ${VAR:-default} / ${VAR?x} / ${NAME:default}
    base, _default = _target_base_default(target)
    if base.startswith(_EXTERNAL_PREFIXES) or "://" in base:
        return REF_EXTERNAL
    # JSON Pointer external-file forms bypass to external — a
    # `./file#/frag`-style prefix#fragment has no scheme and is not in
    # the family external-prefix table; the family pattern list has no
    # #/ forms, so zero pollution. (Targets of internal forms like
    # `#/frag` are already restored to dotted paths at the parser
    # layer and never reach this branch.)
    if "#/" in base and not base.startswith("#"):
        return REF_EXTERNAL
    if base.startswith("(") or "$(" in base:
        return REF_DYNAMIC
    if "?" in base and ":" in base:
        return REF_DYNAMIC
    # Helm leading-dot family ({{ .Release.Name }} / {{ .Values.x }})
    if base.startswith("."):
        rel = base.lstrip(".")
        head = rel.partition(".")[0]
        if head == "Values":
            if not rel[len("Values"):].lstrip("."):
                return REF_DYNAMIC  # 裸 .Values：整个 values 对象引用
            return None  # .Values.x → 映射 x 文档内寻址（R1）
        if head in _HELM_BUILTIN_ROOTS:
            return REF_DYNAMIC  # 渲染期内置注入对象
        # Other rare leading-dot shapes → fall through to the existing dotted rule below
    if base in path_set:
        return None  # in-document target
    # Env-var shape (uppercase/underscore) not found in-document → runtime injection
    if _ENV_VAR_RE.match(base):
        return REF_DYNAMIC
    # GHA context namespaces — gated by {{...}} syntax only; not
    # triggered when the head is itself an in-document path (a real
    # container) — in-document takes precedence
    if ref_syntax is not None and ref_syntax.startswith("{{"):
        head = base.partition(".")[0]
        if "." in base and head in _GHA_CONTEXTS and head not in path_set:
            return REF_DYNAMIC
    # Pointer gate: single-segment targets of the internal pointer
    # family (syntax "#/" prefix) that are not in-document must NOT
    # fall through to the single-token dynamic rule — in-document
    # addressing failure is unresolved, not runtime injection. ENV_VAR
    # shaped targets (e.g. `#/PATH_LIKE`) stay dynamic: that check
    # precedes this gate (frozen behavior, not flipped).
    if (
        ref_syntax is not None
        and ref_syntax.startswith("#/")
        and "." not in base
        and ":" not in base
        and not _ENV_VAR_RE.match(base)
    ):
        return REF_UNRESOLVED
    # Single-token context variables (no dot / no colon residue) →
    # runtime-injection family (Jinja loop/host vars, Prometheus
    # template vars, shell positional/custom vars). Colon residue
    # (e.g. ${VAR:?err} → "DB:") keeps the existing unresolved
    # classification (frozen behavior)
    if "." not in base and ":" not in base:
        return REF_DYNAMIC
    # INI cross-section references section.key — loose match: the last segment hits any path segment
    return None if "." in base else REF_UNRESOLVED


# ============================================
# schema tri-state (domain_hints structural expectations, family-common layer)
# ============================================


def schema_expectations(hints, entries, containers):
    """domain_hints structural expectations (offline-verifiable).

    Family-common layer promoted from yaml_parser (logic kept
    verbatim): returns a violations list (empty = satisfied), or
    None when no known profile applies. Evidence is limited to
    structural facts of this file — no network access, no guessing
    at remote schemas.
    """
    if not hints:
        return None
    entry_map = {e["path_str"]: e for e in entries}
    cont_map = {c["path_str"]: c for c in containers}
    violations = []
    for hint in hints:
        if hint == "kubernetes":
            for key in ("apiVersion", "kind"):
                e = entry_map.get(key)
                if e is None or not isinstance(e.get("value"), str) \
                        or not e.get("value"):
                    violations.append({
                        "path": key,
                        "expectation": "non-empty string",
                        "actual": json_safe(e.get("value"))
                        if e is not None else None,
                    })
        elif hint == "compose":
            c = cont_map.get("services")
            if c is None or c.get("container_type") != "dict" \
                    or not c.get("child_count"):
                violations.append({
                    "path": "services",
                    "expectation": "non-empty mapping",
                    "actual": ("empty" if c is not None and
                               not c.get("child_count") else "missing"),
                })
        elif hint == "ci":
            if not any(cont_map.get(k) for k in ("jobs", "stages")):
                violations.append({
                    "path": "jobs",
                    "expectation": "mapping (jobs or stages)",
                    "actual": "missing",
                })
    return violations


def apply_schema_status(contract, schema_ref, hints, entries, containers,
                        reasons):
    """Write the schema tri-state keys (on-demand keys: all omitted
    without evidence).

    Family-common layer promoted from yaml_parser, shared by yaml
    single-file _build_contract, parse_many_merged, and json
    _build_contract. Status evidence is limited to domain_hints
    structural expectations (offline-verifiable); a declared schema
    with no known profile → absent + reason (no network, no
    guessing).
    """
    if schema_ref:
        contract["schema_ref"] = schema_ref
    violations = schema_expectations(hints, entries, containers)
    if violations is not None:
        if violations:
            contract["schema_status"] = "violated"
            contract["schema_violations"] = violations
        else:
            contract["schema_status"] = "satisfied"
    elif schema_ref:
        contract["schema_status"] = "absent"
        reasons.append(f"schema_declared_unverifiable:{schema_ref}")


# ============================================
# reference resolution (five states + cycle detection + deep-chain follow)
# ============================================

# Deep-chain follow depth cap — deeply chained $ref schemas (recursive
# draft definitions) are normally ≤3 levels; no cap would be a DoS
# surface
_MAX_REF_FOLLOW_DEPTH = 8


def resolve_references(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve all references in the contract (five states + cycle
    detection), fill statuses, and return the summary.

    Modifies contract["entries"][*]["references"] and
    contract["references"] in place.
    """
    entries = contract.get("entries", [])
    containers = contract.get("containers", [])

    # path → entry index
    path_set = {e["path_str"] for e in entries}
    path_set.update(c["path_str"] for c in containers)

    # Reference graph: entry path_str → [target]
    graph: Dict[str, List[str]] = {}
    for entry in entries:
        refs = entry.get("references") or []
        # Detection may not have run yet (parser may store only raw)
        if not refs:
            refs = detect_references(entry.get("raw_value"))
            entry["references"] = refs
        targets = []
        for ref in refs:
            status = _classify_target(
                ref["target"], path_set, ref.get("syntax")
            )
            if status is None:
                targets.append(ref["target"])
        if targets:
            graph[entry["path_str"]] = targets

    def _is_cyclic(start_path: str) -> bool:
        """DFS: does the reference chain from start_path return to itself?"""
        stack = [start_path]
        on_path = {start_path}
        while stack:
            node = stack.pop()
            for target in graph.get(node, []):
                base, _default = _doc_target_base(target)
                if base == start_path:
                    return True
                # Only follow in-document targets
                if base in graph and base not in on_path:
                    on_path.add(base)
                    stack.append(base)
        return False

    flat_refs: List[Dict[str, Any]] = []
    status_counts = {
        REF_RESOLVED: 0, REF_UNRESOLVED: 0, REF_CYCLIC: 0,
        REF_EXTERNAL: 0, REF_DYNAMIC: 0, REF_NOT_APPLICABLE: 0,
    }

    # Static-expansion evidence: in-document path → value (entries
    # with a value, incl. explicit null)
    value_map: Dict[str, Any] = {}
    for e in entries:
        if e.get("value") is not None \
                or e.get("value_state") == VALUE_STATE_EXPLICIT_NULL:
            value_map[e["path_str"]] = e.get("value")

    def _ref_default(syntax: str, target: str) -> Optional[str]:
        """Static disclosure of `${VAR:-default}` / `${NAME:default}` defaults."""
        if not syntax.startswith("${") or not syntax.endswith("}"):
            return None
        _base, default = _target_base_default(target)
        return default

    def _static_value(ref: Dict[str, Any]) -> Optional[str]:
        """Statically expandable value of a reference: resolved value
        first, then the ``:-`` default."""
        if ref.get("resolved_value") is not None:
            return str(ref["resolved_value"])
        if ref.get("default") is not None:
            return str(ref["default"])
        return None

    def _doc_base(target: str) -> Optional[str]:
        """In-document base of a reference target (strips default
        separators + the Helm ``.Values.`` namespace prefix); None
        when not in-document."""
        base, _default = _doc_target_base(target)
        return base if base in path_set else None

    for entry in entries:
        for ref in entry.get("references") or []:
            target = ref["target"]
            status = _classify_target(
                target, path_set, ref.get("syntax")
            )
            if status is None:
                # In-document target: distinguish resolved / cyclic
                # (base goes through _doc_target_base, so `.Values.x`
                # addresses in-document x)
                base, _default = _doc_target_base(target)
                if base in path_set:
                    # Keep walking from the referenced target; returning to this entry means a cycle
                    if _is_cyclic(entry["path_str"]):
                        status = REF_CYCLIC
                    else:
                        status = REF_RESOLVED
                else:
                    status = REF_UNRESOLVED
            ref["resolution_status"] = status
            status_counts[status] += 1
            # Template static-expansion additive disclosure (no keys added when absent)
            default = _ref_default(ref["syntax"], target)
            if default is not None:
                ref["default"] = default
            if status == REF_RESOLVED:
                base, _default = _doc_target_base(target)
                if base in value_map:
                    ref["resolved_value"] = value_map[base]

    # --------------------------------------------------------
    # Deep-chain follow: when the target's value is itself a
    # reference, keep walking to the scalar leaf (bounded by
    # _MAX_REF_FOLLOW_DEPTH; past the limit a reason is disclosed,
    # never silent). Container targets get the additive
    # resolved_target_kind (no subtree materialized/copied). Follow
    # walks only resolved references; cycles terminate at the
    # classification layer (references on a cycle are already judged
    # cyclic and never enter follow). Two chain shapes: a scalar
    # entry whose value is itself a reference (string pointer /
    # template), and a pure $ref mapping container (a dict holding
    # only the single $ref key). Container-path cycles invisible to
    # the classification layer terminate silently in follow via the
    # visited set.
    # --------------------------------------------------------
    entry_map = {e["path_str"]: e for e in entries}
    cont_map = {c["path_str"]: c for c in containers}

    def _entry_link(path_str: str) -> Optional[str]:
        """Scalar entry whose value is itself a reference (string pointer / template shape) → next hop."""
        e = entry_map.get(path_str)
        if e is None:
            return None
        for r in e.get("references") or []:
            nb = _doc_base(r["target"])
            if nb is not None:
                return nb
        return None

    def _pure_ref_link(path_str: str) -> Optional[str]:
        """Pure $ref mapping container (dict with only the single $ref key) → next hop."""
        cont = cont_map.get(path_str)
        if cont is None or cont.get("container_type") != "dict" \
                or cont.get("child_count") != 1:
            return None
        e = entry_map.get(path_str + ".$ref")
        if e is None:
            return None
        for r in e.get("references") or []:
            nb = _doc_base(r["target"])
            if nb is not None:
                return nb
        return None

    depth_hits = 0
    for entry in entries:
        for ref in entry.get("references") or []:
            if ref.get("resolution_status") != REF_RESOLVED:
                continue
            base = _doc_base(ref["target"])
            if base is None:
                continue
            cur = base
            visited = {base}
            for _ in range(_MAX_REF_FOLLOW_DEPTH):
                nxt = _entry_link(cur) or _pure_ref_link(cur)
                if nxt is None:
                    # cur is the chain's end: for containers, disclose the kind (additive)
                    cont = cont_map.get(cur)
                    if cont is not None:
                        ref["resolved_target_kind"] = cont.get(
                            "container_type"
                        )
                    break
                if nxt in visited:
                    # Follow-level cycle: node reappears on the chain, terminate silently
                    break
                visited.add(nxt)
                if nxt in value_map:
                    # Scalar-leaf candidate (its value may itself be a reference; keep walking to overwrite)
                    ref["resolved_value"] = value_map[nxt]
                    cur = nxt
                    continue
                # Container / valueless target: next round decides via
                # link checks whether to continue or disclose the
                # terminal state
                cur = nxt
            else:
                # Depth exhausted (loop finished without break): disclose
                # honestly only when an unvisited continuation (a 9th hop)
                # still exists — reaching the leaf in exactly 8 hops is
                # not over the limit
                nxt = _entry_link(cur) or _pure_ref_link(cur)
                if nxt is not None and nxt not in visited:
                    depth_hits += 1
    if depth_hits:
        contract.setdefault("loss", {}).setdefault("reasons", []).append(
            f"ref_follow_depth_limit:{depth_hits}"
        )

    for entry in entries:
        for ref in entry.get("references") or []:
            flat = {
                "path": entry["path"],
                "path_str": entry["path_str"],
                "syntax": ref["syntax"],
                "target": ref["target"],
                "resolution_status": ref.get("resolution_status"),
            }
            if "default" in ref:
                flat["default"] = ref["default"]
            if "resolved_value" in ref:
                flat["resolved_value"] = ref["resolved_value"]
            if "resolved_target_kind" in ref:
                flat["resolved_target_kind"] = ref["resolved_target_kind"]
            flat_refs.append(flat)

        # Entry-level expanded_value: when every reference is
        # statically expandable, replace the template fragments in raw
        # with their static values (absent when dynamic / unresolved
        # references exist — never fabricated)
        refs_now = entry.get("references") or []
        raw = entry.get("raw_value")
        if refs_now and isinstance(raw, str):
            expanded = raw
            fully_static = True
            for ref in refs_now:
                # JSON Pointer / $ref are not expanded or materialized —
                # five-state disclosure only; family references without
                # a kind key (template shapes) are unaffected
                if ref.get("kind") in ("pointer", "$ref"):
                    fully_static = False
                    break
                sv = _static_value(ref)
                if sv is None:
                    fully_static = False
                    break
                if ref["syntax"] in expanded:
                    expanded = expanded.replace(ref["syntax"], sv)
                else:
                    fully_static = False
                    break
            if fully_static and expanded != raw:
                entry["expanded_value"] = expanded

    contract["references"] = flat_refs
    contract["reference_summary"] = {
        "total": len(flat_refs),
        "counts": status_counts,
    }
    return contract["reference_summary"]


# ============================================
# config version diff (format-agnostic)
# ============================================


def diff_contracts(
    contract_a: Dict[str, Any],
    contract_b: Dict[str, Any],
) -> Dict[str, Any]:
    """Entry-level version diff between two contracts.

    key = (doc_index, path_str) (same path across documents is not
    folded). changed compares both value and value_state (float
    specials compared via their string carriers — equal carriers mean
    equal values); line-number facts are disclosed on both sides. Pure
    function, format-agnostic (yaml/ini/properties family contracts
    are all comparable).

    Returns {added, removed, changed, unchanged_count, total_a, total_b}.
    """
    def _index(contract):
        out = {}
        for e in contract.get("entries", []):
            out[(e.get("doc_index"), e["path_str"])] = e
        return out

    def _snap(e):
        return {
            "value": e.get("value"),
            "value_state": e.get("value_state"),
            "raw_value": e.get("raw_value"),
            "line_start": e.get("line_start"),
        }

    a_map = _index(contract_a)
    b_map = _index(contract_b)

    def _emit(key, e, which):
        return {
            "path": e.get("path"),
            "path_str": e["path_str"],
            "doc_index": e.get("doc_index"),
            which: _snap(e),
        }

    added = [_emit(k, e, "new") for k, e in b_map.items() if k not in a_map]
    removed = [_emit(k, e, "old") for k, e in a_map.items() if k not in b_map]
    changed = []
    unchanged = 0
    for k, e_a in a_map.items():
        e_b = b_map.get(k)
        if e_b is None:
            continue
        if (e_a.get("value") == e_b.get("value")
                and e_a.get("value_state") == e_b.get("value_state")):
            unchanged += 1
        else:
            changed.append({
                "path": e_b.get("path"),
                "path_str": e_b["path_str"],
                "doc_index": e_b.get("doc_index"),
                "old": _snap(e_a),
                "new": _snap(e_b),
            })
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
        "total_a": len(a_map),
        "total_b": len(b_map),
    }


# ============================================
# accessors
# ============================================


def get_contract(document_or_metadata: Any) -> Optional[Dict[str, Any]]:
    """Read the contract from a Document or metadata dict; None for incompatible versions."""
    if document_or_metadata is None:
        return None
    meta = (
        document_or_metadata.metadata
        if hasattr(document_or_metadata, "metadata")
        else document_or_metadata
    )
    if not isinstance(meta, dict):
        return None
    contract = meta.get("config_entries")
    if not isinstance(contract, dict):
        return None
    if contract.get("contract_version") != CONFIG_ENTRY_CONTRACT_VERSION:
        return None
    return contract


def fill_child_counts(entries: List[Dict], containers: List[Dict]) -> None:
    """Backfill container child_count and fix the empty-container states.

    child_count = direct child entries + direct child containers.
    Containers whose child_count stays None (inline, not enumerated)
    are not fixed up.
    """
    child_counts: Dict[str, int] = {}
    for c in containers:
        child_counts.setdefault(c["path_str"], 0)
    for record in entries + containers:
        parent = record["path"][:-1]
        while parent:
            p = path_str(parent)
            if p in child_counts:
                child_counts[p] += 1
                break
            parent = parent[:-1]
    for c in containers:
        if c.get("child_count") is None:
            continue  # 内联未枚举容器，保留 None
        c["child_count"] = child_counts.get(c["path_str"], c["child_count"])
        if c["child_count"] == 0:
            if c["container_type"] == "dict":
                c["value_state"] = VALUE_STATE_EMPTY_DICT
            else:
                c["value_state"] = VALUE_STATE_EMPTY_LIST
        else:
            c["value_state"] = VALUE_STATE_CONTAINER


def flatten_structured_data(obj: Any, parent: str = "") -> Dict[str, str]:
    """Degraded path: flatten bare structured_data into path→value (fallback for quality evaluation)."""
    result: Dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            p = f"{parent}.{key}" if parent else str(key)
            if isinstance(value, (dict, list)):
                result.update(flatten_structured_data(value, p))
            else:
                result[p] = value
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            p = f"{parent}.{idx}"
            if isinstance(value, (dict, list)):
                result.update(flatten_structured_data(value, p))
            else:
                result[p] = value
    return result


# ========================================
# Multi-file overlay-merge shared layer (converges the isomorphic
# yaml/json/properties parse_many_merged implementations)
# ========================================

def merge_contracts(
    format_name: str,
    contracts: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Multi-file overlay merge: later files shadow earlier ones
    (kustomize/helm market semantics).

    Shared layer for the yaml/json/properties ``parse_many_merged``
    trio. Merges at the contract level keyed by ``(doc_index,
    path_str)``:
    - Scalar entries: the last file to define a path wins, with the
      additive ``source_file`` / ``source_file_index``; cross-file
      shadowing is disclosed in full via ``override_chain`` (each
      occurrence {file, file_index, value, line}, including the
      winning file's own in-file duplicate chain, expanded in file
      order)
    - containers: same-path winner + source annotation; child_count
      is recomputed
    - references: resolve_references re-runs after the merge
      (cross-file static references resolve against the merged path
      set — merged-snapshot semantics)

    ``duplicate_key_semantics`` becomes ``last_file_wins`` (the merge
    layer's cross-file shadowing semantics; in-file semantics
    override_chain/rejected/last_wins stay on their own entries/loss
    layers — the two layers are orthogonal).

    Returns the merged contract dict (not a Document — across files
    there is no single original text; line numbers are attributed via
    source_file).
    """

    def _occurrence(entry, file_index, file_name):
        return {
            "file": file_name,
            "file_index": file_index,
            "value": entry.get("value"),
            "line": entry.get("line_start"),
        }

    def _entry_occurrences(entry, file_index, file_name):
        """All occurrences of a path within one file (in-file duplicate chain expanded)."""
        chain = entry.get("override_chain")
        if chain:
            out = []
            for occ in chain:
                item = dict(occ)
                item.setdefault("file", file_name)
                item.setdefault("file_index", file_index)
                out.append(item)
            return out
        return [_occurrence(entry, file_index, file_name)]

    merged_entries: List[Dict[str, Any]] = []
    entry_slots: Dict[tuple, tuple] = {}
    order: List[tuple] = []
    merged_containers: List[Dict[str, Any]] = []
    cont_slots: Dict[tuple, tuple] = {}
    for fi, (c, src) in enumerate(zip(contracts, sources)):
        fname = src.get("file_name")
        for e in c.get("entries", []):
            key = (e.get("doc_index"), e["path_str"])
            if key not in entry_slots:
                entry_slots[key] = ([], None)
                order.append(("entry", key))
            occs, _win = entry_slots[key]
            occs.extend(_entry_occurrences(e, fi, fname))
            entry_slots[key] = (occs, e)
        for cont in c.get("containers", []):
            key = (cont.get("doc_index"), cont["path_str"])
            if key not in cont_slots:
                order.append(("container", key))
            cont_slots[key] = (cont, fi, fname)

    for kind, key in order:
        if kind == "entry":
            occs, winner = entry_slots[key]
            # Winning file index = the last occurrence's file_index
            # (single-file override_chain semantics put the winner at
            # the chain tail, so after expansion the last item is the
            # winning occurrence — the chain keeps its
            # all-occurrences-including-winner shape)
            win_fi = occs[-1].get("file_index", 0) if occs else 0
            win_fname = (occs[-1].get("file") or
                         sources[0]["file_name"]) if occs \
                else sources[0]["file_name"]
            out = dict(winner)
            out["source_file"] = win_fname
            out["source_file_index"] = win_fi
            if len(occs) > 1:
                out["duplicate"] = True
                out["override_chain"] = occs
            merged_entries.append(out)
        else:
            cont, fi, fname = cont_slots[key]
            out = dict(cont)
            out["source_file"] = fname
            out["source_file_index"] = fi
            merged_containers.append(out)

    merged: Dict[str, Any] = {
        "contract_version": CONFIG_ENTRY_CONTRACT_VERSION,
        "format": format_name,
        "merged": True,
        "duplicate_key_semantics": "last_file_wins",
        "sources": sources,
        "entries": merged_entries,
        "containers": merged_containers,
        "loss": {"reasons": sorted({
            r for c in contracts
            for r in (c.get("loss") or {}).get("reasons", [])
        })},
    }
    # Recount the sensitive-count reason as the single authority —
    # strip stale per-file pass-through counts (count-bearing strings
    # differ pairwise, so a set union leaves multiple wrong values).
    # yaml/env/properties/toml/plist/xml are rescanned here under the
    # two-channel policy; json/ini re-attach their own same-policy /
    # profile-gated counts at the tail (json via the tail
    # _attach_sensitive_disclosure; ini's npmrc gating is registered
    # separately), so they are skipped to avoid duplicate policies and
    # duplicate reasons. The rescan idempotently rewrites the
    # pass-through candidate keys (same policy, same input).
    merged["loss"]["reasons"] = [
        r for r in merged["loss"]["reasons"]
        if not r.startswith("sensitive_entry_values:")
    ]
    if format_name not in ("json", "ini"):
        sensitive_count = scan_sensitive_entries(merged_entries)
        if sensitive_count:
            merged["loss"]["reasons"].append(
                f"sensitive_entry_values:{sensitive_count}"
            )
    try:
        fill_child_counts(merged_entries, merged_containers)
    except Exception as e:  # pragma: no cover - defensive
        merged["loss"]["reasons"].append(f"child_count_recount_failed:{e}")
    try:
        resolve_references(merged)
    except Exception as e:  # pragma: no cover - defensive
        merged["loss"]["reasons"].append(f"resolve_references_failed:{e}")
    return merged
