# ============================================================
# Structured Config Domain
# direnv .envrc Parser (direnv shell dialect, static
# identification, never executed)
# ============================================================
import logging
import re

from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.env_parser import (
    _PLACEHOLDER_RE,
    EnvParser,
)

_log = logging.getLogger(__name__)

# Shell variable-name shape (a POSIX identifier): a key-value
# line's key must match to be registered — pseudo-keys split out
# of test lines like `if [ x = y ]; then` (containing spaces)
# and bare tokens (command calls, not assignments) are honestly
# rejected into unrecognized_shell_line.
_SHELL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# direnv stdlib directives + common .envrc shell builtins (a
# leading token hit → directive entry registration). Basis: the
# official direnv-stdlib function families; identified and
# registered only, never executed — reading the target files of
# source/use belongs to the execution domain (zero shell / zero
# child processes / zero network paths / zero extra IO).
ENVRC_DIRECTIVE_NAMES = frozenset({
    # direnv-stdlib official functions
    "dotenv", "source_env", "source_env_if_exists",
    "source_up", "source_up_if_exists",
    "fetchurl", "fetchgithub",
    "PATH_add", "MANPATH_add", "path_add", "expand_path",
    "layout", "use", "rvm", "load_prefix",
    "direnv_load", "direnv_apply_dump",
    "watch_file", "watch_dir",
    # common .envrc shell builtins
    "source", "eval",
})

_EXECUTE_SEMANTICS = "not-executed-disclosed"


