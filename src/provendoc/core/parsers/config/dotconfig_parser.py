# -*- coding: utf-8 -*-
# ============================================================
# Structured Config Domain
# Generic `.config` Content-Gated Parser
# ============================================================
"""Generic ``.config`` parser (content-gated).

Positioning: ``.config`` is an extension-name family, not a single
syntax family — no second ``.config`` grammar is invented, and
content identification takes priority over extension semantics.
This parser is a thin content-gated entry point (following the
.conf/.cnf precedent, opposite in direction to the systemd
extension gate):

1. read the file first (this file's IO only, per the encoding
   diagnosis chain);
2. the content detector decides detected_syntax / profile /
   confidence / alternative_profiles / resolution_status /
   format_mismatch / profile_source;
3. an identified existing family (XML/JSON/YAML/INI/TOML,
   machine-verifiable evidence) is **handed off to the
   corresponding parser for reuse** (zero second implementations);
4. anything else falls to the ConfParser generic five-syntax
   recovery layer, plus an UNKNOWN_CONFIG_SYNTAX loss appended
   here (when the conf side itself handed off a family it is
   not appended — the syntax was identified by the delegate);
5. the config_profile handoff disclosure is attached (additive,
   existing keys never rewritten).

Red lines: zero execution; additive zero-drift (``.config``
previously had no registered entries — pure addition); zero
external IO; honest identification (when uncertain,
profile_uncertain, never guessing); XML security (DOCTYPE
rejected) and central sensitive-candidate stamping are
inherited via the delegation channel's existing
implementations. .NET Domain Profile semantics
(appSettings/configSource/XDT/inheritance levels) live in
``dotconfig_profile``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.parsers.config.dotconfig_profile import (
    apply_dotconfig_profile,
)

_log = logging.getLogger(__name__)

_FORMAT = "config"

# ------------------------------------------------------------
# filename profile candidate table (candidates only, no
# semantics assigned — same-source principle; when the content
# is not XML the candidate demotes to an alternative: a
# filename is only evidence, never a substitute for content)
# ------------------------------------------------------------
_FILENAME_PROFILE_HINTS = {
    "web.config": ("dotnet_web_config", 0.9),
    "app.config": ("dotnet_app_config", 0.9),
    "machine.config": ("dotnet_machine_config", 0.9),
    "nuget.config": ("nuget_config", 0.9),
    "runtime.config": ("runtime_config", 0.9),
    "dotnet-tools.config": ("dotnet_tools_config", 0.9),
}

# XML evidence 2: root element <configuration (a strong .NET signal)
_XML_ROOT_RE = re.compile(r"^<configuration[\s>/]", re.I)
# XML evidence 3: the first non-blank text looks like a
# well-formed opening tag (<name ...; used together with a
# dotnet filename as dual evidence; other angle forms are never
# guessed as XML — they fall to the ConfParser generic layer)
_TAG_LIKE_RE = re.compile(r"^<[A-Za-z_][\w.\-]*")


# ============================================================
# Content detector (pure functions, zero IO)
# ============================================================

def _family_hit(family: str, confidence: float, profile: str,
                alternatives: list, evidence: list) -> dict:
    """An existing-family hit → handoff detection result
    (format_mismatch is set when the detected family differs
    from the xml default)."""
    return {
        "detected_syntax": family,
        "profile": profile,
        "confidence": confidence,
        "alternative_profiles": alternatives,
        "resolution_status": "handed_off",
        "evidence_type": "machine_verifiable",
        "format_mismatch": family != "xml",
        "profile_source": "content_detection",
        "evidence": evidence,
    }


def _uncertain_hit() -> dict:
    """No machine-verifiable hit → honest degradation (the unknown
    case)."""
    return {
        "detected_syntax": "unknown",
        "profile": "generic_config_candidate",
        "confidence": 0.3,
        "alternative_profiles": [],
        "resolution_status": "profile_uncertain",
        "evidence_type": "none",
        "format_mismatch": False,
        "profile_source": "none",
        "evidence": [],
    }


def _xml_hit(confidence: float, evidence: list, fname_hint,
             profile: str = "xml_config") -> dict:
    """A three-evidence XML hit → handoff (profile defaults to
    xml_config; a dotnet filename hit uses the filename profile —
    filename and content evidence combine)."""
    alternatives: list = []
    conf = confidence
    if fname_hint is not None:
        if fname_hint[0] != profile:
            alternatives.append({
                "profile": fname_hint[0],
                "confidence": fname_hint[1],
                "evidence": ["filename"],
            })
        else:
            conf = max(conf, fname_hint[1])
    return _family_hit("xml", conf, profile, alternatives, evidence)


def detect_dotconfig_profile(text: str, file_name: str) -> dict:
    """Content detector → detection result dict (evidence-only).

    Decision order (machine-verifiable evidence first):
    JSON (0.98) → XML prolog/DOCTYPE (0.95) → root
    ``<configuration`` (0.9) → YAML ``---`` (0.85) → dotnet
    filename + angle structure (0.85) → INI/TOML heuristics
    (0.8, same skeleton as conf's ``_detect_existing_family``)
    → uncertain (0.3).
    """
    s = text.lstrip()
    if not s:
        return _uncertain_hit()
    fname_hint = _FILENAME_PROFILE_HINTS.get(file_name.lower())

    def filename_alt() -> list:
        if fname_hint is None:
            return []
        return [{
            "profile": fname_hint[0],
            "confidence": fname_hint[1],
            "evidence": ["filename"],
        }]

    # JSON: starts with { / [ and json.loads accepts it
    # (machine-verifiable)
    if s[:1] in "{[":
        try:
            json.loads(s)
            hit = _family_hit(
                "json", 0.98, "json_config", filename_alt(),
                ["content"],
            )
            return hit
        except ValueError:
            pass  # invalid JSON → no guessing, falls to the
            # generic recovery layer

    # XML evidence 1: prolog / DOCTYPE (machine-verifiable)
    if s[:5].lower() == "<?xml" or s[:9].lower() == "<!doctype":
        return _xml_hit(0.95, ["content"], fname_hint)

    # XML evidence 2: root element <configuration (a strong .NET signal)
    if _XML_ROOT_RE.match(s):
        return _xml_hit(0.9, ["content"], fname_hint)

    # YAML: starts with ---
    if text.startswith("---"):
        return _family_hit(
            "yaml", 0.85, "yaml_config", filename_alt(), ["content"],
        )

    # XML evidence 3: dotnet filename + angle structure, dual
    # evidence (a filename candidate alone never decides XML —
    # pure angle forms are never guessed)
    if fname_hint is not None and _TAG_LIKE_RE.match(s):
        return _xml_hit(
            0.85, ["filename", "content"], None,
            profile=fname_hint[0],
        )

    # INI / TOML heuristics (same skeleton as conf's
    # `_detect_existing_family`)
    lines = [ln.strip() for ln in text.splitlines()]
    content = [ln for ln in lines if ln and not ln.startswith(("#", ";"))]
    if content:
        def is_section(ln):
            return re.match(r"^\[+[^\[\]\n]+\]+$", ln) is not None

        def is_kv(ln):
            return re.match(r"^[A-Za-z0-9_.\-]+\s*[=:]\s*\S", ln) is not None

        has_brace = any("{" in ln for ln in content)
        semicolon_lines = [ln for ln in content if ln.endswith(";")]
        if (
            all(is_section(ln) or is_kv(ln) for ln in content)
            and not has_brace
            and not semicolon_lines
        ):
            toml_only = (
                any(re.match(r"^\s*\[\[.+\]\]", ln) for ln in content)
                or any(
                    re.match(r"^\s*\w[\w.\-]*\s*=\s*\[", ln)
                    for ln in content
                )
            )
            if toml_only:
                return _family_hit(
                    "toml", 0.8, "toml_config",
                    [{"profile": "ini", "evidence": ["heuristic"]}]
                    + filename_alt(),
                    ["content"],
                )
            if any(is_section(ln) for ln in content):
                return _family_hit(
                    "ini", 0.8, "ini_config",
                    [{"profile": "toml", "evidence": ["heuristic"]}]
                    + filename_alt(),
                    ["content"],
                )

    # no machine-verifiable hit at all → generic_config_candidate
    # + profile_uncertain + 0.3 (the unknown case)
    return _uncertain_hit()


# ============================================================
# DotConfigParser
# ============================================================

class DotConfigParser(BaseParser):
    """Generic ``.config`` content-gated parser (thin gate, zero
    second implementations).

    Five-way delegation reuses the existing core parsers
    (xml/json/yaml/ini/toml); uncertain content falls to the
    ConfParser generic five-syntax recovery layer (generic_
    directive / key_value / section / brace_block / shell_like,
    identify-only). XML security (DOCTYPE rejection) and
    central sensitive-candidate stamping are inherited via the
    delegation channel's existing implementations. The
    detection result is disclosed via metadata ``config_profile``
    (additive).
    """

    def __init__(self, encoding: str = "utf-8-sig"):
        """Init: encoding is only a reading fallback
        (INVALID_ENCODING codes it)."""
        self.encoding = encoding

    # ==================================
    # BaseParser interface
    # ==================================

    def parse(self, file_path) -> Document:
        """Parse entry: content detection → handoff or generic
        layer, produces a Document."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(
                file_path, _FORMAT, self.encoding, True,
            )
            text = ctx.text
            file_name = Path(file_path).name

            # content identification takes priority over extension
            # semantics
            detection = detect_dotconfig_profile(text, file_name)

            if detection["resolution_status"] == "handed_off":
                # identified as an existing family → hand off for
                # reuse (zero second implementations)
                doc = self._delegate_parse(
                    detection["detected_syntax"], file_path,
                )
            else:
                # uncertain → ConfParser generic recovery reuse
                doc = self._conf_generic_recovery(file_path)
                self._note_unknown_config_syntax(doc)

            self._attach_config_profile(doc, detection)
            # .NET Domain Profile semantics pass (dotconfig_profile)
            # — guarded, attached after the handoff disclosure; on
            # failure it degrades silently
            try:
                apply_dotconfig_profile(doc, detection, file_name)
            except Exception:  # pragma: no cover - defensive guard
                _log.debug("dotnet config profile pass skipped")
            md = doc.metadata
            if isinstance(md, dict):
                md["parse_duration_ms"] = round(
                    (time.perf_counter() - start_time) * 1000, 3,
                )
            return doc
        except Exception as e:
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                },
            )

    # ==================================
    # five-way delegation (handing off to the existing family
    # parsers, following the ConfParser precedent)
    # ==================================

    @staticmethod
    def _delegate_parse(family: str, file_path) -> Document:
        if family == "xml":
            from provendoc.core.parsers.config.xml_parser import (
                XmlParser,
            )
            return XmlParser().parse(file_path)
        if family == "json":
            from provendoc.core.parsers.config.json_parser import (
                JsonParser,
            )
            return JsonParser().parse(file_path)
        if family == "yaml":
            from provendoc.core.parsers.config.yaml_parser import (
                YamlParser,
            )
            return YamlParser().parse(file_path)
        if family == "ini":
            from provendoc.core.parsers.config.ini_parser import (
                IniParser,
            )
            return IniParser().parse(file_path)
        if family == "toml":
            from provendoc.core.parsers.config.toml_parser import (
                TomlParser,
            )
            return TomlParser().parse(file_path)
        raise ValueError(f"unknown delegate family: {family!r}")

    @staticmethod
    def _conf_generic_recovery(file_path) -> Document:
        """Uncertain → ConfParser generic five-syntax recovery
        reuse."""
        from provendoc.core.parsers.config.conf_parser import ConfParser

        return ConfParser().parse(file_path)

    @staticmethod
    def _note_unknown_config_syntax(doc: Document) -> None:
        """.config-level unidentified syntax → append
        UNKNOWN_CONFIG_SYNTAX.

        If the ConfParser side itself handed off a family (its
        identification surface is wider), nothing is appended —
        the syntax was already identified by the delegate and
        only this layer's profile_uncertain remains; otherwise
        UNKNOWN_CONFIG_SYNTAX (this layer) and any
        UNKNOWN_CONF_PROFILE (the conf recovery layer) keep
        their distinct meanings and coexist as evidence.
        """
        try:
            md = doc.metadata
            if not isinstance(md, dict) or md.get("parse_failed"):
                return
            conf_profile = md.get("conf_profile")
            if isinstance(conf_profile, dict) and (
                conf_profile.get("resolution_status") == "handed_off"
            ):
                return
            contract = md.get("config_entries")
            if isinstance(contract, dict):
                loss = contract.get("loss")
                if isinstance(loss, dict):
                    reasons = loss.get("reasons")
                    if isinstance(reasons, list) and (
                        "UNKNOWN_CONFIG_SYNTAX" not in reasons
                    ):
                        reasons.append("UNKNOWN_CONFIG_SYNTAX")
        except Exception:  # pragma: no cover - defensive guard
            _log.debug("unknown config syntax note skipped")

    @staticmethod
    def _attach_config_profile(doc: Document, detection: dict) -> None:
        """After handoff/recovery, attach the additive detection
        disclosure (existing keys never rewritten)."""
        try:
            md = doc.metadata
            if isinstance(md, dict) and "config_profile" not in md:
                profile = dict(detection)
                profile["delegated_to"] = (
                    f"{detection['detected_syntax']} parser"
                    if detection["resolution_status"] == "handed_off"
                    else "conf_generic_recovery"
                )
                md["config_profile"] = profile
        except Exception:  # pragma: no cover - defensive guard
            _log.debug("dotconfig profile attach skipped")
