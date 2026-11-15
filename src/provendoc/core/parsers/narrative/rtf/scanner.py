# ============================================================
# Narrative Domain -- RTF scanner state machine.
# ============================================================
# Narrative Domain -- RTF scanner state machine.

import re
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.parsers.narrative.rtf.facts import (
    _FONT_FAMILY_WORDS,
    _INFO_SCALAR_KEYS,
    _INFO_TEXT_KEYS,
    _INFO_TIME_KEYS,
    _KNOWN_IGNORED_DESTS,
    _MAC_CODEPAGE_ALIASES,
    _MAX_BOOKMARK_NAME_CHARS,
    _MAX_BOOKMARK_NAMES,
    _MAX_COLOR_ENTRIES,
    _MAX_EMBEDDED_OBJECTS,
    _MAX_FIELD_INSTRUCTION_CHARS,
    _MAX_FIELDS,
    _MAX_FONT_ALIAS_SAMPLES,
    _MAX_FONT_ENTRIES,
    _MAX_FOOTNOTES,
    _MAX_HEADING_CANDIDATES,
    _MAX_LIST_ENTRIES,
    _MAX_LIST_LEVELS,
    _MAX_LIST_OVERRIDE_ENTRIES,
    _MAX_NESTED_RTF_TEXT_CHARS,
    _MAX_OBJDATA_HEX_CHARS,
    _MAX_PARAGRAPH_OFFSETS,
    _MAX_PARAGRAPH_SAMPLES,
    _MAX_PARAGRAPH_TEXT_CHARS,
    _MAX_PICT_HEX_CHARS,
    _MAX_PICT_RECORDS,
    _MAX_REVISION_AUTHOR_SAMPLES,
    _MAX_RSID_SAMPLES,
    _MAX_RUNS_HEAD,
    _MAX_STYLE_ENTRIES,
    _MAX_TABLE_ROWS,
    _MAX_VIEW_TEXT_CHARS,
    _PICT_FORMAT_WORDS,
    _PICT_MAGICS,
    _RESOURCE_DESTINATIONS,
    _RUN_FORMAT_WORDS,
    _SKIP_DESTINATIONS,
    _STRUCTURE_DESTINATIONS,
    _STRUCTURE_DESTINATIONS_V2,
    _SYMBOL_OUTPUT,
    _TRAILING_NL,
    _VIEW_DESTINATIONS,
    _WORD_OUTPUT,
)
from provendoc.core.structure.heading_patterns import HeadingCandidate


def _to_codepage_name(codepage: int | None) -> str:
    """\\ansicpgN -> Python codec name; cp1252 when undeclared (RTF ANSI).

    Mac 10000-series codepages resolve through an alias table
    (10001 -> cp932); unknown or unusable names fall back to cp1252.
    """
    if not codepage:
        return "cp1252"
    cp = int(codepage)
    for name in _MAC_CODEPAGE_ALIASES.get(cp, ()):
        try:
            "test".encode(name)
            return name
        except (LookupError, ValueError):
            continue
    try:
        name = f"cp{cp}"
        "test".encode(name)
        return name
    except (LookupError, ValueError):
        return "cp1252"


_HEADING_NAME_RE = re.compile(r"^heading\s+(\d+)\b", re.IGNORECASE)


def _heading_depth_of(entry: Dict[str, Any]) -> int | None:
    """Style entry -> heading depth; None for non-heading styles.

    Only explicit declarations count: a "heading N" style name or
    \\outlinelev0-8 in the style definition.
    """
    name = str(entry.get("name", ""))
    m = _HEADING_NAME_RE.match(name.strip())
    if m:
        return int(m.group(1))
    ol = entry.get("outline_level")
    if isinstance(ol, int) and 0 <= ol < 9:
        return ol + 1
    return None


# ------------------------------------------------------------
# State machine helpers (module-level pure functions)
# ------------------------------------------------------------

def _read_control_word(text: str, i: int) -> tuple[str, int | None, int]:
    """Read the control word starting at i (first char after \\).

    Returns (word, param, next_i); param is a signed decimal
    (``\\u8364`` -> ("u", 8364)) or None. A non-digit ends the
    parameter run (RTF grammar).
    """
    j = i
    n = len(text)
    while j < n and text[j].isalpha() and ord(text[j]) < 128:
        j += 1
    word = text[i:j]
    param = None
    if j < n and text[j] == "-":
        k = j + 1
        digits = _digits_at(text, k)
        if digits:
            param = -int(digits)
            j = k + len(digits)
    elif j < n and text[j].isdigit():
        digits = _digits_at(text, j)
        param = int(digits)
        j = j + len(digits)
    # A single space after a control word is a delimiter (RTF grammar).
    if j < n and text[j] == " ":
        j += 1
    return word, param, j


def _digits_at(text: str, i: int) -> str:
    n = len(text)
    j = i
    while j < n and text[j].isdigit():
        j += 1
    return text[i:j]


class _Frame:
    """Group frame: push on {, pop on }.

    skip_name marks the group for discarding; collect redirects
    content to a collection buffer (fonttbl/colortbl/info/pict/ud/
    generator/stylesheet/listtable/object/header_footer/footnote/
    annotation/objclass/leveltext). collect overrides an outer
    skip -- nested destinations re-scope, e.g. \\*\\shppict with
    a nested {\\pict}.
    view_kind: header/footer kind of the collecting frame.
    revision: "ins"/"del" routing for the group's text.
    """

    __slots__ = (
        "skip_name", "collect", "uc", "unicode_skip",
        "upr", "orphan_surrogate", "fmt_snapshot",
        "view_kind", "revision",
    )

    def __init__(self, uc: int, skip_name: str | None = None,
                 collect: str | None = None):
        self.skip_name = skip_name
        self.collect = collect
        self.uc = uc               # \ucN inherited within the group
        self.unicode_skip = 0      # fallback items to skip after \uN
        self.upr = False           # \upr Unicode primary group
        self.orphan_surrogate = False  # orphan surrogate (primary corrupted)
        self.fmt_snapshot: Dict[str, Any] | None = None  # format scope snapshot
        self.view_kind: str | None = None   # header/footer kind
        self.revision: str | None = None    # ins/del revision routing


def _pict_magic(data: bytes) -> str | None:
    """Map byte magic to a format label; None when unknown."""
    for magic, fmt in _PICT_MAGICS:
        if data.startswith(magic):
            return fmt
    return None


def _decode_pict_hex(hex_buf: List[str]) -> Tuple[bytes | None, str | None]:
    """pict group hex char stream -> bytes; (None, reason) on corruption.

    Whitespace is filtered (RTF line-wrapping); odd length or
    non-hex characters are registered in reason -- never silently
    dropped, never fabricated.
    """
    joined = "".join(hex_buf)
    stripped = "".join(ch for ch in joined if not ch.isspace())
    if not stripped:
        return None, None  # empty: caller decides via pict_ctx
    if len(stripped) % 2 != 0:
        return None, "INVALID_PICT_HEX"
    try:
        return bytes.fromhex(stripped), None
    except ValueError:
        # non-hex chars: drop them and retry; keep the honest reason
        cleaned = "".join(
            ch for ch in stripped if ch in "0123456789abcdefABCDEF"
        )
        if len(cleaned) % 2 != 0 or not cleaned:
            return None, "CORRUPTED_PICT_HEX"
        try:
            return bytes.fromhex(cleaned), "CORRUPTED_PICT_HEX"
        except ValueError:
            return None, "CORRUPTED_PICT_HEX"


def _extract_rtf(text: str) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    r"""RTF scanner entry: returns (extracted text, rtf_structure, pict records).

    Single pass over the decoded source with a group stack: output
    routing follows the innermost active frame -- collect buffers
    override an outer skip, skip frames discard, body frames emit.
    """
    return _RtfScanner(text).run()