class EnvrcParser(EnvParser):
    """direnv ``.envrc`` parser (static-identification recovery
    mode).

    Syntax facts (direnv dialect, identify-only):
        - ``export KEY=VALUE`` / ``KEY=VALUE``: dotenv-isomorphic
          key-value pairs (reusing EnvParser value semantics:
          quote stripping / escapes / trailing comments /
          continuations), with the additive field
          ``envrc_directive`` disclosing the line form
          (export / assign / directive); the key must match the
          POSIX identifier shape, otherwise honestly rejected
          (pseudo-keys / bare tokens are shell statements, not
          assignments);
        - direnv stdlib directives (source/use/layout/PATH_add/...):
          registered as entries with the directive name + raw
          arguments + ``execute_semantics:
          "not-executed-disclosed"`` — registered only, never
          executed;
        - subshells ``$(...)`` / backticks / NUL: disclosed as
          loss reasons (isomorphic disclosure: detected, never
          executed);
        - shell lines that cannot be statically identified
          (function definitions / loops / conditionals / bare
          token command calls): loss reason
          ``unrecognized_shell_line:N``, honest registration,
          no guessing.

    ``format`` is ``"envrc"`` (``get_contract`` only validates
    contract_version, format is unconstrained — shared
    representations can consume the envrc contract).
    """

    _FORMAT = "envrc"

    def _parse_flat(self, text):
        """The flat view keeps key-value lines only (directive
        lines / rejected lines never enter structured_data)."""
        flat = {}
        rows, _, _ = self._logical_lines(text)
        for _start, _end, logical, _cont in rows:
            line = logical.strip()
            if not line or line.startswith("#"):
                continue
            if line.split(None, 1)[0] in ENVRC_DIRECTIVE_NAMES:
                continue
            kv = self._split_line(line)
            if (kv is None or "=" not in line
                    or not _SHELL_NAME_RE.match(kv[0])):
                continue
            key, val, _exported, _meta = kv
            flat[key] = (
                self._infer_type(val) if self.type_infer else val
            )
        return flat

    # ==================================
    # config entry contract
    # ==================================

    def _build_contract(self, text, file_name=None):
        """Build the envrc contract (format "envrc";
        identify-only disclosure)."""
        contract = cc.build_contract(
            "envrc", cc.DUP_LAST_WINS,
            line_numbers=True, comments=True,
        )
        reasons = contract["loss"]["reasons"]
        reasons.extend([
            "flat_key_space: envrc keys are kept verbatim",
            "identify_only: direnv directives registered, not executed",
        ])

        entries = contract["entries"]
        comments = contract["comments"]
        entry_map = {}
        pending_block = None
        unrecognized = 0

        # Reuse the env-family logical-line channel (quoted
        # multi-line spans / backslash continuations) — .envrc is
        # a shell script whose value semantics are close to
        # dotenv; the zero-drift anchors for the merged-failure
        # fallback to first-line processing match env.
        rows, unclosed_quotes, unclosed_conts = self._logical_lines(text)
        lines = text.splitlines()

        def flush_block(target_path):
            """Bind the pending block comment to the target path
            and write it into the contract."""
            nonlocal pending_block
            if pending_block is None:
                return
            s, e, t = pending_block
            comments.append(cc.make_comment(
                "block", t, "\n".join(lines[s:e + 1]), s, e,
                bound_path=target_path,
                bound_kind="key" if target_path else None,
            ))
            pending_block = None

        def _sync_tail(existing, val, start, end):
            """Duplicate-key tail-value sync (override_chain fully
            present + tail-value governance)."""
            if existing["override_chain"] is None:
                existing["override_chain"] = [
                    {"value": existing["raw_value"],
                     "line": existing["line_start"]},
                    {"value": val, "line": start},
                ]
            else:
                existing["override_chain"].append(
                    {"value": val, "line": start},
                )
            existing["duplicate"] = True
            existing["raw_value"] = val

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

            # direnv directive line: leading token hits the
            # list → directive entry
            first_token = line.split(None, 1)[0]
            if first_token in ENVRC_DIRECTIVE_NAMES:
                flush_block([first_token])
                args = line.split(None, 1)[1] if " " in line else ""
                if first_token in entry_map:
                    existing = entry_map[first_token]
                    _sync_tail(existing, args, start, end)
                    existing["value"] = args
                    existing["line_start"] = start
                    existing["line_end"] = end
                else:
                    entry = cc.make_entry(
                        [first_token], first_token, args, args,
                        line_start=start, line_end=end,
                    )
                    entry["envrc_directive"] = "directive"
                    entry["execute_semantics"] = _EXECUTE_SEMANTICS
                    entries.append(entry)
                    entry_map[first_token] = entry
                continue

            # key-value line (export/assign) — reusing EnvParser
            # value semantics. Gating: the line contains = and
            # the key matches a POSIX identifier (bare tokens are
            # command calls, not assignments; pseudo-keys split
            # out of test lines are rejected)
            kv = self._split_line(line)
            if kv is not None and "=" in line \
                    and _SHELL_NAME_RE.match(kv[0]):
                key, val, exported, meta = kv
                flush_block([key])
                directive = "export" if exported else "assign"
                if key in entry_map:
                    existing = entry_map[key]
                    _sync_tail(existing, val, start, end)
                    existing["value"] = (
                        self._infer_type(val) if self.type_infer else val
                    )
                    existing["value_type"] = (
                        cc.VALUE_TYPE_EMPTY_STRING if val == "" else None
                    )
                    existing["envrc_directive"] = directive
                    existing["exported"] = exported
                    existing["line_start"] = start
                    existing["line_end"] = end
                    existing["original_spacing"] = meta["spacing"]
                    existing["original_quote"] = meta["quote"]
                    if meta["inline_comment"] is not None:
                        existing["inline_comment"] = True
                else:
                    entry = cc.make_entry(
                        [key], key, val,
                        self._infer_type(val) if self.type_infer else val,
                        line_start=start, line_end=end,
                    )
                    if val == "":
                        entry["value_type"] = cc.VALUE_TYPE_EMPTY_STRING
                    entry["envrc_directive"] = directive
                    entry["exported"] = exported
                    if cont is not None:
                        entry["continuation_type"] = cont
                    if isinstance(val, str) and _PLACEHOLDER_RE.match(val):
                        entry["placeholder_candidate"] = True
                    entry["original_spacing"] = meta["spacing"]
                    entry["original_quote"] = meta["quote"]
                    if meta["inline_comment"] is not None:
                        entry["inline_comment"] = True
                        comments.append(cc.make_comment(
                            "inline", meta["inline_comment"],
                            " # " + meta["inline_comment"],
                            start, end, bound_path=[key], bound_kind="key",
                        ))
                    entry_map[key] = entry
                    entries.append(entry)
                continue

            # shell line that cannot be statically identified
            # (function definitions / loops / conditionals / bare
            # token command calls) — honest registration, no
            # guessing (evidence-only)
            flush_block(None)
            unrecognized += 1

        flush_block(None)
        cc.fill_child_counts(entries, contract["containers"])
        # Isomorphic disclosure: command substitution / NUL
        # (detected, never executed). envrc is a shell dialect,
        # so $() can appear on any line (including unrecognized
        # ones); the counting basis is lines (the env family
        # counts entries — the dialect difference is on record).
        cmdsub = sum(
            1 for ln in lines
            if "$(" in ln or "`" in ln
        )
        if cmdsub:
            reasons.append(f"command_substitution_detected:{cmdsub}")
        if "\x00" in text:
            reasons.append("nul_byte_detected:1")
        if unclosed_quotes:
            reasons.append(f"unclosed_quote:{unclosed_quotes}")
        if unclosed_conts:
            reasons.append(f"unclosed_continuation:{unclosed_conts}")
        if unrecognized:
            reasons.append(f"unrecognized_shell_line:{unrecognized}")
        # sensitive-value disclosure (shared two-channel helper;
        # no hit → zero keys zero reasons — zero drift)
        sensitive_count = cc.scan_sensitive_entries(entries)
        if sensitive_count:
            reasons.append(f"sensitive_entry_values:{sensitive_count}")
        try:
            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover
            reasons.append(f"reference_resolution_failed: {e}")
        # isomorphic dialect capability statement (an additive
        # top-level key of this family, not in loss.reasons)
        contract["dialect"] = {
            "profile": "direnv_envrc",
            "export_prefix": "stripped_and_flagged",
            "quotes": "paired",
            "continuation": "backslash+quoted_multiline",
            "interpolation": "identify_only",
            "duplicate_policy": "last_wins",
            "type_inference": bool(self.type_infer),
            "execution": _EXECUTE_SEMANTICS,
        }
        return contract
