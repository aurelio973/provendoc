# ============================================================
# Structured Config Domain
# systemd Unit Extension-Gated Parser
# ============================================================
"""systemd unit parser.

``.service``/``.timer`` are INI-like but **not plain INI** — repeated
directives append multi-values, empty values reset, Exec* get command-line
lexing, specifiers, and unit dependencies / start ordering (generic INI
last_wins does not apply). This parser is an **extension-gated** entry
point (the unit type comes from the file name — unlike the content gating
used for ``.conf``/``.cnf``):

1. Lexical layer: sections / directives (sole separator ``=``) /
   comments ``#`` ``;`` / backslash continuation (odd/even rule) /
   quotes / C-style escapes;
2. Semantic axes: unit_type + is_template + minimal
   directive_cardinality table + full occurrence retention + reset
   detection;
3. Exec* static parsing (zero execution): ``-@+!!`` prefix stripping +
   argv tokenization (non-shell) + shell_candidate + env reference
   candidates;
4. Environment multi-assignment / EnvironmentFile zero-IO registration
   (never read, injected, or expanded; dotenv semantics do not apply);
5. Specifiers identified, never expanded (16-letter alphabet in four
   classes, resolved_value always null);
6. Dependency / ordering / trigger-edge registration (After and Requires
   kept strictly distinct + inferred_target_candidate unadjudicated);
7. config_entry contract + systemd_profile / systemd_status disclosure
   (no Markdown summary output).

Red lines: zero execution (Exec* always not-executed-disclosed), zero
routing drift for ``.conf``/``.cnf``/``.ini``, zero IO (EnvironmentFile
never read), honest identification (off-table keys get
cardinality="unknown" and are never adjudicated).
"""

from __future__ import annotations

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
from provendoc.core.pipeline.parser_registry import drop_in_suffix_of

_log = logging.getLogger(__name__)

_FORMAT = "systemd"
_MAX_STATEMENTS = 100_000
_MAX_VALUE_LEN = 1_000_000
_EXECUTE_SEMANTICS = "not-executed-disclosed"

# ------------------------------------------------------------
# Semantic-axis constants: known sections + minimal cardinality table
# ------------------------------------------------------------
_KNOWN_SECTIONS = ("Unit", "Install", "Service", "Timer")
# section → owning unit type (Unit/Install generic; Service/Timer specific)
_SECTION_OWNER_UNIT_TYPE = {"Service": "service", "Timer": "timer"}

# The seven Exec* directives → resettable_list (empty value clears; repeats append)
_EXEC_DIRECTIVES = frozenset(
    (
        "ExecStart",
        "ExecStartPre",
        "ExecStartPost",
        "ExecReload",
        "ExecStop",
        "ExecStopPost",
        "ExecCondition",
    )
)

# multi_value (list keys: space-separated + repeated keys append)
_UNIT_LIST_DIRECTIVES = frozenset(
    (
        "After",
        "Before",
        "Wants",
        "Requires",
        "Requisite",
        "BindsTo",
        "PartOf",
        "Conflicts",
        "OnFailure",
        "OnSuccess",
        "Documentation",
    )
)
_UNIT_SINGLE_DIRECTIVES = frozenset(
    (
        "Description",
        "StartLimitIntervalSec",
        "StartLimitBurst",
        "DefaultDependencies",
    )
)
_INSTALL_LIST_DIRECTIVES = frozenset(
    (
        "WantedBy",
        "RequiredBy",
        "Also",
        "Alias",
        "UpheldBy",
    )
)
_SERVICE_LIST_DIRECTIVES = frozenset(
    (
        "Environment",
        "EnvironmentFile",
        "PassEnvironment",
        "UnsetEnvironment",
        "SupplementaryGroups",
        "RuntimeDirectory",
        "StateDirectory",
        "CacheDirectory",
        "LogsDirectory",
        "ReadWritePaths",
        "ReadOnlyPaths",
        "InaccessiblePaths",
        "CapabilityBoundingSet",
        "AmbientCapabilities",
        "RestrictAddressFamilies",
        "IPAddressAllow",
        "IPAddressDeny",
    )
)
_SERVICE_SINGLE_DIRECTIVES = frozenset(
    (
        "Type",
        "Restart",
        "RestartSec",
        "User",
        "Group",
        "WorkingDirectory",
        "RootDirectory",
        "LimitNOFILE",
        "TimeoutStartSec",
        "TimeoutStopSec",
        "KillMode",
        "KillSignal",
        "NotifyAccess",
        "StandardOutput",
        "StandardError",
        "SyslogIdentifier",
        "UMask",
        "NoNewPrivileges",
        "PrivateTmp",
        "PrivateDevices",
        "ProtectSystem",
        "ProtectHome",
    )
)
_TIMER_LIST_DIRECTIVES = frozenset(
    (
        "OnActiveSec",
        "OnBootSec",
        "OnStartupSec",
        "OnUnitActiveSec",
        "OnUnitInactiveSec",
        "OnCalendar",
    )
)
_TIMER_SINGLE_DIRECTIVES = frozenset(
    (
        "AccuracySec",
        "RandomizedDelaySec",
        "Persistent",
        "WakeSystem",
        "RemainAfterElapse",
        "Unit",
        "FixedRandomDelay",
        "DeferReactivation",
    )
)

# directive → canonical section (criterion for DIRECTIVE_IN_WRONG_SECTION)
_DIRECTIVE_SECTION: dict[str, str] = {}
for _k in _UNIT_LIST_DIRECTIVES | _UNIT_SINGLE_DIRECTIVES:
    _DIRECTIVE_SECTION[_k] = "Unit"
for _k in _INSTALL_LIST_DIRECTIVES:
    _DIRECTIVE_SECTION[_k] = "Install"
for _k in _EXEC_DIRECTIVES | _SERVICE_LIST_DIRECTIVES | _SERVICE_SINGLE_DIRECTIVES:
    _DIRECTIVE_SECTION[_k] = "Service"
for _k in _TIMER_LIST_DIRECTIVES | _TIMER_SINGLE_DIRECTIVES:
    _DIRECTIVE_SECTION[_k] = "Timer"