class _RtfScanner:
    r"""RTF single-pass scanner (implementation of _extract_rtf).

    run() dispatches on group open / group close / escape / plain
    char; control words dispatch a second level by collect domain
    and semantic family.
    """

    def __init__(self, text: str) -> None:
        """Initialize scanner state from decoded RTF source."""
        self.text = text
        self.n = len(text)
        self.i = 0
        self.out: List[str] = []          # body output (rendered semantics)
        self.frames: List[_Frame] = []
        self.depth = 0
        self.depth_max = 0
        self.unbalanced = False
        self.codepage: int | None = None
        self.pending_high: int | None = None  # pending surrogate high

        # global counters
        self.control_word_count = 0
        self.paragraph_count = 0
        self.line_break_count = 0
        self.tab_count = 0
        self.page_break_count = 0
        self.table_row_count = 0
        self.unicode_escape_count = 0
        self.surrogate_pair_count = 0
        self.hex_escape_count = 0
        self.binary_bytes_skipped = 0
        self.field_count = 0
        self.hyperlink_count = 0
        self.destination_counts: Dict[str, int] = {}
        self.outline_level_counts: Dict[str, int] = {}
        self.fldinst_buf: List[str] = []  # \*\fldinst instructions (HYPERLINK count)
        self.upr_count = 0
        self.ud_fallback_used = 0

        # collectors (shared across subgroups: font entries are {\f0..} subgroups)
        self.font_entries: List[Dict[str, Any]] = []
        # first colortbl slot can be None (auto color)
        self.color_entries: List[Optional[Dict[str, Any]]] = []
        self.document_info: Dict[str, Any] = {}
        self.generator_text: List[str] = []
        self.ud_text: List[str] = []
        self.pict_records: List[Dict[str, Any]] = []

        # active collection context
        self.cur_font: Dict[str, Any] = {}
        self.font_name_buf: List[str] = []
        self.rgb_cur: Dict[str, int] = {}
        self.info_key: str | None = None
        self.info_val: List[str] = []
        self.info_raw: List[str] = []     # raw control words of time groups
        self.pict_hex_buf: List[str] = []
        self.pict_hex_overflow = False
        self.pict_ctx: Dict[str, Any] = {}

        # ---- structure collectors (v2) ----
        # style table
        self.style_entries: List[Dict[str, Any]] = []
        self.cur_style: Dict[str, Any] = {}
        self.style_name_buf: List[str] = []
        self.style_truncated = False
        # paragraph samples + heading candidates (judged at scan end:
        # the style table must be complete first)
        self.para_style: Optional[int] = None
        self.para_outline_level: Optional[int] = None
        self.par_start = 0
        self.paragraphs_head: List[Dict[str, Any]] = []
        self.paragraphs_samples_truncated = False
        self.heading_candidates: List[HeadingCandidate] = []
        self.heading_truncated = False
        # list tables
        self.list_table: List[Dict[str, Any]] = []
        self.list_truncated = False
        self.cur_list: Dict[str, Any] = {}
        self.cur_level: Dict[str, Any] = {}
        self.leveltext_buf: List[str] = []
        self.list_override_table: List[Dict[str, Any]] = []
        self.list_override_truncated = False
        self.cur_override: Dict[str, Any] = {}
        # header/footer, footnotes, sections
        self.header_footer_view: Dict[str, List[str]] = {}
        self.hf_buf: List[str] = []
        self.hf_truncated = False
        self.footnotes: List[str] = []
        self.footnotes_total = 0
        self.footnotes_truncated = False
        self.footnote_buf: List[str] = []
        self.footnote_ref_count = 0
        self.sections_total = 0
        # fields, bookmarks, annotations, revisions
        self.fields: List[Dict[str, Any]] = []
        self.fields_total = 0
        self.fields_truncated = False
        self.bookmark_names: List[str] = []
        self.bookmarks_total = 0
        self.bookmark_truncated = False
        self.annotation_view: List[str] = []
        self.annotation_truncated = False
        self.annotation_buf: List[str] = []
        self.revision_inserted: List[str] = []
        self.revision_deleted: List[str] = []
        self.rev_ins_total = 0
        self.rev_del_total = 0
        self.rev_ins_buf: List[str] = []
        self.rev_del_buf: List[str] = []
        # revision author / session id / font alias census
        self.revtbl_buf: List[str] = []
        self.falt_buf: List[str] = []
        self.revision_author_total = 0
        self.revision_author_samples: List[str] = []
        self.rsid_total = 0
        self.rsid_samples: List[int] = []
        self.font_alias_total = 0
        self.font_alias_samples: List[str] = []
        # hidden text (kept in main, also registered)
        self.hidden_text_buf: List[str] = []
        # embedded objects
        self.embedded_objects: List[Dict[str, Any]] = []
        self.embedded_objects_total = 0
        self.embedded_truncated = False
        self.obj_class_buf: List[str] = []
        self.obj_class_value: Optional[str] = None
        # unstarred \objclass name collection: a control word opens
        # the stream, the next control word or group close ends it;
        # mutually exclusive with the objdata hex stream.
        self.obj_class_active = False
        self.objdata_hex_buf: List[str] = []
        self.objdata_active = False
        self.objdata_overflow = False
        self.obj_ctx: Dict[str, Any] = {}
        # \protected run flag count (declaration only; key written
        # only when nonzero)
        self.protected_word_count = 0
        # group recovery evidence + cell merges
        self.early_closes = 0
        self.cell_merge_count = 0

        # table structure
        self.table_rows: List[Dict[str, Any]] = []
        self.table_rows_total = 0
        self.table_rows_truncated = False
        self.row_cellx: List[int] = []
        self.row_cells: List[str] = []
        self.cell_start = 0
        self.in_table_row = False

        # run tracking (body frames only)
        self.fmt_state: Dict[str, Any] = {}
        self.run_open: Dict[str, Any] | None = None
        self.run_stats = {"total": 0, "truncated": False}
        self.runs_head: List[Dict[str, Any]] = []

        self.paragraph_offsets: List[int] = []
        self.paragraph_offsets_total = 0
        self.unknown_destinations: Dict[str, int] = {}
        self.reason_codes: List[str] = []

    # ---- counting / reason registration ----

    def _count_destination(self, name: str) -> None:
        self.destination_counts[name] = self.destination_counts.get(name, 0) + 1

    def _add_reason(self, code: str) -> None:
        if code not in self.reason_codes:
            self.reason_codes.append(code)

    # ---- frame navigation ----

    def _current_frame(self) -> _Frame | None:
        return self.frames[-1] if self.frames else None

    def _in_skip(self) -> bool:
        # innermost active frame wins: collect overrides an outer
        # skip (nested destinations re-scope, e.g. \*\shppict with
        # a nested {\pict} whose hex still needs collecting)
        for f in reversed(self.frames):
            if f.collect is not None:
                return False
            if f.skip_name is not None:
                return True
        return False

    def _active_collect(self) -> str | None:
        for f in reversed(self.frames):
            if f.collect is not None:
                return f.collect
        return None

    def _in_fldinst(self) -> bool:
        for f in reversed(self.frames):
            if f.collect is not None:
                return False
            if f.skip_name == "*fldinst":
                return True
        return False

    def _in_upr(self) -> bool:
        return any(f.upr for f in self.frames)

    # ---- emit / run tracking ----

    def _emit(self, ch: str) -> None:
        """Emit routing: collect domains go to buffers; skip domains
        drop; body frames append to out and track runs. Deleted
        revision text stays out of main (kept in revision_view);
        inserted revision and hidden text keep main + view copy."""
        if self._in_skip():
            return
        coll = self._active_collect()
        if coll == "fonttbl":
            self.font_name_buf.append(ch)
        elif coll == "colortbl":
            pass  # colortbl has no text semantics (control words handled)
        elif coll == "revtbl":
            # author-name text stream (split on ";" at flush)
            self.revtbl_buf.append(ch)
        elif coll == "rsidtbl":
            pass  # no text semantics (\rsidN handled as control word)
        elif coll == "falt":
            # font-alias name text stream (trailing ";" stripped at flush)
            self.falt_buf.append(ch)
        elif coll == "info":
            self.info_val.append(ch)
        elif coll == "ud":
            self.ud_text.append(ch)
        elif coll == "generator":
            self.generator_text.append(ch)
        elif coll == "pict":
            if len(self.pict_hex_buf) < _MAX_PICT_HEX_CHARS:
                self.pict_hex_buf.append(ch)
            else:
                self.pict_hex_overflow = True
        elif coll == "stylesheet":
            self.style_name_buf.append(ch)
        elif coll == "leveltext":
            self.leveltext_buf.append(ch)
        elif coll == "objclass":
            self.obj_class_buf.append(ch)
        elif coll == "annotation":
            if len(self.annotation_buf) < _MAX_VIEW_TEXT_CHARS:
                self.annotation_buf.append(ch)
        elif coll == "header_footer":
            if len(self.hf_buf) < _MAX_VIEW_TEXT_CHARS:
                self.hf_buf.append(ch)
        elif coll == "footnote":
            if len(self.footnote_buf) < _MAX_VIEW_TEXT_CHARS:
                self.footnote_buf.append(ch)
        elif coll == "object":
            # objdata hex stream (non-hex chars diverted earlier);
            # unstarred \objclass name text stream.
            if self.objdata_active:
                if len(self.objdata_hex_buf) < _MAX_OBJDATA_HEX_CHARS:
                    self.objdata_hex_buf.append(ch)
                else:
                    self.objdata_overflow = True
            elif self.obj_class_active:
                if len(self.obj_class_buf) < 256:
                    self.obj_class_buf.append(ch)
        elif coll == "objdata":
            # starred objdata group: residual chars after the hex
            # stream are dropped (group boundary = stream boundary)
            pass
        else:
            # body frame: revision routing before run tracking
            rev = None
            for f in reversed(self.frames):
                if f.revision is not None:
                    rev = f.revision
                    break
            if rev == "del":
                if len(self.rev_del_buf) < _MAX_VIEW_TEXT_CHARS:
                    self.rev_del_buf.append(ch)
                return
            if rev == "ins":
                if len(self.rev_ins_buf) < _MAX_VIEW_TEXT_CHARS:
                    self.rev_ins_buf.append(ch)
            if self.fmt_state.get("hidden"):
                if len(self.hidden_text_buf) < _MAX_VIEW_TEXT_CHARS:
                    self.hidden_text_buf.append(ch)
            if self.run_open is None:
                self.run_open = {
                    "start": len(self.out),
                    "end": len(self.out),
                    "bold": self.fmt_state.get("bold", False),
                    "italic": self.fmt_state.get("italic", False),
                    "underline": self.fmt_state.get("underline", False),
                    "strike": self.fmt_state.get("strike", False),
                    "hidden": self.fmt_state.get("hidden", False),
                    "super": self.fmt_state.get("super", False),
                    "sub": self.fmt_state.get("sub", False),
                    "font_size": self.fmt_state.get("font_size"),
                    "color_idx": self.fmt_state.get("color_idx"),
                }
            self.run_open["end"] = len(self.out) + 1
            self.out.append(ch)

    def _close_run(self) -> None:
        """Close the current run; non-empty spans count."""
        if self.run_open is not None:
            if self.run_open["end"] > self.run_open["start"]:
                self.run_stats["total"] += 1
                if len(self.runs_head) < _MAX_RUNS_HEAD:
                    self.runs_head.append(dict(self.run_open))
                elif not self.run_stats["truncated"]:
                    self.run_stats["truncated"] = True
                    self._add_reason("RUNS_TRUNCATED")
            self.run_open = None

    def _fmt_changed(self) -> None:
        """Format state changed: close the current run."""
        self._close_run()

    # ---- v1/v2 flush (collection close-out) ----

    def _flush_font_entry(self) -> None:
        if self.cur_font or self.font_name_buf:
            name = "".join(self.font_name_buf).strip()
            entry = dict(self.cur_font)
            if name:
                entry["name"] = name
            if len(self.font_entries) < _MAX_FONT_ENTRIES:
                self.font_entries.append(entry)
            elif "FONT_TABLE_TRUNCATED" not in self.reason_codes:
                self.reason_codes.append("FONT_TABLE_TRUNCATED")
        self.cur_font = {}
        self.font_name_buf.clear()

    def _flush_color_entry(self) -> None:
        if self.rgb_cur:
            if len(self.color_entries) < _MAX_COLOR_ENTRIES:
                self.color_entries.append(
                    {"red": self.rgb_cur.get("red"),
                     "green": self.rgb_cur.get("green"),
                     "blue": self.rgb_cur.get("blue")}
                )
            self.rgb_cur = {}
        elif not self.color_entries:
            # first empty slot = auto color (colortbl spec)
            self.color_entries.append(None)
        # other empty slots (truncated/corrupt tail): dropped

    def _flush_info_entry(self) -> None:
        if self.info_key is not None:
            if self.info_key in _INFO_TIME_KEYS:
                raw = " ".join(self.info_raw)
                if raw and self.info_key not in self.document_info:
                    self.document_info[self.info_key] = raw
                self.info_raw.clear()
            else:
                val = "".join(self.info_val).strip()
                if val and self.info_key not in self.document_info:
                    self.document_info[self.info_key] = val
                self.info_val.clear()
        self.info_key = None

    def _flush_pict(self) -> None:
        hex_chars = sum(len(s) for s in self.pict_hex_buf)
        if hex_chars > 0 or self.pict_ctx:
            data, decode_reason = _decode_pict_hex(self.pict_hex_buf)
            record: Dict[str, Any] = {
                "format": self.pict_ctx.get("format"),
                "picw": self.pict_ctx.get("picw"),
                "pich": self.pict_ctx.get("pich"),
                "picwgoal": self.pict_ctx.get("picwgoal"),
                "pichgoal": self.pict_ctx.get("pichgoal"),
                "hex_chars": hex_chars,
            }
            if self.pict_ctx.get("bin_bytes"):
                # \binN binary picture data: bytes skipped, not decoded
                record["extracted"] = False
                record["reason"] = "BIN_PICT_NOT_DECODED"
                self._add_reason("BIN_PICT_NOT_DECODED")
            elif data is not None:
                magic = _pict_magic(data)
                record["byte_len"] = len(data)
                record["magic"] = magic
                record["extracted"] = True
                if self.pict_hex_overflow:
                    record["reason"] = "TRUNCATED_PICT_DATA"
                    self._add_reason("TRUNCATED_PICT_DATA")
                from provendoc.core.image_assets.schema import (
                    asset_id_for,
                )
                record["asset_id"] = asset_id_for(data)
                record["_bytes"] = data
                if magic is None:
                    record["reason"] = "UNKNOWN_IMAGE_MAGIC"
                    self._add_reason("UNKNOWN_IMAGE_MAGIC")
            else:
                record["extracted"] = False
                record["reason"] = (
                    decode_reason or "MISSING_IMAGE_DATA"
                )
                self._add_reason(decode_reason or "MISSING_IMAGE_DATA")
            if len(self.pict_records) < _MAX_PICT_RECORDS:
                self.pict_records.append(record)
        self.pict_hex_buf = []
        self.pict_hex_overflow = False
        self.pict_ctx = {}

    def _flush_cell(self) -> None:
        # cell_start is reset by the \cell handler after appending
        # the tab, or the separator tab leaks into the next cell
        self.row_cells.append("".join(self.out[self.cell_start:]))

    def _flush_row(self) -> None:
        if self.in_table_row:
            self.table_rows_total += 1
            if len(self.table_rows) < _MAX_TABLE_ROWS:
                self.table_rows.append({
                    "cells": list(self.row_cells),
                    "cellx": list(self.row_cellx),
                })
            elif not self.table_rows_truncated:
                self.table_rows_truncated = True
                self._add_reason("TABLE_STRUCTURE_TRUNCATED")
        self.in_table_row = False
        self.row_cellx = []
        self.row_cells = []

    # ---- structure flush ----

    def _flush_style_entry(self) -> None:
        r"""Style entry close-out (\sN/\csN subgroup close or residual guard)."""
        if self.cur_style:
            name = "".join(self.style_name_buf).strip()
            entry = dict(self.cur_style)
            if name:
                entry["name"] = name[:_MAX_PARAGRAPH_TEXT_CHARS]
            if len(self.style_entries) < _MAX_STYLE_ENTRIES:
                self.style_entries.append(entry)
            elif not self.style_truncated:
                self.style_truncated = True
                self._add_reason("STYLE_TABLE_TRUNCATED")
        self.cur_style = {}
        self.style_name_buf.clear()

    def _flush_level(self) -> None:
        """Single listlevel entry close-out."""
        if self.cur_level:
            levels = self.cur_list.setdefault("levels", [])
            if len(levels) < _MAX_LIST_LEVELS:
                levels.append(dict(self.cur_level))
            elif not self.list_truncated:
                self.list_truncated = True
                self._add_reason("LIST_TABLE_TRUNCATED")
        self.cur_level = {}
        self.leveltext_buf.clear()

    def _flush_list_entry(self) -> None:
        r"""\list entry close-out."""
        if self.cur_list:
            if len(self.list_table) < _MAX_LIST_ENTRIES:
                self.list_table.append(dict(self.cur_list))
            elif not self.list_truncated:
                self.list_truncated = True
                self._add_reason("LIST_TABLE_TRUNCATED")
        self.cur_list = {}
        self._flush_level()

    def _flush_override(self) -> None:
        """listoverride entry close-out."""
        if self.cur_override:
            if len(self.list_override_table) < _MAX_LIST_OVERRIDE_ENTRIES:
                self.list_override_table.append(dict(self.cur_override))
            elif not self.list_override_truncated:
                self.list_override_truncated = True
                self._add_reason("LIST_OVERRIDE_TABLE_TRUNCATED")
        self.cur_override = {}

    def _flush_hf(self, kind: str | None) -> None:
        """Header/footer group close-out -> header_footer_view[kind]."""
        text = "".join(self.hf_buf).strip()
        if text:
            bucket = self.header_footer_view.setdefault(
                kind or "unknown", [],
            )
            bucket.append(text[:_MAX_VIEW_TEXT_CHARS])
            if len(text) > _MAX_VIEW_TEXT_CHARS:
                self.hf_truncated = True
                self._add_reason("VIEW_TEXT_TRUNCATED")
        self.hf_buf.clear()

    def _flush_footnote(self) -> None:
        """Footnote group close-out -> footnotes list."""
        text = "".join(self.footnote_buf).strip()
        if text:
            self.footnotes_total += 1
            if len(self.footnotes) < _MAX_FOOTNOTES:
                self.footnotes.append(text[:_MAX_VIEW_TEXT_CHARS])
            elif not self.footnotes_truncated:
                self.footnotes_truncated = True
                self._add_reason("FOOTNOTES_TRUNCATED")
        self.footnote_buf.clear()

    def _flush_annotation(self) -> None:
        """Annotation group close-out -> annotation_view."""
        text = "".join(self.annotation_buf).strip()
        if text:
            self.annotation_view.append(text[:_MAX_VIEW_TEXT_CHARS])
            if len(text) > _MAX_VIEW_TEXT_CHARS:
                self.annotation_truncated = True
                self._add_reason("VIEW_TEXT_TRUNCATED")
        self.annotation_buf.clear()

    def _flush_field_instruction(self) -> None:
        """fldinst group close-out -> field samples + hyperlink count."""
        instruction = "".join(self.fldinst_buf)
        self.fields_total += 1
        self.hyperlink_count += instruction.count("HYPERLINK")
        if instruction.strip():
            if len(self.fields) < _MAX_FIELDS:
                self.fields.append({
                    "instruction_head": (
                        instruction[:_MAX_FIELD_INSTRUCTION_CHARS].strip()
                    ),
                    "is_hyperlink": "HYPERLINK" in instruction,
                })
            elif not self.fields_truncated:
                self.fields_truncated = True
                self._add_reason("FIELDS_TRUNCATED")
        self.fldinst_buf.clear()

    def _flush_revision(self, kind: str) -> None:
        """Revision group close-out -> view copy (ins keeps main)."""
        if kind == "ins":
            self.rev_ins_total += 1
            text = "".join(self.rev_ins_buf).strip()
            if text:
                self.revision_inserted.append(text[:_MAX_VIEW_TEXT_CHARS])
            self.rev_ins_buf.clear()
        elif kind == "del":
            self.rev_del_total += 1
            text = "".join(self.rev_del_buf).strip()
            if text:
                self.revision_deleted.append(text[:_MAX_VIEW_TEXT_CHARS])
            self.rev_del_buf.clear()

    def _flush_object(self) -> None:
        r"""\object group close-out -> embedded_objects entry
        (objdata hex decode + magic + nested {\rtf recursion)."""
        hex_chars = sum(len(s) for s in self.objdata_hex_buf)
        # unstarred objclass name wins over the starred form
        # (first writer keeps the value)
        obj_class = self.obj_class_value
        if obj_class is None:
            obj_class = "".join(self.obj_class_buf).strip() or None
        record: Dict[str, Any] = {
            "class": obj_class,
            "hex_chars": hex_chars,
        }
        if self.obj_ctx.get("objsize") is not None:
            record["objsize_declared"] = self.obj_ctx["objsize"]
        # OLE auto-behavior flags: declared-only registration,
        # never executed (flag name = control word name)
        for _flag in ("objautlink", "objupdate"):
            if self.obj_ctx.get(_flag):
                record[_flag] = True
        if hex_chars > 0:
            data, decode_reason = _decode_pict_hex(self.objdata_hex_buf)
            if data is not None:
                record["data_bytes"] = len(data)
                record["magic"] = _pict_magic(data)
                record["extracted"] = True
                if self.objdata_overflow:
                    record["reason"] = "TRUNCATED_OBJDATA"
                    self._add_reason("TRUNCATED_OBJDATA")
                if data.startswith(b"{\\rtf"):
                    record["nested_rtf"] = True
                    try:
                        nested = _extract_rtf(data.decode("latin-1"))
                        record["nested_rtf_text_head"] = nested[0][
                            :_MAX_NESTED_RTF_TEXT_CHARS
                        ]
                    except Exception:
                        record["nested_rtf_extract_failed"] = True
                else:
                    record["nested_rtf"] = False
            else:
                record["extracted"] = False
                record["reason"] = (
                    decode_reason or "MISSING_OBJECT_DATA"
                )
                self._add_reason(decode_reason or "MISSING_OBJECT_DATA")
        else:
            record["extracted"] = False
            record["reason"] = "MISSING_OBJECT_DATA"
            self._add_reason("MISSING_OBJECT_DATA")
        self.embedded_objects_total += 1
        if len(self.embedded_objects) < _MAX_EMBEDDED_OBJECTS:
            self.embedded_objects.append(record)
        elif not self.embedded_truncated:
            self.embedded_truncated = True
            self._add_reason("EMBEDDED_OBJECTS_TRUNCATED")
        self.objdata_hex_buf.clear()
        self.objdata_active = False
        self.objdata_overflow = False
        self.obj_class_value = None
        self.obj_class_buf.clear()
        self.obj_class_active = False
        self.obj_ctx = {}

    def _close_paragraph(self) -> None:
        r"""Paragraph close-out (\par / EOF) -> paragraph sample;
        paragraph attributes reset (undeclared = not a heading)."""
        text = "".join(self.out[self.par_start:]).strip("\n")
        if (
            text.strip()
            or self.para_style is not None
            or self.para_outline_level is not None
        ):
            if len(self.paragraphs_head) < _MAX_PARAGRAPH_SAMPLES:
                self.paragraphs_head.append({
                    "text": text[:_MAX_PARAGRAPH_TEXT_CHARS],
                    "style": self.para_style,
                    "outline_level": self.para_outline_level,
                    "start": self.par_start,
                    "end": len(self.out),
                })
            elif not self.paragraphs_samples_truncated:
                self.paragraphs_samples_truncated = True
                self._add_reason("PARAGRAPH_SAMPLES_TRUNCATED")
        self.par_start = len(self.out)
        self.para_style = None
        self.para_outline_level = None

    # ---- main loop dispatch ----

    def run(self) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
        """Single-pass main loop: group open / close / escape / plain char."""
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "{":
                self._open_group()
            elif ch == "}":
                self._close_group()
            elif ch == "\\":
                self._handle_escape()
            else:
                self._handle_char(ch)
        return self._finish()

    def _open_group(self) -> None:
        parent = self.frames[-1] if self.frames else None
        new_frame = _Frame(
            uc=parent.uc if parent else 1,
            # collect inheritance (fonttbl/stylesheet entry subgroups
            # flush on close); object is the exception: its subgroups
            # are not independent entries -- inherited collect would
            # falsely trigger _flush_object on a subgroup close.
            collect=(
                parent.collect
                if parent and parent.collect != "object"
                else None
            ),
        )
        self.frames.append(new_frame)
        self.depth += 1
        if self.depth > self.depth_max:
            self.depth_max = self.depth
        # group-level format scope snapshot (restored on close;
        # body frames only)
        if self._active_collect() is None:
            new_frame.fmt_snapshot = dict(self.fmt_state)
        self.i += 1

    def _close_group(self) -> None:
        if not self.frames:
            self.unbalanced = True  # extra closing brace (truncated/corrupt)
            self.early_closes += 1
            self.i += 1
            return
        popped = self.frames.pop()
        self.depth -= 1
        # collect frame close: flush its context
        self._flush_collect_frame(popped)
        # fldinst group close-out (skip-frame observation)
        if popped.skip_name == "*fldinst":
            self._flush_field_instruction()
        # revision group close-out (view copies)
        if popped.revision is not None:
            self._flush_revision(popped.revision)
        if popped.upr:
            self._close_upr_group(popped)
        # restore format scope + close run (only when the state
        # actually changed)
        if popped.fmt_snapshot is not None and (
            self.fmt_state != popped.fmt_snapshot
        ):
            self.fmt_state.clear()
            self.fmt_state.update(popped.fmt_snapshot)
            self._fmt_changed()
        self.i += 1

    def _flush_collect_frame(self, popped: _Frame) -> None:
        """Collect frame close dispatch: flush by collect domain."""
        coll = popped.collect
        if coll == "fonttbl":
            self._flush_font_entry()
        elif coll == "colortbl":
            self._flush_color_entry()
        elif coll == "info":
            self._flush_info_entry()
        elif coll == "pict":
            self._flush_pict()
        elif coll == "stylesheet":
            self._flush_style_entry()
        elif coll == "leveltext":
            # leveltext subgroup close-out (decoded chars kept as-is)
            if self.cur_level:
                self.cur_level["leveltext"] = "".join(self.leveltext_buf)
            self.leveltext_buf.clear()
        elif coll == "listlevel":
            self._flush_level()
        elif coll == "listtable":
            self._flush_list_entry()
        elif coll == "listoverridetable":
            self._flush_override()
        elif coll == "header_footer":
            self._flush_hf(popped.view_kind)
        elif coll == "footnote":
            self._flush_footnote()
        elif coll == "annotation":
            self._flush_annotation()
        elif coll == "objclass":
            # objclass name close-out (consumed by _flush_object)
            self.obj_class_value = "".join(self.obj_class_buf).strip() or None
            self.obj_class_buf.clear()
        elif coll == "objdata":
            # starred objdata group close: group boundary = stream
            # boundary (buffer kept until the object group closes)
            self.objdata_active = False
        elif coll == "object":
            self._flush_object()
        elif coll == "revtbl":
            self._flush_revtbl()
        elif coll == "falt":
            self._flush_falt()

    def _close_upr_group(self, popped: _Frame) -> None:
        r"""\upr group close: a leftover orphan surrogate marks the
        primary version as corrupted; withdraw it and fall back to
        \*\ud text (reason registered)."""
        orphan = popped.orphan_surrogate
        if self.pending_high is not None:
            if self.out and self.out[-1] == chr(self.pending_high):
                self.out.pop()
            orphan = True
            self.pending_high = None
        if orphan:
            if self.ud_text:
                self.out.append("".join(self.ud_text))
                self.ud_fallback_used += 1
            self._add_reason("UNICODE_PRIMARY_CORRUPTED")
        self.ud_text.clear()

    # ---- escape branch ----

    def _handle_escape(self) -> None:
        text, n, i = self.text, self.n, self.i
        if i + 1 >= n:
            self.i = n  # end of scan
            return
        nxt = text[i + 1]
        # literals and control symbols
        if nxt in ("\\", "{", "}"):
            self._emit(nxt)
            self.i += 2
            return
        if nxt in _SYMBOL_OUTPUT:
            self._emit(_SYMBOL_OUTPUT[nxt])
            self.i += 2
            return
        if nxt == "*":
            self._handle_star_destination()
            return
        if nxt == "-" or nxt == "_":
            self.i += 2  # \- \_ optional/soft hyphen: no output
            return
        if nxt == "'":
            self._handle_hex_escape()
            return
        if nxt.isalpha() and ord(nxt) < 128:
            word, param, j = _read_control_word(text, i + 1)
            self.control_word_count += 1
            self._handle_control_word(word, param, j)
            return
        # unrecognized \x control symbol: swallowed silently
        self.i += 2

    def _handle_star_destination(self) -> None:
        r"""Ignorable destination: \* followed by a control word marks this group as skip/collect."""
        text, n, i = self.text, self.n, self.i
        j = i + 2
        while j < n and text[j] == " ":
            j += 1
        if j < n and text[j] == "\\":
            j += 1
        if j < n and text[j].isalpha() and ord(text[j]) < 128:
            word, _, j2 = _read_control_word(text, j)
            frame = self._current_frame()
            if frame is not None:
                if word == "ud":
                    # \*\ud collected as fallback text
                    frame.skip_name = None
                    frame.collect = "ud"
                    self._count_destination("*ud")
                elif word == "generator":
                    # \*\generator text registration
                    frame.skip_name = None
                    frame.collect = "generator"
                    self._count_destination("*generator")
                elif word == "fldinst":
                    frame.skip_name = "*fldinst"
                    frame.collect = None
                    self._count_destination("*fldinst")
                elif word == "annotation":
                    # \*\annotation collected (annotation view)
                    frame.skip_name = None
                    frame.collect = "annotation"
                    self._count_destination("*annotation")
                elif word == "revtbl":
                    # \*\revtbl author table collected
                    # (semicolon-separated names, verbatim)
                    frame.skip_name = None
                    frame.collect = "revtbl"
                    self._count_destination("*revtbl")
                elif word == "rsidtbl":
                    # \*\rsidtbl collected (\rsidN decimals, verbatim)
                    frame.skip_name = None
                    frame.collect = "rsidtbl"
                    self._count_destination("*rsidtbl")
                elif word == "falt":
                    # \*\falt font-alias names collected verbatim
                    frame.skip_name = None
                    frame.collect = "falt"
                    self._count_destination("*falt")
                elif word == "objclass":
                    # \*\objclass name collection
                    frame.skip_name = None
                    frame.collect = "objclass"
                    self._count_destination("*objclass")
                elif word == "objdata":
                    # \*\objdata hex stream collection (starred
                    # form; group boundary = stream boundary, buffer
                    # kept until the object group closes)
                    frame.skip_name = None
                    frame.collect = "objdata"
                    self.objdata_active = True
                    self._count_destination("*objdata")
                elif word in ("listtable", "listoverridetable"):
                    # \*\listtable / \*\listoverridetable collected
                    # (starred form is the common real-world shape);
                    # same path as the unstarred form: collect frames
                    # + subgroup-close flushes (fonttbl mechanism)
                    frame.skip_name = None
                    frame.collect = word
                    self._count_destination(f"*{word}")
                else:
                    if word not in _KNOWN_IGNORED_DESTS:
                        self.unknown_destinations[word] = (
                            self.unknown_destinations.get(word, 0) + 1
                        )
                        self._add_reason("UNKNOWN_RTF_DESTINATION")
                    frame.skip_name = f"*{word}"
                    frame.collect = None
                    self._count_destination(f"*{word}")
            self.i = j2
            return
        self.i += 2  # \* with no control word: swallow marker

    def _handle_hex_escape(self) -> None:
        r"""\'hh hex escape: decoded via the declared codepage.

        Consecutive \'hh items are buffered and decoded as one run,
        so multi-byte codepages (e.g. Mac Japanese 10001 -> cp932)
        keep their lead/trail byte pairs instead of collapsing to
        U+FFFD; \uN fallback items appearing as \'hh are swallowed
        one by one. Single-byte codepages are unaffected.
        """
        text, i = self.text, self.i
        frame0 = self._current_frame()
        buf = bytearray()
        j = i
        while True:
            # later items must carry their own \' prefix (RTF grammar):
            # int() tolerates whitespace and signs, so without the
            # check arbitrary following characters could be misread
            # as hex.
            if j > i and text[j:j + 2] != "\\'":
                break
            part = text[j + 2:j + 4]
            if len(part) != 2:
                break
            try:
                byte_val = int(part, 16)
            except ValueError:
                break
            if not 0 <= byte_val <= 255:
                break  # defensive end of aggregation
            if frame0 is not None and frame0.unicode_skip > 0:
                # \uN fallback item: swallowed, not buffered
                frame0.unicode_skip -= 1
                self.hex_escape_count += 1
                j += 4
                continue
            buf.append(byte_val)
            self.hex_escape_count += 1
            j += 4
        if j == i:
            # first item truncated/invalid: swallow marker or U+FFFD + reason
            part = text[i + 2:i + 4]
            if len(part) == 2:
                self.hex_escape_count += 1
                self._add_reason("INVALID_HEX_ESCAPE")
                self._emit("\ufffd")
                self.i = i + 4
            else:
                self.i = i + 2
            return
        if buf:
            try:
                decoded = bytes(buf).decode(
                    _to_codepage_name(self.codepage), errors="replace"
                )
            except (LookupError, ValueError):
                decoded = "\ufffd" * len(buf)
            self._emit(decoded)
        self.i = j

    # ---- control word dispatch ----

    def _handle_control_word(self, word: str, param: int | None, j: int) -> None:
        frame = self._current_frame()

        # \ucN / \ansicpgN / \outlinelevN global evidence first
        if word == "uc" and param is not None:
            if frame is not None:
                frame.uc = param
            self.i = j
            return
        if word == "ansicpg" and param is not None:
            self.codepage = param
            self.i = j
            return
        if word == "outlinelev" and param is not None:
            self.outline_level_counts[str(param)] = (
                self.outline_level_counts.get(str(param), 0) + 1
            )
            # inside a style definition: style outline level;
            # body frame: current paragraph outline level
            coll_ol = self._active_collect()
            if coll_ol == "stylesheet":
                self.cur_style["outline_level"] = param
            elif coll_ol is None and not self._in_skip():
                self.para_outline_level = param
            self.i = j
            return

        # \upr marker (Unicode primary group)
        if word == "upr":
            self.upr_count += 1
            if frame is not None:
                frame.upr = True
            self.i = j
            return

        # ---- collect-domain control words ----
        coll = self._active_collect()
        if coll is not None and self._cw_collect_domain(coll, word, param, j):
            return

        # \uN: signed 16-bit + surrogate pairs + \ucN fallback skip
        if word == "u" and param is not None:
            self._cw_unicode(param, j)
            return

        # \binN: char-level skip (boundary registered)
        if word == "bin" and param is not None and param >= 0:
            self.binary_bytes_skipped += param
            if j + param > self.n:
                self._add_reason("BIN_TRUNCATED")  # D21
            self.i = min(j + param, self.n)
            return

        # ---- general registration control words ----
        if self._cw_registration(word, j, frame):
            return

        # destination dispatch
        if self._cw_destination(word, j, frame):
            return

        # \field: counted, group not skipped (fldrslt text kept)
        if word == "field":
            self.field_count += 1
            self.i = j
            return

        # body-semantics control words (swallowed in skip/collect frames)
        if not self._in_skip() and not self._active_collect():
            self._cw_body_word(word, param)
        self.i = j

    def _cw_collect_domain(
        self, coll: str, word: str, param: int | None, j: int,
    ) -> bool:
        """Collect-domain control word dispatch; True = consumed.

        Collect domains without a specific handler return False and
        fall through to the general dispatch.
        """
        handler = {
            "fonttbl": self._cw_fonttbl,
            "colortbl": self._cw_colortbl,
            "info": self._cw_info,
            "pict": self._cw_pict,
            "stylesheet": self._cw_stylesheet,
            "listtable": self._cw_listtable,
            "listlevel": self._cw_listlevel,
            "listoverridetable": self._cw_listoverridetable,
            "rsidtbl": self._cw_rsidtbl,
        }.get(coll)
        if handler is not None:
            handler(word, param, j)
            return True
        if coll == "object":
            return self._cw_object(word, param, j)
        return False

    def _cw_fonttbl(self, word: str, param: int | None, j: int) -> None:
        if word == "f" and param is not None:
            self._flush_font_entry()
            self.cur_font = {"id": param}
        elif word == "cpg" and param is not None:
            self.cur_font["codepage"] = param
        elif word == "charset" and param is not None:
            self.cur_font["charset"] = param
        elif word in _FONT_FAMILY_WORDS:
            self.cur_font["family"] = word
        self.i = j

    def _cw_colortbl(self, word: str, param: int | None, j: int) -> None:
        if word == "red" and param is not None:
            self.rgb_cur["red"] = param
        elif word == "green" and param is not None:
            self.rgb_cur["green"] = param
        elif word == "blue" and param is not None:
            self.rgb_cur["blue"] = param
        self.i = j

    def _cw_info(self, word: str, param: int | None, j: int) -> None:
        if word in _INFO_TEXT_KEYS or word in _INFO_TIME_KEYS:
            self._flush_info_entry()
            self.info_key = word
        elif word in _INFO_SCALAR_KEYS:
            # integer info scalars verbatim (source is a decimal
            # integer; same key-wins rule as _flush_info_entry)
            self._flush_info_entry()
            if param is not None and word not in self.document_info:
                self.document_info[word] = param
        elif (
            self.info_key is not None
            and self.info_key in _INFO_TIME_KEYS
        ):
            # raw control-word sequence inside a time group
            self.info_raw.append(
                f"{word}{param if param is not None else ''}"
            )
        self.i = j

    def _cw_rsidtbl(self, word: str, param: int | None, j: int) -> None:
        # \rsidN session ids verbatim (count full, samples capped)
        if word == "rsid" and param is not None:
            self.rsid_total += 1
            if len(self.rsid_samples) < _MAX_RSID_SAMPLES:
                self.rsid_samples.append(param)
        self.i = j

    def _flush_revtbl(self) -> None:
        # \*\revtbl author table close-out: semicolon-separated
        # names verbatim (count full, samples capped)
        raw = "".join(self.revtbl_buf)
        self.revtbl_buf.clear()
        for name in raw.split(";"):
            name = name.strip()
            if not name:
                continue
            self.revision_author_total += 1
            if (
                len(self.revision_author_samples)
                < _MAX_REVISION_AUTHOR_SAMPLES
            ):
                self.revision_author_samples.append(name)

    def _flush_falt(self) -> None:
        # \*\falt close-out: verbatim (trailing ";" is a separator)
        name = "".join(self.falt_buf).strip().rstrip(";").strip()
        self.falt_buf.clear()
        if name:
            self.font_alias_total += 1
            if len(self.font_alias_samples) < _MAX_FONT_ALIAS_SAMPLES:
                self.font_alias_samples.append(name)

    def _cw_pict(self, word: str, param: int | None, j: int) -> None:
        if word in _PICT_FORMAT_WORDS:
            self.pict_ctx["format"] = _PICT_FORMAT_WORDS[word]
            if word == "wmetafile" and param is not None:
                self.pict_ctx["wmetafile_type"] = param
        elif word == "picw" and param is not None:
            self.pict_ctx["picw"] = param
        elif word == "pich" and param is not None:
            self.pict_ctx["pich"] = param
        elif word == "picwgoal" and param is not None:
            self.pict_ctx["picwgoal"] = param
        elif word == "pichgoal" and param is not None:
            self.pict_ctx["pichgoal"] = param
        elif (
            word == "bin" and param is not None and param >= 0
        ):
            # binary picture data inside pict: char-level skip
            self.binary_bytes_skipped += param
            if j + param > self.n:
                self._add_reason("BIN_TRUNCATED")  # D21
            self.pict_ctx["bin_bytes"] = (
                self.pict_ctx.get("bin_bytes", 0) + param
            )
            self.i = min(j + param, self.n)
            return
        self.i = j

    def _cw_stylesheet(self, word: str, param: int | None, j: int) -> None:
        # style entries (\sN paragraph / \csN character)
        if word == "s" and param is not None:
            self._flush_style_entry()
            self.cur_style = {"id": param, "type": "paragraph"}
        elif word == "cs" and param is not None:
            self._flush_style_entry()
            self.cur_style = {"id": param, "type": "character"}
        elif word == "sbasedon" and param is not None:
            self.cur_style["basedon"] = param
        elif word == "snext" and param is not None:
            self.cur_style["next"] = param
        elif word == "styrsid" and param is not None:
            pass  # revision session id: no structural meaning
        self.i = j

    def _cw_listtable(self, word: str, param: int | None, j: int) -> None:
        # list template registration
        frame = self._current_frame()
        if word == "list":
            self._flush_list_entry()
            self.cur_list = {}
        elif word == "listtemplateid" and param is not None:
            self.cur_list["listtemplateid"] = param
        elif word == "listhybrid":
            self.cur_list["hybrid"] = True
        elif word == "listlevel":
            self._flush_level()
            self.cur_level = {
                "level": (
                    param if param is not None
                    else len(self.cur_list.get("levels", []))
                ),
            }
        elif word == "leveltext":
            # leveltext subgroup: the frame switches to the
            # leveltext collect domain
            if frame is not None:
                frame.collect = "leveltext"
        self.i = j

    def _cw_listlevel(self, word: str, param: int | None, j: int) -> None:
        # level attributes (inside listlevel, outside leveltext)
        if (
            word in (
                "levelstartat", "levelfollow", "levelspace",
                "levelindent", "levelnfc", "leveljc",
            )
            and param is not None
        ):
            self.cur_level[word] = param
        elif word == "leveltext":
            frame = self._current_frame()
            if frame is not None:
                frame.collect = "leveltext"
        self.i = j

    def _cw_listoverridetable(self, word: str, param: int | None, j: int) -> None:
        # list override registration
        if word == "listoverride":
            self._flush_override()
            self.cur_override = {}
        elif word == "listid" and param is not None:
            self.cur_override["listid"] = param
        elif word == "listoverridecount" and param is not None:
            self.cur_override["override_count"] = param
        elif word == "l" and param is not None:
            self.cur_override["l"] = param
        self.i = j

    def _cw_object(self, word: str, param: int | None, j: int) -> bool:
        r"""object-domain control words (embedded object registration).

        Nested destination words (\pict etc.) are not intercepted --
        they return False and the general dispatch re-routes them.
        Unstarred \objclass name collection: a control word opens
        the stream, the next control word or group close ends it.
        """
        # any object-domain control word closes the objclass name stream
        self.obj_class_active = False
        if word == "objclass":
            self.objdata_active = False
            self.obj_class_active = True
            self._count_destination("objclass")
            self.i = j
            return True
        if word == "objdata":
            self.objdata_active = True
            if param is not None:
                # no-space form: param chars read as the hex stream
                # head (decimal misread); registered as ambiguous
                self._add_reason("OBJDATA_PARAM_AMBIGUOUS")
            self.i = j
            return True
        if word == "objsize" and param is not None:
            self.obj_ctx["objsize"] = param
            self.i = j
            return True
        # OLE auto-behavior flags (objautlink / objupdate):
        # declared-only registration, never executed
        if word in ("objautlink", "objupdate"):
            self.obj_ctx[word] = True
            self.i = j
            return True
        self.objdata_active = False  # a control word ends the hex stream
        if word in (
            _STRUCTURE_DESTINATIONS
            | _RESOURCE_DESTINATIONS
            | _VIEW_DESTINATIONS
            | _STRUCTURE_DESTINATIONS_V2
            | _SKIP_DESTINATIONS
        ):
            return False  # destination word: fall through for re-routing
        self.i = j
        return True

    def _cw_unicode(self, param: int, j: int) -> None:
        r"""\uN: signed 16-bit + surrogate pairs + \ucN fallback skip."""
        cp = param & 0xFFFF  # negative wraps as two's complement
        self.unicode_escape_count += 1
        if self._in_skip():
            self.i = j
            return
        if self.pending_high is not None and (
            0xDC00 <= cp <= 0xDFFF
        ):
            combined = 0x10000 + (
                (self.pending_high - 0xD800) << 10
            ) + (cp - 0xDC00)
            if self.out and self.out[-1] == chr(self.pending_high):
                self.out.pop()
            self._emit(chr(combined))
            self.surrogate_pair_count += 1
            self.pending_high = None
        elif 0xD800 <= cp <= 0xDBFF:
            self.pending_high = cp
            self._emit(chr(cp))  # lone high surrogate; fixed on pairing
        else:
            if self.pending_high is not None:
                # previous high surrogate orphaned: withdraw inside upr
                if self._in_upr():
                    if self.out and self.out[-1] == chr(self.pending_high):
                        self.out.pop()
                    for f in reversed(self.frames):
                        if f.upr:
                            f.orphan_surrogate = True
                            break
                self.pending_high = None
            self._emit(chr(cp))
        frame = self._current_frame()
        if frame is not None:
            frame.unicode_skip = frame.uc
        self.i = j

    def _cw_registration(
        self, word: str, j: int, frame: _Frame | None,
    ) -> bool:
        r"""General registration control words (any frame)."""
        if word in ("sect", "sectd"):
            self.sections_total += 1
            self.i = j
            return True
        if word == "chftn":
            self.footnote_ref_count += 1
            self.i = j
            return True
        if word in ("insrs", "delrs", "ins", "del", "revised", "deleted"):
            # revision markers: route the current group's text.
            # \revised/\deleted are the standard Word forms
            # (revised text keeps the final view, deleted text
            # stays out of main).
            if frame is not None:
                frame.revision = (
                    "ins"
                    if word in ("insrs", "ins", "revised")
                    else "del"
                )
            self.i = j
            return True
        if word in ("clmgf", "clmrg"):
            self.cell_merge_count += 1
            self.i = j
            return True
        if word in ("bkmkstart", "bkmkend"):
            # bookmark names (name is the text right after the control word)
            text, n = self.text, self.n
            k = j
            while (
                k < n
                and text[k] not in " \\{}"
                and k - j < _MAX_BOOKMARK_NAME_CHARS
            ):
                k += 1
            name = text[j:k]
            self.bookmarks_total += 1
            if word == "bkmkstart" and name:
                if len(self.bookmark_names) < _MAX_BOOKMARK_NAMES:
                    self.bookmark_names.append(name)
                elif not self.bookmark_truncated:
                    self.bookmark_truncated = True
                    self._add_reason("BOOKMARKS_TRUNCATED")
            if k < n and text[k] == " ":
                k += 1  # separator space after the name
            self.i = k
            return True
        return False

    def _cw_destination(
        self, word: str, j: int, frame: _Frame | None,
    ) -> bool:
        """Destination dispatch (skip / collect / view / structure)."""
        if word in _STRUCTURE_DESTINATIONS:
            self._count_destination(word)
            if frame is not None:
                frame.skip_name = None
                frame.collect = word
                self.i = j
            else:
                self._skip_to_next_group(j)
            return True
        if word in _RESOURCE_DESTINATIONS:
            self._count_destination(word)
            if frame is not None:
                frame.skip_name = None
                frame.collect = "pict"
                self.i = j
            else:
                self._skip_to_next_group(j)
            return True
        if word in _SKIP_DESTINATIONS:
            self._count_destination(word)
            if frame is not None:
                frame.skip_name = word
                frame.collect = None
                self.i = j
            else:
                self._skip_to_next_group(j)
            return True
        # view destinations (header/footer + footnote) -> view
        # channel, not main
        if word in _VIEW_DESTINATIONS:
            self._count_destination(word)
            if frame is not None:
                frame.skip_name = None
                if word == "footnote":
                    frame.collect = "footnote"
                else:
                    frame.collect = "header_footer"
                    frame.view_kind = word
                self.i = j
            else:
                self._skip_to_next_group(j)
            return True
        # structure destinations: stylesheet/listtable/
        # listoverridetable/object -> parsed and registered
        if word in _STRUCTURE_DESTINATIONS_V2:
            self._count_destination(word)
            if frame is not None:
                frame.skip_name = None
                frame.collect = word
                self.i = j
            else:
                self._skip_to_next_group(j)
            return True
        return False

    def _skip_to_next_group(self, j: int) -> None:
        """Destination outside a group: skip to the next { or EOF."""
        nxt_brace = self.text.find("{", j)
        self.i = nxt_brace if nxt_brace >= 0 else self.n

    def _cw_body_word(self, word: str, param: int | None) -> None:
        """Body-semantics control words (caller ensures no skip/collect)."""
        if word == "par":
            self.paragraph_count += 1
            self.paragraph_offsets_total += 1
            if len(self.paragraph_offsets) < _MAX_PARAGRAPH_OFFSETS:
                self.paragraph_offsets.append(len(self.out))
            self._fmt_changed()
            self.out.append("\n")
            self._close_paragraph()
        elif word == "line":
            self.line_break_count += 1
            self._fmt_changed()
            self.out.append("\n")
        elif word == "tab":
            self.tab_count += 1
            self.out.append("\t")
            if self.run_open is not None:
                self.run_open["end"] = len(self.out)
        elif word == "page":
            self.page_break_count += 1
        elif word == "trowd":
            # row start
            self._flush_row()
            self.in_table_row = True
            self.row_cellx = []
            self.row_cells = []
            self.cell_start = len(self.out)
            self.table_row_count += 1
        elif word == "cellx" and param is not None:
            if self.in_table_row:
                self.row_cellx.append(param)
        elif word == "cell":
            # cell boundary (tab; text preserved)
            if self.in_table_row:
                self._flush_cell()
            self.out.append("\t")
            self.cell_start = len(self.out)
            if self.run_open is not None:
                self.run_open["end"] = len(self.out)
        elif word == "row":
            self._flush_row()
        elif word == "s" and param is not None:
            # paragraph style reference (heading detection)
            self.para_style = param
        elif word in _WORD_OUTPUT:
            self._emit(_WORD_OUTPUT[word])
        elif word in _RUN_FORMAT_WORDS:
            # format toggle (no param = on, 0 = off)
            on = True if param is None else (param != 0)
            key = _RUN_FORMAT_WORDS[word]
            if self.fmt_state.get(key, False) != on:
                self.fmt_state[key] = on
                self._fmt_changed()
        elif word == "fs" and param is not None:
            if self.fmt_state.get("font_size") != param:
                self.fmt_state["font_size"] = param
                self._fmt_changed()
        elif word == "cf" and param is not None:
            if self.fmt_state.get("color_idx") != param:
                self.fmt_state["color_idx"] = param
                self._fmt_changed()
        elif word == "protected":
            # \protected run flag: counted as declared (no param =
            # on, \protected0 = off); body frames only, same scope
            # as run tracking.
            self.protected_word_count += 1

    # ---- plain character branch ----

    def _handle_char(self, ch: str) -> None:
        # \uN fallback items are swallowed first (frame.unicode_skip)
        frame = self._current_frame()
        if frame is not None and frame.unicode_skip > 0:
            frame.unicode_skip -= 1
            self.i += 1
            return

        # collect separators
        coll = self._active_collect()
        if ch == ";" and coll == "fonttbl":
            self._flush_font_entry()
            self.i += 1
            return
        if ch == ";" and coll == "colortbl":
            self._flush_color_entry()
            self.i += 1
            return

        # object-domain objdata hex stream split (non-hex char ends
        # the stream; group boundary = stream boundary)
        if coll in ("object", "objdata") and self.objdata_active:
            if ch.isspace():
                self.i += 1
                return
            if ch in "0123456789abcdefABCDEF":
                if len(self.objdata_hex_buf) < _MAX_OBJDATA_HEX_CHARS:
                    self.objdata_hex_buf.append(ch)
                else:
                    self.objdata_overflow = True
                self.i += 1
                return
            self.objdata_active = False  # hex stream ended
            self.i += 1
            return

        if self._in_fldinst():
            # instruction text: observed only, never emitted
            self.fldinst_buf.append(ch)
            self.i += 1
            return

        if self._in_skip():
            self.i += 1
            return
        self._emit(ch)
        self.i += 1

    # ---- result assembly ----

    def _finish(self) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
        if self.frames:
            self.unbalanced = True  # unclosed groups at EOF

        # ---- group-level recovery evidence ----
        unclosed_at_eof = len(self.frames)
        group_recovery: Dict[str, int] = {
            "unclosed_at_eof": unclosed_at_eof,
            "early_closes": self.early_closes,
        }
        if unclosed_at_eof or self.early_closes:
            self._add_reason("GROUP_RECOVERY_APPLIED")
        if self.unbalanced:
            self._add_reason("UNBALANCED_GROUPS")

        self._flush_residual()

        extracted = "".join(self.out).rstrip(_TRAILING_NL)

        # tri-state resolution
        if self.unknown_destinations:
            self._add_reason("UNKNOWN_RTF_DESTINATION")
        resolution_status = "partial" if self.reason_codes else "complete"
        # ---- provenance clues (never decide parsing) ----
        generator_value = "".join(self.generator_text).strip() or None
        has_generator = generator_value is not None
        has_info = bool(self.document_info)
        if has_generator and has_info:
            source_hint = "metadata_or_generator"
        elif has_generator:
            source_hint = "generator"
        elif has_info:
            source_hint = "metadata"
        else:
            source_hint = None
        # conversion_artifact: declared \*\generator evidence only;
        # True when present, None otherwise (never asserted False).
        conversion_artifact: Optional[bool] = (
            True if has_generator else None
        )

        coverage = self._build_coverage()
        self._build_heading_candidates()
        views = self._build_views(extracted)
        structure = self._build_structure(
            group_recovery=group_recovery,
            resolution_status=resolution_status,
            generator_value=generator_value,
            source_hint=source_hint,
            conversion_artifact=conversion_artifact,
            coverage=coverage,
            views=views,
        )
        # \protected flag: key written only when declared
        if self.protected_word_count:
            structure["protected_word_count"] = (
                self.protected_word_count
            )
        return extracted, structure, self.pict_records

    def _flush_residual(self) -> None:
        """Residual collection flush (truncation guard)."""
        coll = self._active_collect()
        if coll == "fonttbl":
            self._flush_font_entry()
        elif coll == "colortbl":
            self._flush_color_entry()
        elif coll == "pict":
            self._flush_pict()
        elif coll == "stylesheet":
            self._flush_style_entry()
        elif coll in (
            "listtable", "listlevel", "leveltext",
        ):
            if coll == "leveltext" and self.cur_level:
                self.cur_level["leveltext"] = "".join(self.leveltext_buf)
                self.leveltext_buf.clear()
            self._flush_list_entry()
        elif coll == "listoverridetable":
            self._flush_override()
        elif coll == "object":
            self._flush_object()
        if self._in_fldinst():
            self._flush_field_instruction()
        self._flush_row()
        self._close_paragraph()
        self._close_run()

    def _build_coverage(self) -> Dict[str, str]:
        r"""coverage six independent tri-states.

        Judged only from source-provable facts; a file declaring no
        content of a kind is vacuously complete. embedded_objects is
        partial once \object is declared: full OLE compound parsing
        is out of scope.
        """
        return {
            "text": (
                "partial"
                if self.unbalanced or "INVALID_HEX_ESCAPE" in self.reason_codes
                else "complete"
            ),
            "paragraphs": (
                "partial"
                if self.paragraph_offsets_total > len(self.paragraph_offsets)
                else "complete"
            ),
            "character_format": (
                "partial" if self.run_stats["truncated"] else "complete"
            ),
            "tables": (
                "partial" if self.table_rows_truncated else "complete"
            ),
            "images": (
                "partial"
                if any(
                    rec.get("extracted") is not True for rec in self.pict_records
                )
                or "TRUNCATED_PICT_DATA" in self.reason_codes
                else "complete"
            ),
            "embedded_objects": (
                # declared \object: objdata hex decoded and registered,
                # but full CFB structure parsing is out of scope
                # -> partial, not complete
                "partial" if self.destination_counts.get("object") else "complete"
            ),
        }

    def _build_heading_candidates(self) -> None:
        """Heading detection (after the style table is complete)."""
        heading_style_depth: Dict[int, int] = {}
        for entry in self.style_entries:
            entry_depth = _heading_depth_of(entry)
            if entry_depth is not None:
                sid = entry.get("id")
                if isinstance(sid, int):
                    heading_style_depth[sid] = entry_depth
        for para in self.paragraphs_head:
            para_depth: int | None = None
            para_pattern: str | None = None
            if (
                para["style"] is not None
                and para["style"] in heading_style_depth
            ):
                para_depth = heading_style_depth[para["style"]]
                para_pattern = "rtf_style"
            elif (
                para["outline_level"] is not None
                and para["outline_level"] < 9
            ):
                para_depth = para["outline_level"] + 1
                para_pattern = "rtf_outlinelev"
            if para_depth is not None and para["text"].strip():
                assert para_pattern is not None  # set with para_depth
                if len(self.heading_candidates) < _MAX_HEADING_CANDIDATES:
                    self.heading_candidates.append(HeadingCandidate(
                        raw=para["text"][:_MAX_PARAGRAPH_TEXT_CHARS],
                        pattern=para_pattern,
                        depth_hint=para_depth,
                        start=para["start"],
                        end=para["end"],
                        provenance="declared",
                    ))
                elif not self.heading_truncated:
                    self.heading_truncated = True
                    self._add_reason("HEADING_CANDIDATES_TRUNCATED")

    def _build_views(self, extracted: str) -> Dict[str, Any]:
        """Six view channels (default output is still main only)."""
        hidden_text_value = "".join(self.hidden_text_buf)
        return {
            "main_view": {
                "paragraphs": self.paragraph_count,
                "chars": len(extracted),
            },
            "header_footer_view": self.header_footer_view,
            "annotation_view": self.annotation_view,
            "revision_view": {
                "inserted": self.revision_inserted,
                "deleted": self.revision_deleted,
                "inserted_groups": self.rev_ins_total,
                "deleted_groups": self.rev_del_total,
            },
            "hidden_text_view": hidden_text_value[:_MAX_VIEW_TEXT_CHARS],
            "metadata_view": {
                "document_info_keys": sorted(self.document_info.keys()),
                "fonts": len(self.font_entries),
                "colors": len(
                    [c for c in self.color_entries if c is not None]
                ),
                "styles": len(self.style_entries),
                "lists": len(self.list_table),
            },
        }

    def _build_structure(
        self,
        *,
        group_recovery: Dict[str, int],
        resolution_status: str,
        generator_value: str | None,
        source_hint: str | None,
        conversion_artifact: Optional[bool],
        coverage: Dict[str, str],
        views: Dict[str, Any],
    ) -> Dict[str, Any]:
        """rtf_structure_v2 evidence dict assembly (key order frozen).

        Additive keys (revision_author_*/rsid_*/font_alias_*) are
        written only when payload exists.
        """
        structure = {
            "extractor": "rtf_structure_v2",
            # ---- v1 evidence keys (frozen) ----
            "control_word_count": self.control_word_count,
            "paragraph_count": self.paragraph_count,
            "line_break_count": self.line_break_count,
            "tab_count": self.tab_count,
            "page_break_count": self.page_break_count,
            "table_row_count": self.table_row_count,
            "group_depth_max": self.depth_max,
            "unbalanced_groups": self.unbalanced,
            "unicode_escape_count": self.unicode_escape_count,
            "surrogate_pair_count": self.surrogate_pair_count,
            "hex_escape_count": self.hex_escape_count,
            "binary_bytes_skipped": self.binary_bytes_skipped,
            "codepage": self.codepage,
            "field_count": self.field_count,
            "hyperlink_count": self.hyperlink_count,
            "destination_counts": self.destination_counts,
            "outline_level_counts": self.outline_level_counts,
            # ---- v2 evidence keys ----
            "font_table": self.font_entries,
            "color_table": self.color_entries,
            "document_info": self.document_info,
            "generator": generator_value,
            "source_hint": source_hint,
            "conversion_artifact": conversion_artifact,
            "page_break_declared": self.page_break_count,
            "coverage": coverage,
            "evidence_type": "native_rtf",
            "upr_count": self.upr_count,
            "ud_fallback_used": self.ud_fallback_used,
            "unknown_destinations": self.unknown_destinations,
            "table_rows": self.table_rows,
            "table_rows_total": self.table_rows_total,
            "table_rows_truncated": self.table_rows_truncated,
            "runs_total": self.run_stats["total"],
            "runs_head": self.runs_head,
            "runs_truncated": self.run_stats["truncated"],
            "paragraph_offsets": self.paragraph_offsets,
            "paragraph_offsets_total": self.paragraph_offsets_total,
            "resolution_status": resolution_status,
            "reason_codes": self.reason_codes,
            # ---- P1/P2 evidence keys ----
            "style_table": self.style_entries,
            "style_table_truncated": self.style_truncated,
            "list_table": self.list_table,
            "list_override_table": self.list_override_table,
            "sections_total": self.sections_total,
            "cell_merge_count": self.cell_merge_count,
            "footnotes": self.footnotes,
            "footnotes_total": self.footnotes_total,
            "footnote_ref_count": self.footnote_ref_count,
            "fields": self.fields,
            "fields_total": self.fields_total,
            "bookmark_names": self.bookmark_names,
            "bookmarks_total": self.bookmarks_total,
            "embedded_objects": self.embedded_objects,
            "embedded_objects_total": self.embedded_objects_total,
            "group_recovery": group_recovery,
            "views": views,
            # heading_candidates carries HeadingCandidate objects
            # (consumed via .depth_hint etc., same as the
            # txt/odp/docx parsers).
            "heading_candidates": list(self.heading_candidates),
            "note": (
                "evidence-only registration: P1/P2 (prereg v2) upgraded "
                "stylesheet/listtable/listoverridetable/object and "
                "header/footer/footnote from registry-only counting to "
                "structured registration (views for header/footer/"
                "annotation/revision/hidden text; objdata hex decoded "
                "with magic + nested-RTF recursion; never executed); "
                "structure destinations (fonttbl/colortbl/info) parsed "
                "v2; pict hex decoded to byte assets (same schema as "
                "docx_image_assets); \\bin skips char-level after "
                "decoding (byte-accurate recovery is out of scope: the "
                "text decode channel destroys binary bytes; honest "
                "reason registered, no fabricated bytes); heading "
                "candidates are style/outlinelev-declared only (no "
                "guessing); runs tracked body-only; table rows capped "
                "at registration, totals always counted; coverage is "
                "six independent tri-states (no single aggregate), "
                "vacuous-complete when the file declares no content of "
                "that kind; embedded_objects is partial (not complete) "
                "when \\object is declared: CFB structure parsing is out "
                "of scope; page_break_declared counts only source-"
                "provable \\page words (no page-number inference); "
                "source_hint/conversion_artifact are provenance clues "
                "only and never decide parsing; deleted revision text "
                "stays out of main view (preserved in revision_view); "
                "evidence_type vocabulary reserves rendered_conversion "
                "(unused in this chain); r25: standard-form "
                "\\revised/\\deleted route like ins/del (deleted text "
                "stays out of main, registered in revision_view); "
                "\\*\\revtbl author names, \\*\\rsidtbl session ids, "
                "\\*\\falt font aliases and \\info scalars "
                "(\\edmins/\\nofpages/\\nofwords/\\nofchars) registered "
                "verbatim (counts full truth, samples capped; keys "
                "written only when payload present)"
            ),
        }
        # additive keys (written only when non-empty)
        if self.revision_author_total:
            structure["revision_author_count"] = self.revision_author_total
            structure["revision_author_samples"] = (
                self.revision_author_samples
            )
        if self.rsid_total:
            structure["rsid_count"] = self.rsid_total
            structure["rsid_samples"] = self.rsid_samples
        if self.font_alias_total:
            structure["font_alias_count"] = self.font_alias_total
            structure["font_alias_samples"] = self.font_alias_samples
        return structure
