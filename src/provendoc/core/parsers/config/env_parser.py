# ============================================================
# Structured Config Domain
# dotenv (.env) Parser
# ============================================================
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

_log = logging.getLogger(__name__)

# unquoted placeholder-value pattern (a common shape in
# example/template files; a hit only sets placeholder_candidate
# to reduce false-positive noise, never touches the sensitive count)
_PLACEHOLDER_RE = re.compile(
    r"(?i)\A(your[-_\w]*|my[-_\w]*|change[_-]?me|replace[_-]?me|"
    r"placeholder|dummy|sample|xxx+|<[^>\n]{1,64}>|"
    r"\$\{[^}\n]{1,64}\})\Z"
)

# explicit template-file basename suffix list. Deeper variants
# like `.env.example.local` are deliberately not listed (no
# market evidence); this module is the single source of truth.
ENV_TEMPLATE_SUFFIXES = (
    ".env.example",
    ".env.sample",
    ".env.template",
)


def is_env_template_name(name: str) -> bool:
    """True for explicit template basenames (`.env.example` etc.)."""
    lowered = name.lower()
    return lowered.endswith(ENV_TEMPLATE_SUFFIXES)


def discover_env_profile_chain(paths, mode=None):
    """Sort an env-file chain by Vite/Next.js official precedence
    (pure sorting helper).

    Load order (low precedence first, later files shadow earlier
    ones): ``.env`` < ``.env.local`` < ``.env.[mode]`` <
    ``.env.[mode].local`` (per the official docs: mode-specific
    files beat generic files, and the `.local` variant of the
    same name beats the non-local one).

    Sorts and returns only — never parses or merges (the chain
    order is handed to ``parse_many_merged`` by the caller).
    With ``mode=None`` the mode-specific layer cannot be
    decided, and variant-named files go honestly into
    excluded. Returns ``{"chain": [...], "excluded":
    [{"path":..., "reason":...}]}``; each chain item carries
    path / file_name / profile / mode / locality /
    precedence (higher wins). Identical paths are deduplicated;
    non-env-family members (.envrc / other suffixes) never
    enter the chain.
    """
    rank_of = {".env": 0, ".env.local": 1}
    if mode:
        rank_of[f".env.{mode}"] = 2
        rank_of[f".env.{mode}.local"] = 3
    seen = set()
    chain = []
    excluded = []
    for p in paths:
        p_str = str(p)
        if p_str in seen:
            continue
        seen.add(p_str)
        name = p_str.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in rank_of:
            chain.append({
                "path": p_str,
                "file_name": name,
                "profile": (
                    "mode-specific" if name.startswith(f".env.{mode}")
                    and mode else "generic"
                ),
                "mode": mode if mode and name.startswith(f".env.{mode}") else None,
                "locality": "local" if name.endswith(".local") else "shared",
                "precedence": rank_of[name],
            })
        elif name.startswith(".env."):
            excluded.append({
                "path": p_str,
                "reason": "variant-name-mode-unknown"
                if mode is None else "variant-not-in-chain",
            })
        else:
            excluded.append({
                "path": p_str, "reason": "not-env-family",
            })
    chain.sort(key=lambda item: item["precedence"])
    return {"chain": chain, "excluded": excluded}


