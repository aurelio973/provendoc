# ============================================================
# Code Domain
# Output-view and config-reproducibility declarations
# ============================================================
"""Engineering declaration facts for code-domain parses.

Frozen rules (code_engineering_facts_prereg_v1 P3, frozen 2026-10-25
D3=two-views): mirror the txt ``output_views`` / ``parse_config``
contracts, adapted honestly to the code domain.

Two views, not three: the code-domain cleaner contract is *never
rewrite code or string literals*, so the sanitized display text equals
the structured text and claiming a separate ``display`` view would be
an information-free alignment.  The raw view guarantees are identical
to the txt contract because both ride the same shared
read-decode-diagnose-normalize pipeline.
"""

import hashlib
import json
from typing import Any, Dict

_RAW_GUARANTEES = [
    "no_line_merging",
    "no_indent_stripping",
    "no_zero_width_removal",
    "no_transcoding",
    "no_value_rewriting",
    "no_deduplication",
]


def build_code_output_views() -> Dict[str, Any]:
    """Declare the two output views and the raw-mode guarantees.

    Raw mode already exists as ``Document.source_text`` (the faithful
    decoded view before BOM stripping / newline normalization /
    sanitization); this fact makes the contract explicit and testable.
    """
    return {
        "raw": "source_text",
        "structured": "text",
        "raw_guarantees": list(_RAW_GUARANTEES),
        "note": (
            "code-domain two-view contract: the code cleaner never "
            "rewrites code or string literals, so sanitized display "
            "text equals structured text and no third view is claimed; "
            "raw view = faithful decoded byte stream (pre-transform); "
            "truly-raw bytes remain addressable via metadata.sha256 "
            "of the source file"
        ),
    }


def build_code_parse_config(
    metadata: Dict[str, Any],
    parser_name: str,
    language: str,
    declared_encoding: str,
    tolerant: bool,
) -> Dict[str, Any]:
    """Config-reproducibility block: same file + same config = same facts.

    Records the effective configuration and a stable hash so diverging
    parse results across runs can be attributed to config drift instead
    of guessed at.  No timestamps, no durations — the hash is
    deterministic across runs of the same file and config.
    """
    from provendoc import __version__

    effective: Dict[str, Any] = {
        "parser": parser_name,
        "parser_version": __version__,
        "language": language,
        "encoding_override": declared_encoding,
        "tolerant": tolerant,
    }
    canonical = json.dumps(effective, sort_keys=True, ensure_ascii=True)
    effective["config_hash"] = hashlib.sha256(
        canonical.encode("ascii")
    ).hexdigest()[:16]
    effective["encoding_used"] = metadata.get("encoding_used")
    return effective