# Security-directive discovery list: 23 hardening fields + 12 high-risk
# fields (discovered, not adjudicated)
_SERVICE_SECURITY_DIRECTIVES = frozenset(
    (
        "User",
        "Group",
        "DynamicUser",
        "NoNewPrivileges",
        "PrivateTmp",
        "PrivateDevices",
        "ProtectSystem",
        "ProtectHome",
        "ReadWritePaths",
        "InaccessiblePaths",
        "CapabilityBoundingSet",
        "AmbientCapabilities",
        "RestrictAddressFamilies",
        "IPAddressAllow",
        "IPAddressDeny",
        "SystemCallFilter",
        "SystemCallArchitectures",
        "LockPersonality",
        "RestrictSUIDSGID",
        "RemoveIPC",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectControlGroups",
    )
)
_HIGH_RISK_DIRECTIVES = frozenset(
    (
        "ExecStart",
        "ExecStartPre",
        "ExecStartPost",
        "ExecStop",
        "Environment",
        "EnvironmentFile",
        "WorkingDirectory",
        "RootDirectory",
        "BindPaths",
        "ReadWritePaths",
        "LoadCredential",
        "LoadCredentialEncrypted",
    )
)
# Credential-reference directives (registered with zero IO)
_CREDENTIAL_DIRECTIVES = frozenset(("LoadCredential", "LoadCredentialEncrypted", "SetCredential"))
# User-context-dependent specifiers (%h home / %u user / %U uid)
_CONTEXT_DEPENDENT_SPECIFIERS = frozenset(("h", "u", "U"))


def _cardinality_of(key: str) -> str:
    """Directive cardinality (five classes; off-table keys → unknown)."""
    if key in _EXEC_DIRECTIVES:
        return "resettable_list"
    if key in (
        _UNIT_LIST_DIRECTIVES
        | _SERVICE_LIST_DIRECTIVES
        | _INSTALL_LIST_DIRECTIVES
        | _TIMER_LIST_DIRECTIVES
    ):
        return "multi_value"
    if key.startswith(("Condition", "Assert")):
        return "multi_value"  # conditions/assertions may repeat and stack
    if key in (_UNIT_SINGLE_DIRECTIVES | _SERVICE_SINGLE_DIRECTIVES | _TIMER_SINGLE_DIRECTIVES):
        return "single_value"
    return "unknown"


# ------------------------------------------------------------
# Relation edge types (After and Requires kept strictly distinct)
# ------------------------------------------------------------
_RELATION_EDGE_TYPES = {
    "Requires": "requires",
    "Wants": "wants",
    "Requisite": "requisite",
    "BindsTo": "binds_to",
    "PartOf": "part_of",
    "Conflicts": "conflicts",
    "OnFailure": "on_failure",
    "OnSuccess": "on_success",
    "After": "ordering_after",
    "Before": "ordering_before",
}

# ------------------------------------------------------------
# Specifier alphabet (15 letters + %%)
# ------------------------------------------------------------
_SPECIFIER_LETTERS = "uUhHtSTNnpifjJ"
_TEMPLATE_INSTANCE_SPECIFIERS = frozenset(("i", "I"))
_SPECIFIER_MEANINGS = {
    # Only letters with a defined meaning get one (evidence-only)
    "i": "unescaped_instance_name",
    "I": "escaped_instance_name",
}

# ------------------------------------------------------------
# Lexical constants: escapes / continuation / quotes / sections
# ------------------------------------------------------------
_SIMPLE_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", '"': '"', "\\": "\\"}
_ESCAPE_SCAN_RE = re.compile(
    r'\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|[tnr"\\]|(.))',
    re.S,
)
_SECTION_RE = re.compile(r"^\[(.*)\]$")
_TRAILING_BACKSLASH_RE = re.compile(r"(\\+)$")
_SPECIFIER_SCAN_RE = re.compile(r"%(.)", re.S)
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069\ufeff\u200b-\u200f]")
_SHELL_BASENAMES = frozenset(
    (
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "ash",
    )
)
_EXEC_PREFIX_CHARS = "-@+!"
_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Instance-name escaping + timezone candidates (weak identification)
_INSTANCE_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_TZ_RE = re.compile(r"^[A-Z][A-Za-z0-9]+(/[A-Za-z0-9_+-]+)+$")


class _Diag:
    """Diagnostic counter (contract channel: syntax errors → loss.reasons `CODE:N`)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def hit(self, code: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1

    def reasons(self) -> list:
        return [f"{k}:{v}" for k, v in sorted(self.counts.items())]


def _hex_decode(text: str) -> str | None:
    """Hex escape decoding (invalid hex → None; literal preserved)."""
    try:
        return chr(int(text, 16))
    except ValueError:
        return None


def _decode_escapes(value: str, diag: _Diag) -> str:
    """C-style escape decoding (inside/outside quotes alike); unknown
    sequences count INVALID_ESCAPE.

    Valid set: ``\\t \\n \\r \\" \\\\ \\xHH \\uXXXX``; any other
    ``\\`` + character → INVALID_ESCAPE (kept literally as a fallback).
    """
    out: list[str] = []
    pos = 0
    for m in _ESCAPE_SCAN_RE.finditer(value):
        out.append(value[pos : m.start()])
        g = m.group(0)
        bad = m.group(1)
        if bad is not None:
            diag.hit("INVALID_ESCAPE")
            out.append(g)  # literal preserved (no characters dropped)
        elif g[1] == "x":
            decoded = _hex_decode(g[2:4])
            if decoded is None:
                diag.hit("INVALID_ESCAPE")
                out.append(g)
            else:
                out.append(decoded)
        elif g[1] == "u":
            decoded = _hex_decode(g[2:6])
            if decoded is None:
                diag.hit("INVALID_ESCAPE")
                out.append(g)
            else:
                out.append(decoded)
        else:
            out.append(_SIMPLE_ESCAPES[g[1]])
        pos = m.end()
    out.append(value[pos:])
    return "".join(out)


def _strip_inline_comment(value: str) -> tuple[str, str | None]:
    """Strip unquoted, whitespace-preceded ``#``/``;`` inline comments (same as cnf)."""
    in_quote = ""
    for idx, ch in enumerate(value):
        if in_quote:
            if ch == in_quote:
                in_quote = ""
            continue
        if ch in "\"'":
            in_quote = ch
            continue
        if ch in "#;" and idx > 0 and value[idx - 1] in " \t":
            return value[:idx].rstrip(), value[idx:]
    return value, None


def _quote_scan(value: str) -> tuple[bool, str | None]:
    """Quote-pair check + whole-value quote_style (single+double mixed → mixed)."""
    styles: set[str] = set()
    in_quote = ""
    for ch in value:
        if in_quote:
            if ch == in_quote:
                in_quote = ""
            continue
        if ch in "\"'":
            in_quote = ch
            styles.add(ch)
    unclosed = bool(in_quote)
    if not styles:
        return unclosed, None
    if styles == {'"'}:
        return unclosed, "double"
    if styles == {"'"}:
        return unclosed, "single"
    return unclosed, "mixed"