class EnvParser(BaseParser):
    """dotenv ``.env`` parser (recovery mode).

    Syntax facts (dotenv dialect, aligned with mainstream
    implementations):
        - declarations: ``KEY=value``; an ``export KEY=value``
          prefix is stripped and flagged ``exported``
          (metadata only, never injected into the process env)
        - comments: a leading ``#`` (only when at line start or
          preceded by whitespace before stripping)
        - quotes: paired ``"`` / ``'`` at both value ends are
          stripped; double-quoted values get escape expansion,
          single-quoted values keep their text verbatim
        - multi-line values: a single trailing backslash
          continues the line (segments joined); a quoted open
          value spans lines (double quotes escape-aware /
          single quotes literal newlines); an unclosed value
          at EOF falls back to first-line standalone handling
          and discloses ``unclosed_quote``
        - KEY convention is UPPER_SNAKE, but not enforced
          (recovery semantics)
        - duplicate keys: last wins (same as the dotenv library)
        - detect but never execute: command substitution ``$(...)``
          / backticks / NUL bytes only land in loss reasons;
          variable references are identified by the shared
          resolve_references (five states, never reads the
          process environment)
    """

    # class-level format tag — subclasses (EnvrcParser with
    # "envrc") reuse the full parse / parse_many_merged chain
    # (including file_type metadata and the merge-layer format
    # tag); this class keeps the literal "env" with zero drift.
    _FORMAT = "env"

    def __init__(self, encoding="utf-8-sig", type_infer=False):
        """Initialize with an encoding and a type-inference switch."""
        self.encoding = encoding
        self.type_infer = type_infer

    def parse(self, file_path) -> Document:
        """Parse a .env file into a Document (duplicate keys: last wins)."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(file_path, self._FORMAT, self.encoding, True)
            text = ctx.text
            metadata = ctx.metadata

            flat = self._parse_flat(text)

            metadata["parse_success"] = not any(
                k == "_error" for k in flat
            )
            metadata["top_level_keys"] = list(flat.keys())

            metadata["structured_data"] = {
                "titles": list(flat.keys()),
                "contents": [{"section": "", "items": dict(flat)}],
                "tables": [],
                "structure": dict(flat),
            }

            metadata["config_entries"] = self._build_contract(
                text, file_name=Path(file_path).name,
            )

            # object-level unresolved registry: derived from the
            # contract's reference states / last_wins shadowed
            # occurrences; original keys untouched, written only
            # when non-empty; guarded silent degradation, never
            # blocks the main parse.
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text, metadata=metadata, source_text=ctx.source_text,
            )
        except Exception as e:
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                    "structured_data": {"_error": str(e)},
                },
            )

    def _parse_flat(self, text):
        flat = {}
        rows, _, _ = self._logical_lines(text)
        for _start, _end, logical, _cont in rows:
            line = logical.strip()
            if not line or line.startswith("#"):
                continue
            kv = self._split_line(line)
            if kv is None:
                continue
            key, val, _exported, _meta = kv
            flat[key] = (
                self._infer_type(val) if self.type_infer else val
            )
        return flat

    # ==================================
    # logical lines (multi-line value merging)
    # ==================================

    def _logical_lines(self, text):
        """Physical lines → logical lines (three-state dotenv
        merging).

        Returns (rows, unclosed_quotes, unclosed_continuations);
        each row = (line_start, line_end, logical_text,
        continuation_type). Three states:

        - a single trailing backslash on the value →
          backslash_continuation (segments joined with the
          ``\\`` and newline dropped, python-dotenv
          semantics; a double trailing backslash is literal
          and does not continue);
        - an unclosed quoted open value → lines swallowed up
          to the closing quote as quoted_multiline
          (double quotes escape-aware / single quotes literal
          newlines);
        - still unclosed at EOF → fall back to first-line
          standalone handling; the unclosed_quotes count
          lands in loss reasons via the caller.

        Comments/blank lines pass through as single physical
        lines (cont=None).
        """
        lines = text.splitlines()
        n = len(lines)
        rows = []
        unclosed_quotes = 0
        unclosed_conts = 0
        i = 0
        while i < n:
            raw = lines[i]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                rows.append((i, i, raw, None))
                i += 1
                continue
            # quoted open value spanning lines
            merged = self._merge_quoted(lines, i, n)
            if merged is not None:
                end, logical, closed = merged
                if closed:
                    rows.append((i, end, logical, "quoted_multiline"))
                    i = end + 1
                else:
                    # unclosed at EOF: fall back to first-line
                    # standalone handling
                    unclosed_quotes += 1
                    rows.append((i, i, raw, None))
                    i += 1
                continue
            # trailing-backslash continuation of the value
            merged = self._merge_continuation(lines, i, n)
            if merged is not None:
                end, logical, closed = merged
                rows.append((i, end, logical, "backslash_continuation"))
                if not closed:
                    unclosed_conts += 1
                i = end + 1
                continue
            rows.append((i, i, raw, None))
            i += 1
        return rows, unclosed_quotes, unclosed_conts

    @staticmethod
    def _value_of(line):
        """Value region of an assignment line (right of ``=``
        after export stripping); None for non-assignments."""
        s = line
        if s.startswith("export ") or s.startswith("export\t"):
            s = s[len("export"):].lstrip()
        if "=" not in s:
            return None
        return s.split("=", 1)[1].strip()

    def _merge_quoted(self, lines, i, n):
        """Merge a quoted open value across lines; return
        (end, logical, closed) or None.

        None = not a quoted-open shape (empty value / no
        quotes / already closed on this line).
        closed=False means it swallowed to EOF without closing
        (the caller falls back to the first line).
        """
        val = self._value_of(lines[i])
        if not val or val[0] not in ("'", '"'):
            return None
        if self._value_closed(val):
            return None
        buf = [lines[i]]
        j = i
        while j + 1 < n:
            buf.append(lines[j + 1])
            candidate = "\n".join(buf)
            cval = self._value_of(candidate)
            if cval and self._value_closed(cval):
                return j + 1, candidate, True
            j += 1
        return i, lines[i], False

    @staticmethod
    def _value_closed(val):
        """Approximate closedness check (for cross-line probing;
        exact single-line judging happens in _split_line)."""
        if not val or val[0] not in ("'", '"'):
            return True
        if len(val) >= 2 and val[0] == val[-1]:
            return True
        j = EnvParser._find_closing_quote(val, val[0])
        if j is None:
            return False
        rest = val[j + 1:].strip()
        return not rest or rest.startswith("#")

    def _merge_continuation(self, lines, i, n):
        """Merge a single-trailing-backslash continuation of the
        value; return (end, logical, closed) or None.

        Only an exactly single trailing ``\\`` (not a literal
        ``\\\\``) continues; each segment drops its trailing
        ``\\`` and the parts are joined directly; a final
        segment without a trailing ``\\`` ends the merge, and
        reaching EOF still open gives closed=False (merged
        honestly; the caller puts it in loss reasons).
        """
        val = self._value_of(lines[i])
        if val is None or not (
            val.endswith("\\") and not val.endswith("\\\\")
        ):
            return None
        parts = []
        j = i
        closed = False
        while j < n:
            seg = lines[j].rstrip()
            if seg.endswith("\\") and not seg.endswith("\\\\"):
                parts.append(seg[:-1])
            else:
                parts.append(seg)
                closed = True
                break
            j += 1
        return (j if closed else n - 1), "".join(parts), closed

    @staticmethod
    def _split_line(line):
        # export prefix: returned as the ``exported`` metadata
        # flag — neither dropped nor injected into the process
        # environment.
        # Returns the 4-tuple (key, val, exported, meta) — meta
        # carries the verbatim-spacing / quote-shape facts and
        # the inline-comment text (stripped and routed into the
        # comments channel).
        exported = False
        if line.startswith("export ") or line.startswith("export\t"):
            exported = True
            line = line[len("export"):].lstrip()
        if not line or line.startswith("#"):
            return None
        meta = {"spacing": None, "quote": "none", "inline_comment": None}
        if "=" not in line:
            # bare declaration (allowed by some dialects) → empty value
            if line and not any(c.isspace() for c in line):
                return line, "", exported, meta
            return None
        raw_key, raw_val = line.split("=", 1)
        # verbatim preservation: key's trailing whitespace +
        # "=" + value's leading whitespace (the original region)
        meta["spacing"] = (
            raw_key[len(raw_key.rstrip()):] + "=" +
            raw_val[:len(raw_val) - len(raw_val.lstrip())]
        )
        key = raw_key.strip()
        val = raw_val.strip()
        if not key:
            return None
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            quote = val[0]
            meta["quote"] = "double" if quote == '"' else "single"
            inner = val[1:-1]
            if quote == '"':
                inner = EnvParser._unescape_double(inner)
            return key, inner, exported, meta
        # quote prefix + trailing content (typically an inline
        # comment KEY="v" # c). When the symmetric fast path
        # misses, locate the closing quote escape-aware; the
        # remainder after it must start with # (inline comment)
        # to be stripped, otherwise the whole value is kept
        # verbatim in recovery mode (no guessing); unclosed
        # values are likewise kept as-is.
        if val and val[0] in ("'", '"'):
            j = EnvParser._find_closing_quote(val, val[0])
            if j is not None:
                rest = val[j + 1:].strip()
                if not rest or rest.startswith("#"):
                    meta["quote"] = "double" if val[0] == '"' else "single"
                    if rest.startswith("#"):
                        # inline-comment capture (after a quoted value)
                        meta["inline_comment"] = (
                            rest.lstrip("#").strip() or None
                        )
                    inner = val[1:j]
                    if val[0] == '"':
                        inner = EnvParser._unescape_double(inner)
                    return key, inner, exported, meta
                # garbage after the closing quote ("a"b): keep the
                # whole value verbatim in recovery mode; the quote
                # shape is disclosed from the leading quote
                meta["quote"] = "double" if val[0] == '"' else "single"
            else:
                # truly unclosed: kept as-is
                meta["quote"] = "unclosed"
        # unquoted: strip a trailing comment (dotenv dialect: the
        # # after a value must be preceded by whitespace)
        for idx in range(1, len(val)):
            if val[idx] == "#" and val[idx - 1] in (" ", "\t"):
                # inline-comment capture (after an unquoted value)
                meta["inline_comment"] = (
                    val[idx:].lstrip("#").strip() or None
                )
                val = val[:idx].rstrip()
                break
        return key, val, exported, meta

    @staticmethod
    def _find_closing_quote(val, quote):
        """Escape-aware closing-quote location (dotenv dialect).

        Inside double quotes ``\\`` is an escape prefix (the
        following character is consumed); the single-quote
        dialect does no escape processing, so the first
        matching quote closes. Returns None when unclosed
        (the caller keeps the value verbatim in recovery mode).
        """
        i = 1
        n = len(val)
        while i < n:
            ch = val[i]
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                return i
            i += 1
        return None

    @staticmethod
    def _unescape_double(value):
        out = []
        i = 0
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                nxt = value[i + 1]
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}
                if nxt in mapping:
                    out.append(mapping[nxt])
                    i += 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _infer_type(value):
        # shared with properties_parser (config/contracts.py)
        return cc.infer_scalar(value)

    # ==================================
    # config entry contract
    # ==================================

    def _build_contract(self, text, file_name=None):
        """Build the ConfigEntry contract (flat key space,
        last_wins)."""
        contract = cc.build_contract(
            "env", cc.DUP_LAST_WINS,
            line_numbers=True, comments=True,
        )
        reasons = contract["loss"]["reasons"]
        reasons.extend([
            "flat_key_space: env keys are kept verbatim",
        ])
        # template-file semantic flag (set only on an explicit
        # basename-suffix hit; additive top-level key, never a
        # loss reason)
        if file_name and is_env_template_name(str(file_name)):
            contract["template_file"] = True

        entries = contract["entries"]
        comments = contract["comments"]
        entry_map = {}
        pending_block = None

        # logical lines (multi-line value merging)
        rows, unclosed_quotes, unclosed_conts = self._logical_lines(text)
        lines = text.splitlines()

        def flush_block(target_path):
            """Bind the pending block comment to a target path and
            write it into the contract."""
            nonlocal pending_block
            if pending_block is None:
                return
            s, e, t = pending_block
            comments.append(cc.make_comment(
                "block", t, "\n".join(lines[s:e + 1]), s, e,
                bound_path=target_path, bound_kind="key" if target_path else None,
            ))
            pending_block = None

        for start, end, logical, cont in rows:
            line = logical.strip()
            if not line:
                continue
            if line.startswith("#"):
                comment_text = line.lstrip("#").strip()
                if pending_block is None:
                    pending_block = (start, start, comment_text)
                else:
                    s, _, t = pending_block
                    pending_block = (s, start, t)
                continue
            kv = self._split_line(line)
            if kv is None:
                continue
            key, val, exported, meta = kv
            flush_block([key])
            # empty-value three-state disclosure. A bare
            # declaration line (no =) is missing_value; with =
            # and an empty value (K= or K="") it is
            # empty_string; the value channel keeps the same
            # "" for both (compatibility untouched), and the
            # distinction lives in the additive value_type
            # field.
            missing = "=" not in line
            if key in entry_map:
                existing = entry_map[key]
                if existing["override_chain"] is None:
                    existing["override_chain"] = [
                        {"value": existing["raw_value"], "line": existing["line_start"]},
                        {"value": val, "line": start},
                    ]
                else:
                    existing["override_chain"].append({"value": val, "line": start})
                existing["duplicate"] = True
                existing["raw_value"] = val
                # keep the typed value in sync with the tail
                # value (same structure as the properties/ini
                # duplicate-key branches; previously this was
                # missed, which contradicted the last-wins
                # semantics stated in the docstring and left
                # raw_value / structured_data / chain tail
                # inconsistent).
                existing["value"] = (
                    self._infer_type(val) if self.type_infer else val
                )
                existing["value_type"] = (
                    cc.VALUE_TYPE_MISSING_VALUE if missing
                    else cc.VALUE_TYPE_EMPTY_STRING if val == ""
                    else None
                )
                # exported metadata synced to the tail line
                # (same structure as the tail-value governance)
                existing["exported"] = exported
                existing["line_start"] = start
                existing["line_end"] = end
                # verbatim preservation (synced with the tail value)
                existing["original_spacing"] = meta["spacing"]
                existing["original_quote"] = meta["quote"]
                # inline-comment flag synced to the tail line
                # (the comment body is only registered on the
                # first occurrence)
                if meta["inline_comment"] is not None:
                    existing["inline_comment"] = True
            else:
                entry = cc.make_entry(
                    [key], key, val,
                    self._infer_type(val) if self.type_infer else val,
                    line_start=start, line_end=end,
                )
                if missing:
                    entry["value_type"] = cc.VALUE_TYPE_MISSING_VALUE
                elif val == "":
                    entry["value_type"] = cc.VALUE_TYPE_EMPTY_STRING
                # exported metadata (flag only, never injected
                # into the process environment)
                entry["exported"] = exported
                # multi-line value shape disclosure
                if cont is not None:
                    entry["continuation_type"] = cont
                # placeholder-value flag to reduce false-positive
                # noise (never touches the sensitive count)
                if isinstance(val, str) and _PLACEHOLDER_RE.match(val):
                    entry["placeholder_candidate"] = True
                # verbatim preservation (additive: original
                # spacing region + quote shape)
                entry["original_spacing"] = meta["spacing"]
                entry["original_quote"] = meta["quote"]
                # inline comment: the stripped text goes into the
                # comments channel (inline, bound to the key) +
                # the entry flag (set only on a hit)
                if meta["inline_comment"] is not None:
                    entry["inline_comment"] = True
                    comments.append(cc.make_comment(
                        "inline", meta["inline_comment"],
                        " # " + meta["inline_comment"],
                        start, end, bound_path=[key], bound_kind="key",
                    ))
                entry_map[key] = entry
                entries.append(entry)

        flush_block(None)
        cc.fill_child_counts(entries, contract["containers"])
        # command-substitution / NUL detection (registered only;
        # never executed or expanded — no shell / subprocess /
        # network paths, ever)
        cmdsub = sum(
            1 for e in entries
            if "$(" in (e.get("raw_value") or "")
            or "`" in (e.get("raw_value") or "")
        )
        if cmdsub:
            reasons.append(f"command_substitution_detected:{cmdsub}")
        if "\x00" in text:
            reasons.append("nul_byte_detected:1")
        # EOF-unclosed disclosure (the value already fell back
        # to the current behavior)
        if unclosed_quotes:
            reasons.append(f"unclosed_quote:{unclosed_quotes}")
        if unclosed_conts:
            reasons.append(f"unclosed_continuation:{unclosed_conts}")
        # sensitive disclosure (shared dual-channel helper; zero
        # keys zero reasons when nothing hits — zero drift).
        sensitive_count = cc.scan_sensitive_entries(entries)
        if sensitive_count:
            reasons.append(f"sensitive_entry_values:{sensitive_count}")
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")
        # dialect capability declaration (an additive top-level
        # key, never a loss reason — wrong semantic domain and the
        # merge layer would duplicate it; the merged header is a
        # different shape that omits it)
        contract["dialect"] = {
            "profile": "generic_dotenv",
            "export_prefix": "stripped",
            "quotes": "paired",
            "continuation": "backslash+quoted_multiline",
            "interpolation": "identify_only",
            "duplicate_policy": "last_wins",
            "type_inference": bool(self.type_infer),
        }
        return contract

    # ========================================
    # multi-file cascading merge (a thin wrapper over the
    # shared cc.merge_contracts layer)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Multi-file cascading merge: later files shadow
        earlier ones (via the shared cc.merge_contracts layer).

        Each file goes through the existing single-file
        contract channel (with all disclosure semantics). env
        specifics: flat key space (containers always empty,
        doc_index always 0). The merged header's
        ``duplicate_key_semantics`` is uniformly
        ``last_file_wins`` (the merge layer's cross-file
        shadowing semantics; the in-file last-wins semantics
        are an orthogonal layer). Returns the merged contract
        dict (not a Document — line-number semantics are
        attributed via source_file). The base parse_many
        semantics stay frozen.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            contracts.append(meta.get("config_entries") or cc.build_contract(
                self._FORMAT, cc.DUP_LAST_WINS))
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
        return cc.merge_contracts(self._FORMAT, contracts, sources)