def _tokenize(value: str) -> list[dict]:
    """systemd argv tokenization (non-shell).

    Whitespace separation + quote grouping (double-quoted escapes decoded,
    single-quoted literal) + backslash escaping the next character outside
    quotes (including spaces, forming groups). No ``$`` expansion, no
    glob, no shell metacharacters. Returns ``[{text, quote_style}]``.
    """
    tokens: list[dict] = []
    tok: list[str] = []
    quote = ""
    quote_style: str | None = None
    has_quote = False

    def flush() -> None:
        nonlocal tok, quote_style, has_quote
        if tok or has_quote:
            tokens.append(
                {
                    "text": "".join(tok),
                    "quote_style": quote_style,
                }
            )
        tok = []
        quote_style = None
        has_quote = False

    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if quote:
            if quote == '"' and ch == "\\" and i + 1 < n:
                nxt = value[i + 1]
                if nxt == "x" and i + 3 < n:
                    decoded = _hex_decode(value[i + 2 : i + 4])
                    if decoded is not None:
                        tok.append(decoded)
                        i += 4
                        continue
                if nxt == "u" and i + 5 < n:
                    decoded = _hex_decode(value[i + 2 : i + 6])
                    if decoded is not None:
                        tok.append(decoded)
                        i += 6
                        continue
                tok.append(_SIMPLE_ESCAPES.get(nxt, nxt))
                i += 2
                continue
            if ch == quote:
                quote = ""
            else:
                tok.append(ch)
            i += 1
            continue
        if ch in " \t":
            flush()
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            has_quote = True
            quote_style = ch if quote_style in (None, ch) else "mixed"
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt == "x" and i + 3 < n:
                decoded = _hex_decode(value[i + 2 : i + 4])
                if decoded is not None:
                    tok.append(decoded)
                    i += 4
                    continue
            if nxt == "u" and i + 5 < n:
                decoded = _hex_decode(value[i + 2 : i + 6])
                if decoded is not None:
                    tok.append(decoded)
                    i += 6
                    continue
            tok.append(_SIMPLE_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        tok.append(ch)
        i += 1
    if tok or has_quote:
        flush()
    return tokens


def _scan_specifiers(value: str, diag: _Diag) -> list[dict]:
    """Specifier identification (four classes; never expanded)."""
    nodes: list[dict] = []
    for m in _SPECIFIER_SCAN_RE.finditer(value):
        ch = m.group(1)
        node = {
            "node_type": "specifier",
            "raw_text": m.group(0),
            "specifier": ch,
            "resolved_value": None,  # never expanded
        }
        if ch == "%":
            node["classification"] = "literal_percent"
            # %% is the escaped-literal-percent form
            node["escaped_percent"] = True
        elif ch in _TEMPLATE_INSTANCE_SPECIFIERS:
            node["classification"] = "template_instance_specifier"
            # instance-scope specifier (valid only if the unit is instance/template)
            node["instance_scope"] = True
            node["meaning"] = _SPECIFIER_MEANINGS[ch]
        elif ch in _SPECIFIER_LETTERS:
            node["classification"] = "systemd_specifier"
            node["meaning"] = _SPECIFIER_MEANINGS.get(ch)
            # %h/%u/%U are user-context dependent
            if ch in _CONTEXT_DEPENDENT_SPECIFIERS:
                node["context_dependent"] = True
        else:
            node["classification"] = "invalid_specifier"
            diag.hit("INVALID_SPECIFIER")
        nodes.append(node)
    return nodes


def _parse_exec(value: str, diag: _Diag) -> dict | None:
    """Exec* static parsing (prefix stripping + argv tokenizing; zero
    execution).

    Returns ``{raw_command, prefixes, executable, arguments,
    shell_candidate, env_references}``; no valid token →
    INVALID_EXEC_COMMAND + None.
    """
    raw_command = value
    prefixes = ""
    rest = value.lstrip()
    while rest and rest[0] in _EXEC_PREFIX_CHARS:
        prefixes += rest[0]
        rest = rest[1:].lstrip()
    tokens = _tokenize(rest)
    if not tokens:
        diag.hit("INVALID_EXEC_COMMAND")
        return None
    executable = tokens[0]["text"]
    arguments = [t["text"] for t in tokens[1:]]
    shell_candidate = Path(executable).name in _SHELL_BASENAMES or "-c" in arguments
    env_refs: list[str] = []
    for m in _ENV_REFERENCE_RE.finditer(value):
        env_refs.append(m.group(0))
    return {
        "raw_command": raw_command,
        "prefixes": prefixes,
        "executable": executable,
        "executable_candidate": executable,
        "arguments": arguments,
        "shell_candidate": shell_candidate,
        "env_references": env_refs,
    }


def _parse_environment(value: str) -> list[dict]:
    """Environment= multi-assignment parsing.

    Returns ``[{variable_name, raw_value, decoded_value, quote_style}]``;
    tokens without ``=`` get variable_name=None (honest preservation).
    """
    assignments: list[dict] = []
    for tok in _tokenize(value):
        text = tok["text"]
        if "=" in text:
            name, _, raw = text.partition("=")
            var = name
        else:
            var, raw = None, text
        assignments.append(
            {
                "variable_name": var,
                "raw_value": raw,
                "decoded_value": raw,  # tokenizer already decoded per quote style
                "quote_style": tok["quote_style"],
            }
        )
    return assignments


def _parse_environment_file(value: str) -> dict:
    """EnvironmentFile= registration (zero IO; ``-`` prefix means optional)."""
    optional = value.startswith("-")
    path = value[1:] if optional else value
    return {
        "node_type": "environment_file",
        "path": path.strip(),
        "optional": optional,
        "resolved": False,  # not read, injected, or expanded
        "raw_value": value,
    }


def _parse_credential(directive: str, value: str) -> dict:
    """Credential-reference registration (zero IO).

    Discover / register paths; never read, decrypt, or inject; the
    SetCredential value literal is registered masked as ``"<present>"``,
    never in clear.
    """
    node = {
        "node_type": "credential_reference",
        "directive": directive,
        "encrypted": directive == "LoadCredentialEncrypted",
        "resolved": False,  # not read, decrypted, or injected
    }
    name, sep, rest = value.partition(":")
    if directive == "SetCredential":
        # value literal is sensitive — the whole entry is masked, raw
        # channel included (never registered in clear)
        node["name"] = name if sep else None
        node["value_literal"] = "<present>"  # masked
        node["source_path"] = None
    else:
        node["raw_value"] = value
        node["name"] = value if not sep else name
        node["source_path"] = rest if sep else None
    return node


def _timer_schedule_payload(key: str, value: str) -> dict:
    """Structured representation for Timer schedule fields (weak
    classification).

    ``normalized_value`` is always null and ``validated`` always False
    (no systemd-analyze validation or next-trigger computation);
    timezone/duration are registered as candidates only, unverified.
    """
    kind = _timer_schedule_kind(key)
    payload = {
        "node_type": "timer_schedule",
        "key": key,
        "raw_value": value,
        "schedule_type": kind,
        "normalized_value": None,
        "validated": False,
        "timezone_candidate": None,
        "duration_candidate": None,
    }
    if kind == "calendar":
        tokens = value.split()
        if tokens and _TZ_RE.match(tokens[0]):
            payload["timezone_candidate"] = tokens[0]
    elif kind in ("monotonic", "accuracy"):
        stripped = value.strip()
        if stripped:
            payload["duration_candidate"] = stripped
    return payload


def _template_model(file_name: str) -> dict:
    """template/instance/plain filename modeling (candidates, not
    adjudications).

    - ``worker@.service`` → template (template_name is itself);
    - ``worker@prod.service`` → instance (segment after ``@`` =
      instance_id; ``\\xHH`` systemd escaping → escaped_instance_id
      decode candidate);
    - no ``@`` → plain.

    Everything is a filename-derived candidate — zero IO, target
    existence never verified.
    """
    stem, dot, ext = file_name.rpartition(".")
    if not dot:
        stem, ext = file_name, ""
    if "@" not in stem:
        return {"is_template": False, "kind": "plain"}
    if stem.endswith("@"):
        return {
            "is_template": True,
            "kind": "template",
            "template_name": file_name,
        }
    base, _, inst = stem.partition("@")
    escaped = None
    if _INSTANCE_ESCAPE_RE.search(inst):
        try:
            escaped = _INSTANCE_ESCAPE_RE.sub(
                lambda m: chr(int(m.group(1), 16)),
                inst,
            )
        except ValueError:  # pragma: no cover - hex is always 2 digits
            escaped = None
    return {
        "is_template": False,
        "kind": "instance",
        "template_name": f"{base}@.{ext}" if ext else f"{base}@.",
        "instance_id": inst,
        "escaped_instance_id": escaped,
    }


def _timer_schedule_kind(key: str) -> str | None:
    """Weak schedule_kind declaration for Timer fields."""
    if key == "OnCalendar":
        return "calendar"
    if key in _TIMER_LIST_DIRECTIVES:  # On*Sec monotonic family
        return "monotonic"
    if key in ("AccuracySec", "RandomizedDelaySec"):
        return "accuracy"
    return None


class SystemdUnitParser(BaseParser):
    """systemd ``.service``/``.timer`` unit parser (syntax core)."""

    def __init__(self, encoding: str = "utf-8-sig"):
        """Init: encoding is only a read fallback (INVALID_ENCODING recorded on fallback)."""
        self.encoding = encoding

    # ==================================
    # BaseParser interface
    # ==================================

    def parse(self, file_path) -> Document:
        """Parse entry: lexical/syntactic recovery → semantic registration, producing a Document + contract."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path,
                _FORMAT,
                self.encoding,
                True,
            )
            text = ctx.text
            metadata = ctx.metadata
            file_name = Path(file_path).name
            stem = Path(file_path).stem
            suffix = Path(file_path).suffix

            # unit_type read straight from the extension + template flag (filename evidence)
            unit_type = (
                "service" if suffix == ".service" else "timer" if suffix == ".timer" else None
            )
            is_template = stem.endswith("@")
            # template/instance/plain filename modeling (candidates, not adjudications)
            template_model = _template_model(file_name)

            # drop-in parent-directory gating (same single source of
            # truth as the shared entry): filename evidence for
            # `example.service.d/override.conf` is computed from the
            # parent base unit — unit_name is the base name and
            # unit_type/is_template/template_model are rebuilt from it;
            # source tiering goes through the unit_source key.
            drop_in_gate = drop_in_suffix_of(file_path)
            unit_source = self._unit_source(file_path, drop_in_gate)
            if drop_in_gate is not None:
                file_name = Path(file_path).parent.name[:-2]
                suffix = Path(file_name).suffix
                stem = Path(file_name).stem
                unit_type = (
                    "service"
                    if suffix == ".service"
                    else "timer"
                    if suffix == ".timer"
                    else None
                )
                is_template = stem.endswith("@")
                template_model = _template_model(file_name)

            stmts, diag = self._parse_statements(text)
            if metadata.get("encoding_fallback"):
                diag.hit("INVALID_ENCODING")

            metadata["parse_success"] = not any(
                k.startswith(("INVALID", "UNCLOSED", "TRUNCATED")) for k in diag.counts
            )
            metadata["top_level_keys"] = [
                s.get("key") for s in stmts if s.get("kind") == "directive"
            ][:200]
            metadata["structured_data"] = {
                "statements": self._flat_statements(stmts),
                "structure": self._structure(stmts),
            }

            contract, profile = self._build_contract(
                stmts,
                diag,
                unit_type,
                is_template,
                template_model,
                file_name,
                # BIDI scan uses raw source (sanitized text has U+FFFD substituted)
                ctx.source_text or text,
                unit_source,
            )
            metadata["config_entries"] = contract

            # object-level unresolved registration (family convention; guarded silent degradation)
            try:
                stamp_unresolved_registry(
                    metadata,
                    "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive
                _log.debug("config unresolved registry skipped: %s", exc)

            # systemd_profile / systemd_status disclosure
            metadata["systemd_profile"] = profile
            metadata["systemd_status"] = self._status_of(
                diag,
                profile,
                contract,
            )

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000,
                3,
            )
            return Document(
                text=text,
                metadata=metadata,
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
                        "statements": [],
                        "structure": {},
                    },
                },
            )

    # ==================================
    # Multi-file assembly thin channel (merge_configs facade wiring)
    # ==================================

    def parse_many_merged(self, file_paths):
        """Multi-file assembly: tier-priority base + drop-in merge.

        Thin wrapper following the conf/cnf ``parse_many_merged``
        precedent: each file goes through the existing single-file
        ``parse`` channel (disk reading is entirely the caller's
        responsibility via the paths list — restricted IO; the assembly
        layer is a zero-IO pure function), and the Document list is then
        handed to ``systemd_unit_assembly.assemble_units`` for canonical
        grouping / tier winner selection / drop-in merge candidates /
        the dependency graph. The single-file ``parse`` channel is fully
        frozen (this is a purely additive method).

        No file-count validation here (the facade owns the strict ≥2
        gate; this channel also assembles single files).

        Returns:
            merged contract dict (not a Document — no single raw text
            across files): envelope header (contract_version / format
            systemd / merged / merge_semantics / sources) + the
            ``units`` / ``dependency_graph`` passed through from
            ``assemble_units`` + the auxiliary view
            ``assembly_summary_md`` (does not replace the contract).
            The envelope top level does **not** aggregate loss
            (units[].loss_codes already disclose per unit).
        """
        from provendoc.core.parsers.config.systemd_unit_assembly import (
            assemble_units,
        )
        from provendoc.core.parsers.config.systemd_unit_profile import (
            render_assembly_summary,
        )

        docs = [self.parse(fp) for fp in file_paths]
        assembly = assemble_units(docs)
        sources = []
        for i, (fp, doc) in enumerate(zip(file_paths, docs)):
            meta = doc.metadata or {}
            sources.append({
                "index": i,
                "file_name": meta.get("file_name") or Path(fp).name,
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
        return {
            "contract_version": cc.CONFIG_ENTRY_CONTRACT_VERSION,
            "format": "systemd",
            "merged": True,
            "merge_semantics": {
                "policy": "systemd_tier_drop_in_assembly",
                "note": (
                    "tier-priority winning base + drop-in merge; "
                    "static analysis of caller-provided file set; "
                    "not a runtime effective-configuration "
                    "determination"
                ),
                "validated": False,
                "context_dependent": True,
            },
            "sources": sources,
            "units": assembly["units"],
            "dependency_graph": assembly["dependency_graph"],
            "assembly_summary_md": render_assembly_summary(assembly),
        }

    # ==================================
    # Lexical layer
    # ==================================

    def _parse_statements(self, text: str):
        """Line-level scan → statement list + diagnostic counts."""
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

            if not stripped:
                i += 1
                continue

            # line comment (# or ; at line start) → pending group
            if stripped.startswith(("#", ";")):
                pending.append(
                    {
                        "text": stripped.lstrip("#;").strip(),
                        "raw": raw_line,
                        "line_start": line_no,
                        "line_end": line_no,
                    }
                )
                i += 1
                continue

            # section header
            if stripped.startswith("["):
                m = _SECTION_RE.match(stripped)
                if m:
                    stmt = {
                        "kind": "section",
                        "raw_section": stripped,
                        "name": m.group(1).strip(),
                        "invalid": False,
                        "line_start": line_no,
                        "line_end": line_no,
                        "leading_comments": list(pending),
                    }
                else:
                    # unclosed (no trailing ]) — INVALID_SECTION, recovered at line boundary
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

            # directive line (sole separator =; missing = → INVALID_DIRECTIVE)
            stmt, consumed = self._parse_directive_line(
                lines,
                i,
                pending,
                diag,
            )
            pending.clear()
            if stmt is not None:
                stmts.append(stmt)
            i += consumed

        # leftover comments at EOF → orphan (bound to None)
        if pending:
            stmts.append(
                {
                    "kind": "orphan_comment",
                    "comments": list(pending),
                    "line_start": pending[0]["line_start"],
                    "line_end": pending[-1]["line_end"],
                }
            )
        return stmts, diag

    def _parse_directive_line(self, lines, i, pending, diag):
        """Parse one logical directive + backslash continuation merging
        (odd/even rule).

        Returns (stmt, consumed_physical_lines).
        """
        raw_line = lines[i]
        line_no = i + 1
        n = len(lines)
        body = raw_line.strip()

        # systemd's sole separator is = (the key carries no quotes)
        if "=" not in body:
            diag.hit("INVALID_DIRECTIVE")
            return {
                "kind": "invalid",
                "raw": raw_line,
                "line_start": line_no,
                "line_end": line_no,
                "leading_comments": list(pending),
            }, 1

        raw_key, _, value_part = body.partition("=")
        key = raw_key.strip()
        if not key:
            diag.hit("INVALID_DIRECTIVE")
            return {
                "kind": "invalid",
                "raw": raw_line,
                "line_start": line_no,
                "line_end": line_no,
                "leading_comments": list(pending),
            }, 1

        # Continuation: an odd number of trailing backslashes continues
        # the line; an even number is literal. The raw chain preserves the
        # source verbatim (backslashes + newlines + indentation); the
        # logical chain drops the continuation markers and joins segments
        # with a single space.
        raw_parts = [value_part]
        logical_parts = [value_part.rstrip()]
        physical_lines = [line_no]
        consumed = 1
        continuation = None
        while True:
            m = _TRAILING_BACKSLASH_RE.search(raw_parts[-1].rstrip())
            if not m or len(m.group(1)) % 2 == 0:
                break
            if i + consumed >= n:
                # dangling continuation at EOF — INVALID_CONTINUATION
                diag.hit("INVALID_CONTINUATION")
                logical_parts[-1] = logical_parts[-1].rstrip()[:-1].rstrip()
                break
            raw_parts.append(lines[i + consumed])
            logical_parts.append(lines[i + consumed].strip())
            # drop the continuation backslash and its preceding
            # whitespace from the previous logical segment (boundary
            # whitespace is not semantic; logical segments join with a
            # single space)
            logical_parts[-2] = logical_parts[-2].rstrip()[:-1].rstrip()
            physical_lines.append(i + consumed + 1)
            consumed += 1
            continuation = "backslash"

        value_raw = "\n".join(raw_parts)  # preserved verbatim (incl. continuation newlines)
        logical_value = " ".join(logical_parts)
        if len(logical_value) > _MAX_VALUE_LEN:
            logical_value = logical_value[:_MAX_VALUE_LEN]

        # inline-comment stripping + quote-pair check + escape decoding
        # all operate on the logical value (continuation backslashes are
        # already gone, so `\<newline>` never misreports INVALID_ESCAPE)
        value_clean, inline_comment = _strip_inline_comment(logical_value)
        # trailing whitespace at the line boundary is not semantic (EOF dangling-continuation fallback)
        value_clean = value_clean.rstrip()
        unclosed, quote_style = _quote_scan(value_clean)
        if unclosed:
            diag.hit("UNCLOSED_QUOTE")
        decoded = _decode_escapes(value_clean, diag)

        return {
            "kind": "directive",
            "raw_key": raw_key,
            "key": key,
            "value_raw": value_raw,
            "value_logical": value_clean,
            "decoded_value": decoded,
            "logical_joined": value_clean,
            "inline_comment": inline_comment,
            "continuation": continuation,
            "quote_style": quote_style,
            "physical_lines": physical_lines,
            "line_start": line_no,
            "line_end": physical_lines[-1],
            "leading_comments": list(pending),
        }, consumed

    # ==================================
    # Semantic layer + contract building
    # ==================================

    def _build_contract(
        self,
        stmts,
        diag,
        unit_type,
        is_template,
        template_model,
        file_name,
        text,
        unit_source,
    ):
        contract = cc.build_contract("systemd", cc.DUP_OVERRIDE_CHAIN)
        entries = contract["entries"]
        containers = contract["containers"]
        comments = contract["comments"]
        reasons = contract["loss"]["reasons"]

        # profile-channel counters (findings/blocks/semantic classes — kept out of loss semantics)
        pcounts: dict[str, int] = {}

        def phit(code: str) -> None:
            pcounts[code] = pcounts.get(code, 0) + 1

        section_state: dict[str, dict] = {}
        current_section: dict | None = None
        top_entry_map: dict[str, dict] = {}
        unit_relations: list[dict] = []
        environment_files: list[dict] = []
        install_targets: dict[str, list] = {}
        section_placement: list[dict] = []
        unknown_sections: list[str] = []
        inferred_target_candidate: dict | None = None
        explicit_timer_unit: bool = False
        # credential references + security-directive findings
        credential_references: list[dict] = []
        security_directive_findings: list[dict] = []

        def flush_group(cs, bound_path, bound_kind):
            if not cs:
                return
            comments.append(
                cc.make_comment(
                    "block",
                    "\n".join(c["text"] for c in cs),
                    "\n".join(c["raw"] for c in cs),
                    cs[0]["line_start"],
                    cs[-1]["line_end"],
                    bound_path=bound_path,
                    bound_kind=bound_kind,
                )
            )

        def add_occurrence(entry, stmt, action: str) -> None:
            occ = {
                "raw_value": stmt["value_raw"],
                "action": action,
                "line": stmt["line_start"],
            }
            if stmt["key"] in _EXEC_DIRECTIVES and stmt["value_logical"].strip():
                # empty Exec value is reset semantics — not INVALID_EXEC_COMMAND
                exec_payload = _parse_exec(
                    stmt["value_logical"],
                    diag,
                )
                if exec_payload is not None:
                    occ["exec"] = exec_payload
                    phit("COMMAND_EXECUTION_BLOCKED")
            if stmt["key"] == "Environment":
                occ["assignments"] = _parse_environment(
                    stmt["value_logical"],
                )
            if stmt["key"] == "EnvironmentFile":
                env_file = _parse_environment_file(
                    stmt["value_logical"],
                )
                occ["environment_file"] = env_file
                environment_files.append(env_file)
                phit("ENVIRONMENT_FILE_BLOCKED")
            # credential reference (zero-IO registration + block disclosure)
            if stmt["key"] in _CREDENTIAL_DIRECTIVES:
                cred = _parse_credential(stmt["key"], stmt["value_logical"])
                occ["credential_reference"] = cred
                credential_references.append(cred)
                phit("CREDENTIAL_REFERENCE_BLOCKED")
                if stmt["key"] == "SetCredential":
                    # value literal sensitive — masked registration + reuse-code disclosure
                    phit("SENSITIVE_VALUE_DETECTED")
            entry.setdefault("systemd_occurrences", []).append(occ)

        def emit(stmt) -> None:
            nonlocal current_section, explicit_timer_unit
            nonlocal inferred_target_candidate
            kind = stmt["kind"]

            if kind == "orphan_comment":
                flush_group(stmt["comments"], None, None)
                return

            if kind == "invalid":
                flush_group(stmt.get("leading_comments"), None, None)
                entries.append(
                    cc.make_entry(
                        [f"@line_{stmt['line_start']}"],
                        None,
                        stmt["raw"],
                        stmt["raw"],
                        line_start=stmt["line_start"],
                        line_end=stmt["line_end"],
                    )
                )
                return

            if kind == "section":
                name = stmt.get("name") or ""
                flush_group(
                    stmt.get("leading_comments"),
                    [name],
                    "section",
                )
                state = section_state.get(name)
                if state is None:
                    container = cc.make_container(
                        [name],
                        "dict",
                        line_start=stmt["line_start"],
                        line_end=stmt["line_end"],
                    )
                    container["raw_section"] = stmt.get("raw_section")
                    container["normalized_section"] = name
                    if stmt.get("invalid"):
                        container["invalid_section"] = True
                    container["occurrences"] = [
                        {
                            "line_start": stmt["line_start"],
                            "line_end": stmt["line_end"],
                        }
                    ]
                    containers.append(container)
                    section_state[name] = {
                        "container": container,
                        "entry_map": {},
                    }
                else:
                    # repeated section: every instance preserved
                    state["container"]["duplicate"] = True
                    state["container"]["occurrences"].append(
                        {
                            "line_start": stmt["line_start"],
                            "line_end": stmt["line_end"],
                        }
                    )
                current_section = section_state[name]
                # unknown section / section misplacement (layered reporting)
                if name not in _KNOWN_SECTIONS:
                    phit("UNKNOWN_SECTION")
                    unknown_sections.append(name)
                else:
                    owner = _SECTION_OWNER_UNIT_TYPE.get(name)
                    if owner is not None and owner != unit_type:
                        section_placement.append(
                            {
                                "section": name,
                                "unit_type": unit_type,
                                "line_start": stmt["line_start"],
                            }
                        )
                return

            # kind == "directive"
            state = current_section
            sec_name = state["container"]["path"][0] if state else None
            path = [sec_name, stmt["key"]] if sec_name else [stmt["key"]]
            ps = cc.path_str(path)
            flush_group(
                stmt.get("leading_comments"),
                path,
                "key",
            )
            if not sec_name:
                # bare directive outside any section — syntax-layer anomaly
                diag.hit("INVALID_DIRECTIVE")

            key = stmt["key"]
            cardinality = _cardinality_of(key)
            is_empty = not stmt["value_logical"].strip()

            entry_map = state["entry_map"] if state else top_entry_map
            existing = entry_map.get(ps)

            # misplaced directive (known directive, wrong section owner)
            if sec_name and key in _DIRECTIVE_SECTION:
                if _DIRECTIVE_SECTION[key] != sec_name:
                    phit("DIRECTIVE_IN_WRONG_SECTION")

            if existing is not None:
                # repeated directive: all occurrences preserved (override_chain)
                existing["duplicate"] = True
                if existing["override_chain"] is None:
                    existing["override_chain"] = [
                        {
                            "value": existing["raw_value"],
                            "line": existing["line_start"],
                        }
                    ]
                existing["override_chain"].append(
                    {
                        "value": stmt["value_raw"],
                        "line": stmt["line_start"],
                    }
                )
                if cardinality == "resettable_list" and is_empty:
                    add_occurrence(existing, stmt, "reset")
                    phit("RESET_DIRECTIVE")
                elif cardinality == "single_value" and not is_empty:
                    add_occurrence(existing, stmt, "set")
                    phit("DUPLICATE_SINGLE_VALUE")
                else:
                    action = (
                        "append"
                        if cardinality == "multi_value"
                        else "append_or_set"
                        if cardinality == "resettable_list"
                        else "set"
                    )
                    add_occurrence(existing, stmt, action)
                return

            # first occurrence → create entry (empty value is an empty-string
            # scalar — the cnf-style default; empty-value semantics are
            # disclosed via occurrences.action="reset", no extra state)
            value_state = cc.VALUE_STATE_SCALAR
            desc, desc_lines = self._desc_of(stmt)
            entry = cc.make_entry(
                path,
                key,
                stmt["value_raw"],
                stmt["value_logical"],
                value_state=value_state,
                line_start=stmt["line_start"],
                line_end=stmt["line_end"],
                description=desc,
                description_lines=desc_lines,
            )
            entry["systemd_kind"] = "config_entry"
            entry["raw_key"] = stmt.get("raw_key")
            entry["cardinality"] = cardinality
            if state:
                entry["section"] = sec_name
                entry["raw_section"] = state["container"].get(
                    "raw_section",
                )
            if stmt.get("continuation"):
                entry["continuation_type"] = stmt["continuation"]
                entry["physical_lines"] = list(stmt["physical_lines"])
                entry["line_breaks_preserved"] = True
                entry["logical_value"] = stmt["logical_joined"]
            if stmt.get("quote_style"):
                entry["quote_style"] = stmt["quote_style"]
                entry["decoded_value"] = stmt["decoded_value"]
            elif stmt["decoded_value"] != stmt["value_logical"]:
                entry["decoded_value"] = stmt["decoded_value"]
            if stmt.get("inline_comment"):
                comments.append(
                    cc.make_comment(
                        "inline",
                        stmt["inline_comment"].lstrip("#;").strip(),
                        stmt["inline_comment"],
                        stmt["line_start"],
                        stmt["line_end"],
                        bound_path=path,
                        bound_kind="key",
                    )
                )

            # systemd semantic add-ons
            if key in _EXEC_DIRECTIVES:
                entry["systemd_kind"] = "exec_command"
            elif key == "Environment":
                entry["systemd_kind"] = "environment_assignments"
            elif key == "EnvironmentFile":
                entry["systemd_kind"] = "environment_file"
            elif key.startswith("Condition"):
                entry["systemd_kind"] = "condition"  # identify-only
            elif key.startswith("Assert"):
                entry["systemd_kind"] = "assertion"  # identify-only
            # security-directive findings (hardening/high_risk +
            # COMMAND_EXECUTION_DIRECTIVE findings — finding codes are
            # not vocab codes; nothing invented)
            if key in _HIGH_RISK_DIRECTIVES:
                entry["security_class"] = "high_risk"
            elif key in _SERVICE_SECURITY_DIRECTIVES:
                entry["security_class"] = "hardening"
            if key in _HIGH_RISK_DIRECTIVES or key == "SetCredential":
                security_directive_findings.append(
                    {
                        "code": "COMMAND_EXECUTION_DIRECTIVE",
                        "key": key,
                        "executed": False,
                        "confidence": 1.0,
                    }
                )
            schedule_kind = _timer_schedule_kind(key)
            if schedule_kind:
                entry["timer_schedule_kind"] = schedule_kind
                # structured timer schedule representation (weak
                # classification; coexists with schedule_kind, no key drift)
                entry["timer_schedule"] = _timer_schedule_payload(
                    key,
                    stmt["value_logical"],
                )
                # timer-only field in a non-timer unit →
                # INVALID_TIMER_SCHEDULE (profile channel — schema class)
                if unit_type != "timer":
                    phit("INVALID_TIMER_SCHEDULE")
                # empty OnCalendar (bare empty, outside reset semantics) → record code
                if key == "OnCalendar" and not stmt["value_logical"].strip():
                    phit("INVALID_TIMER_SCHEDULE")

            # specifier identification (scan inside the value)
            spec_nodes = _scan_specifiers(
                stmt["value_logical"],
                diag,
            )
            if spec_nodes:
                entry["systemd_references"] = spec_nodes
                # %i/%I in a plain unit → UNRESOLVED_SPECIFIER
                # (legal in instance/template scope — no code recorded)
                if template_model.get("kind") == "plain":
                    for n in spec_nodes:
                        if n["classification"] == "template_instance_specifier":
                            phit("UNRESOLVED_SPECIFIER")
                            break

            # first-occurrence action determination
            if cardinality == "resettable_list" and is_empty:
                action = "reset"
                phit("RESET_DIRECTIVE")
            elif cardinality == "multi_value":
                action = "append"
            elif cardinality == "resettable_list":
                action = "append_or_set"
            else:
                action = "set"
            add_occurrence(entry, stmt, action)

            # relation edges + [Install] target registration
            edge_type = _RELATION_EDGE_TYPES.get(key)
            if edge_type:
                for tok in _tokenize(stmt["value_logical"]):
                    if not tok["text"]:
                        continue
                    unit_relations.append(
                        {
                            "edge_type": edge_type,
                            "directive": key,
                            "target": tok["text"],
                            "line_start": stmt["line_start"],
                        }
                    )
            elif key == "Unit" and sec_name == "Timer":
                # explicit timer target → timer_triggers edge
                explicit_timer_unit = True
                unit_relations.append(
                    {
                        "edge_type": "timer_triggers",
                        "directive": key,
                        "target": stmt["value_logical"].strip(),
                        "line_start": stmt["line_start"],
                    }
                )
            elif key in _INSTALL_LIST_DIRECTIVES:
                targets = [t["text"] for t in _tokenize(stmt["value_logical"]) if t["text"]]
                if targets:
                    install_targets.setdefault(key, []).extend(targets)

            entries.append(entry)
            entry_map[ps] = entry

        for stmt in stmts:
            emit(stmt)

        # .timer without Unit= → inferred_target_candidate
        # (a registered candidate, never forced); missing-target
        # disclosure code (candidate on record + unadjudicated — kept
        # out of loss semantics)
        if unit_type == "timer" and not explicit_timer_unit and file_name.endswith(".timer"):
            inferred_target_candidate = {
                "target": file_name[: -len(".timer")] + ".service",
                "inferred": True,
                "resolution_status": "unresolved_by_design",
            }
            phit("TIMER_TARGET_MISSING")

        # Exec* merge-semantics axis reduction: resets clear the
        # accumulated list, everything else appends; more than one
        # effective ExecStart → invalid_duplicate_candidate disclosure
        # (never adjudicated)
        for e in entries:
            if e.get("systemd_kind") != "exec_command":
                continue
            effective: list[str] = []
            resets = 0
            for occ in e.get("systemd_occurrences") or []:
                if occ.get("action") == "reset":
                    resets += 1
                    effective = []
                else:
                    effective.append(occ.get("raw_value"))
            e["exec_merge_semantics"] = {
                "directive": e["path"][-1],
                "reset_count": resets,
                "effective_values": effective,
                "invalid_duplicate_candidate": e["path"][-1] == "ExecStart" and len(effective) > 1,
            }

        cc.fill_child_counts(entries, containers)
        reasons.extend(diag.reasons())
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")

        # BIDI control-character detection (raw-source scan; code reused from cnf)
        bidi_line = None
        bidi_m = _BIDI_RE.search(text or "")
        if bidi_m:
            bidi_line = text.count("\n", 0, bidi_m.start()) + 1
            phit("BIDI_CONTROL_DETECTED")

        contract["dialect"] = {
            "unit_type": unit_type,
            "syntax_classes": ["section", "directive"],
            "comments": "#-and-;-line-and-inline",
            "quotes": "paired-single-double",
            "continuation": "backslash-odd-even",
            "separators": "eq-only",
            "escapes": "c-style-hex-unicode",
            "type_inference": "candidate_only",
            "duplicate_policy": "all_occurrences_preserved",
            "cardinality": "resettable_list-multi-single-unknown",
            "specifiers": "identify_only_not_expanded",
            "environment_files": "registered_not_read",
            "execution": _EXECUTE_SEMANTICS,
        }

        profile = {
            "unit_type": unit_type,
            "unit_name": file_name,
            "is_template": is_template,
            "template_instance": template_model,
            "cardinality_counts": self._cardinality_counts(entries),
            "execute_semantics": _EXECUTE_SEMANTICS,
            "unit_enablement": "unknown",  # an [Install] section ≠ enabled
            "unit_relations": unit_relations,
            "environment_files": environment_files,
            "install_targets": install_targets,
            "inferred_target_candidate": inferred_target_candidate,
            # source tiering (static path evidence — role/tier/path
            # hints; masked/preset always "unknown": reading runtime
            # state is IO and forbidden)
            "unit_source": unit_source,
            # context/provenance: never evidenced (static single file —
            # static parse results are never mixed with runtime state)
            "unit_context": "unknown",
            "unit_provenance": {
                "generated": None,
                "masked": None,
                "transient": None,
                "preset": None,
                "runtime_drop_in": None,
            },
            # credential references (zero IO)
            "credential_references": credential_references,
            # security-directive findings
            "security_directive_findings": security_directive_findings,
        }
        if section_placement:
            profile["section_placement"] = section_placement
        if unknown_sections:
            profile["unknown_sections"] = unknown_sections
        if bidi_line is not None:
            profile["security_findings"] = [
                {
                    "code": "BIDI_CONTROL_DETECTED",
                    "key": None,
                    "masked": "****",
                    "line_start": bidi_line,
                    "path": [],
                }
            ]
        if pcounts:
            profile["loss_codes"] = sorted(pcounts)
            profile["loss_code_counts"] = dict(pcounts)
        return contract, profile

    # ==================================
    # Projection/status helpers
    # ==================================

    # Static tier table (order-sensitive — /usr/local/lib/systemd is
    # checked before /usr/lib/systemd so the prefix doesn't swallow the
    # other; same rule set as assembly._TIER_RULES:
    # /usr/lib→vendor(1) /usr/local/lib→local(2) /run→runtime(3)
    # /etc→admin(4), everything else unknown(None)).
    _TIER_PREFIXES = (
        ("usr/local/lib/systemd", "local", 2),
        ("usr/lib/systemd", "vendor", 1),
        ("run/systemd", "runtime", 3),
        ("etc/systemd", "admin", 4),
    )

    @classmethod
    def _unit_source(cls, file_path, drop_in_gate) -> dict:
        """unit_source tiering (pure static path evidence, zero IO).

        Base files are tiered via the static tier table; drop-ins are
        always unknown(None) — merge priority is re-derived by the
        assembly layer from the file's own path. generator/transient
        are path-segment evidence (register-only); masked/preset are
        always "unknown" (they need runtime state).
        """
        norm = str(Path(file_path)).replace("\\", "/").lower()
        generated = "/generator/" in norm
        transient = "/transient/" in norm
        if drop_in_gate is not None:
            parent = Path(file_path).parent.name
            return {
                "role": "drop_in",
                "base_unit": parent[:-2],
                "drop_in_file": Path(file_path).name,
                "tier": "unknown",
                "tier_priority": None,
                "generated": generated,
                "transient": transient,
                "masked": "unknown",
                "preset": "unknown",
            }
        tier = "unknown"
        priority = None
        for prefix, name, prio in cls._TIER_PREFIXES:
            if prefix in norm:
                tier = name
                priority = prio
                break
        return {
            "role": "base",
            "tier": tier,
            "tier_priority": priority,
            "generated": generated,
            "transient": transient,
            "masked": "unknown",
            "preset": "unknown",
        }

    @staticmethod
    def _desc_of(stmt):
        cs = stmt.get("leading_comments") or []
        if not cs:
            return None, None
        return (
            "\n".join(c["text"] for c in cs),
            [c["line_start"] for c in cs],
        )

    @staticmethod
    def _cardinality_counts(entries) -> dict:
        counts: dict[str, int] = {}
        for e in entries:
            c = e.get("cardinality")
            if c:
                counts[c] = counts.get(c, 0) + 1
        return counts

    @staticmethod
    def _flat_statements(stmts) -> list:
        out: list[dict] = []
        current = None
        for s in stmts:
            kind = s.get("kind")
            if kind == "section":
                current = s.get("name")
                out.append(
                    {
                        "kind": "section",
                        "name": current,
                        "line_start": s["line_start"],
                        "line_end": s["line_end"],
                    }
                )
            elif kind == "directive":
                out.append(
                    {
                        "kind": "directive",
                        "section": current,
                        "key": s.get("key"),
                        "value": s.get("value_logical"),
                        "line_start": s["line_start"],
                        "line_end": s["line_end"],
                    }
                )
        return out

    @staticmethod
    def _structure(stmts) -> dict:
        sections: dict[str, int] = {}
        current = None
        for s in stmts:
            if s.get("kind") == "section":
                current = s.get("name")
                sections.setdefault(current, 0)
            elif s.get("kind") == "directive" and current:
                sections[current] = sections.get(current, 0) + 1
        return {"sections": sections}

    def _status_of(self, diag, profile, contract) -> dict:
        """Status axes (layered reporting)."""
        syntax_codes = (
            "INVALID_ENCODING",
            "INVALID_SECTION",
            "INVALID_DIRECTIVE",
            "INVALID_CONTINUATION",
            "UNCLOSED_QUOTE",
            "INVALID_ESCAPE",
            "INVALID_SPECIFIER",
            "INVALID_EXEC_COMMAND",
        )
        schema_bad = any(
            code in profile.get("loss_codes", [])
            for code in ("UNKNOWN_SECTION", "DIRECTIVE_IN_WRONG_SECTION")
        ) or bool(profile.get("section_placement"))
        return {
            "syntax_status": (
                "partial" if any(c in diag.counts for c in syntax_codes) else "complete"
            ),
            "unit_schema_status": "invalid" if schema_bad else "complete",
            # the seven-state runtime axis is out of scope (a static file is not runtime state)
            "runtime_status": "not_applicable",
        }
