# ============================================================
# Narrative Domain
# PPT/CFB (.ppt/.pps/.pot) Parser — legacy PowerPoint binary extraction
# ============================================================
# Fact boundaries (PowerPoint-family content format; like PPTX this is
# not source-evidence):
#   - Document.text = inline body text of SlideListWithText
#     (instance 0) ([MS-PPT] 2.4.14.3/2.4.14.5: SlidePersistAtom
#     persistIdRef group order is slide order; in-group
#     TextCharsAtom/TextBytesAtom runs concatenate);
#   - metadata["ppt_cfb_structure"] = structural evidence registry
#     (written once olefile opens successfully; counters advance
#     only with facts actually reached);
#   - text is located purely through [MS-PPT] structures (Current
#     User stream → UserEditAtom chain backwalk →
#     PersistPtrIncrementalBlock persist directory →
#     DocumentContainer → SLWT instance 0); text is never guessed
#     out of the raw binary;
#   - TextBytesAtom code page: the format declares no body code
#     page, so 8-bit runs decode with the cp1252 ansi default +
#     errors=replace, and high_byte_count discloses the ambiguity;
#     no code-page guessing (CJK decks carry TextCharsAtom UTF-16);
#   - heading_candidates = [] (master style tables are not parsed
#     and titles are not guessed);
#   - SlideContainer Escher text recovery (gated to slides whose
#     SLWT0 group holds zero text, preventing duplication) and
#     notes body (SLWT2 reference + notesID pairing) are delivered;
#     ExObjList hyperlinks register as object-level facts (hyperlink
#     key family + bounded hyperlinks list, mirroring the pptx
#     _hyperlinks contract — object facts never enter the prose;
#     ExEmbed counted; internal-jump links resolve no target and
#     invent no values); Escher shape-level location (SpContainer/
#     Sp/OEPlaceholderAtom facts, shape key family + bounded shapes
#     list, group/patriarch excluded, mirroring the pptx
#     pptx_slide_objects contract); encrypted/corrupt files land
#     in the structural failure tiers (the CU/UEA/record walk
#     fails naturally) — no decryption, no repair.
#
# Dependencies: olefile (CFB container IO); the record walk is a
# fully self-contained spec state machine with no external
# extractors.
import hashlib
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import olefile

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts as _build_carrier_facts,
)
from provendoc.core.diagnostics import (
    detect_security_carriers as _detect_security_carriers,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.parsers.narrative.docx_security import classify_container
from provendoc.core.text.normalization import (
    sanitize_invisible_chars as _sanitize_invisible_chars,
)

# [MS-PPT] record constants
_REC_CURRENT_USER = 0x0FF6          # the Current User stream is itself a record
_CU_TOKEN_PPT97 = 0xE391C05F        # headerToken: PowerPoint 97+ family
_REC_USER_EDIT = 0x0FF5             # UserEditAtom (chain backwalk)
_REC_PERSIST_PTR = 0x1772           # PersistPtrIncrementalBlock
_REC_DOCUMENT = 0x03E8              # DocumentContainer
_REC_SLIDE = 0x03EE                 # SlideContainer
_REC_NOTES = 0x03F0                 # NotesContainer
_REC_MAIN_MASTER = 0x03F8          # MainMasterContainer
_REC_SLIDE_PERSIST = 0x03F3        # SlidePersistAtom (payload[0:4]=persistIdRef)
_REC_SLIDE_LIST_WITH_TEXT = 0x0FF0  # SlideListWithText (instance 0 = slides)
_REC_TEXT_HEADER = 0x0F9F           # TextHeaderAtom (text run boundary)
_REC_TEXT_CHARS = 0x0FA0            # TextCharsAtom (UTF-16LE)
_REC_TEXT_BYTES = 0x0FA8            # TextBytesAtom (8-bit ANSI)
_REC_SLIDE_ATOM = 0x03EF            # SlideAtom (@12 masterID, @16 notesID)

# Hyperlink channel constants (recType values verified against POI)
_REC_EX_OBJ_LIST = 0x0409           # ExObjList (registry container inside Document)
_REC_EX_HYPERLINK = 0x0FD7          # ExHyperlink container (Atom + CString×N)
_REC_EX_HYPERLINK_ATOM = 0x0FD3     # ExHyperlinkAtom (id@payload+0)
_REC_CSTRING = 0x0FBA              # CString (payload is raw UTF-16LE, no prefix)
_REC_INTERACTIVE_INFO = 0x0FF2      # InteractiveInfo container (text anchor)
_REC_INTERACTIVE_INFO_ATOM = 0x0FF3  # InteractiveInfoAtom (id@4/action@8/type@12)
_REC_TX_INTERACTIVE = 0x0FDF       # TxInteractiveInfoAtom (start@0/end@4)
_REC_EX_EMBED = 0x0FCC              # ExEmbed (embedded OLE object container)
_HL_ACTION_HYPERLINK = 4            # InteractiveInfoAtom action = HYPERLINK
_HL_LINK_URL_TYPES = frozenset({8, 9, 10})  # url/otherppt/otherfile

# Shape-level location constants (ClientData/ClientTextbox carry
# ver=0xF and are containers, not atoms — misreading them as atoms
# would falsely zero the placeholder/text counts)
_REC_PP_DRAWING = 0x040C           # PPDrawing (Escher host inside SLD, direct child)
_ESCHER_SP_CONTAINER = 0xF004      # SpContainer (ver=0xF container)
_ESCHER_SP = 0xF00A                # Sp ([spid@0][flags@4], len 8)
_ESCHER_CLIENT_DATA = 0xF011       # ClientData (container, holds PPT records)
_ESCHER_CLIENT_TEXTBOX = 0xF00D    # ClientTextbox (container, holds text atoms)
_REC_OE_PLACEHOLDER = 3011         # OEPlaceholderAtom (0x0BC3)
_SP_FLAG_GROUP = 0x0001            # Sp flags: GROUP
_SP_FLAG_PATRIARCH = 0x0004        # Sp flags: PATRIARCH
_SHAPES_MAX_RECORDS = 64          # shapes list cap (bounds output size)

# placeholderId → semantic slide names; values outside the table
# stay numeric — never guessed, never invented
_PLACEHOLDER_NAMES = {
    0: "none", 7: "datetime", 8: "slide_number", 9: "footer",
    10: "header", 11: "slide_image", 13: "title", 14: "body",
    15: "centered_title", 16: "subtitle", 17: "vertical_title",
    18: "vertical_body", 19: "content", 20: "chart", 21: "table",
    22: "clip_art", 23: "diagram", 24: "media",
    25: "vertical_object", 26: "picture",
}

# shapeType conservative vocabulary (Sp instance = shapeType; only
# types with unique semantic anchors are named, others stay numeric)
_SHAPE_TYPE_NAMES = {202: "text_box"}

# Escher BLIP record types (top-level picture media in the
# Pictures stream)
_BLIP_TYPES = frozenset({
    0xF01A,  # METAFILE EMF
    0xF01B,  # METAFILE WMF
    0xF01C,  # METAFILE PICT
    0xF01D,  # JPEG
    0xF01E,  # PNG
    0xF01F,  # TIFF
    0xF029,  # DIB / BITMAP
})

_UEA_CHAIN_MAX = 512      # UserEditAtom chain depth cap (cycle guard)
_STREAM_MAX_RECORDS = 1_000_000  # top-level record walk count cap
_RECORD_WALK_MAX_DEPTH = 64  # Slide/Notes container recursion depth cap
_HYPERLINK_MAX_RECORDS = 64  # hyperlinks list cap (bounds output size)
_CSTRING_MAX_TEXT = 2048      # CString text cap (corrupt-stream guard)


class _PptCfbError(Exception):
    """Structural failure: carries the stage name and the facts
    reached so far (degradation matrix)."""

    def __init__(self, stage: str, message: str, facts: Dict[str, Any]):
        super().__init__(message)
        self.stage = stage
        self.facts = facts


class PptCfbParser(BaseParser):
    """Legacy PowerPoint binary (OLE/CFB) parser.

    Full [MS-PPT] record walk: the Current User stream (0x0FF6)
    yields offsetToCurrentEdit → the UserEditAtom (0x0FF5) chain
    backwalk collects PersistPtrIncrementalBlock (0x1772) entries
    → persist directory {pid: offset} → docPersistIdRef locates
    the DocumentContainer (0x03E8) → in SlideListWithText
    (0x0FF0) instance 0, group order is slide order, and in-group
    TextCharsAtom (0x0FA0, UTF-16LE) / TextBytesAtom (0x0FA8,
    cp1252) runs become the body text.  When a slide's SLWT0
    group holds zero text, Escher text inside the SlideContainer
    is recovered (gated to prevent duplication); notes pair via
    SLWT2 references + SlideAtom.notesID and append as "Notes: "
    lines after each page's body (mirrors the pptx/odp family
    contracts).  ExObjList hyperlinks register as object-level
    facts (hyperlink key family + bounded hyperlinks list,
    mirroring the pptx _hyperlinks contract — object facts never
    enter the prose; internal-jump links resolve no target and
    invent no values).  Shape-level location walks PPDrawing
    Escher SpContainers into a shape key family + bounded shapes
    list (shape_id/shape_type/has_text/placeholder; entries carry
    the composite slide_index because spid is per-slide;
    group/patriarch excluded, mirroring the pptx
    pptx_slide_objects contract).  Non-CFB files, a missing
    PowerPoint Document stream, PPT95 tokens, a broken chain, or
    empty text each land in an honest-failure tier (structural
    facts reached so far are still registered).
    """

    def __init__(self, enable_image_assets=True, limits=None):
        """PowerPoint-family kwarg compatibility (universal_parser
        family dispatch).

        The record walk has no image-asset or resource-guard
        channel, so both flags are accepted and ignored (the
        body-text path performs neither of those actions).
        """
        super().__init__()
        self._enable_image_assets = bool(enable_image_assets)
        self._limits = limits

    def parse(self, file_path) -> Document:
        """Parse .ppt/.pps/.pot into a Document (page-ordered body
        text + structural evidence)."""
        metadata = _build_metadata(file_path)
        suffix = Path(file_path).suffix.lstrip(".").lower()
        metadata["file_type"] = suffix if suffix in ("ppt", "pps", "pot") \
            else "ppt"
        start_time = time.perf_counter()

        try:
            raw = Path(file_path).read_bytes()
            metadata["sha256"] = hashlib.sha256(raw).hexdigest()

            # 1. CFB header check: a non-OLE/CFB container fails
            #    honestly — gettext .pot text (suffix ambiguity; the
            #    content language is not guessed) and truncated
            #    files; no stream-level keys are written for facts
            #    not reached.
            container_facts = classify_container(raw)
            if not container_facts["ole_container"]:
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "not an OLE/CFB container (legacy PowerPoint "
                    "binary expected; format mismatch honest failure)"
                )
                metadata["parse_duration_ms"] = round(
                    (time.perf_counter() - start_time) * 1000, 3,
                )
                return Document(text="", metadata=metadata)

            # 2. olefile open
            try:
                ole = olefile.OleFileIO(str(file_path))
            except Exception as e:
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    f"olefile open failed: {type(e).__name__} "
                    f"(corrupt/truncated CFB honest failure)"
                )
                metadata["parse_duration_ms"] = round(
                    (time.perf_counter() - start_time) * 1000, 3,
                )
                return Document(text="", metadata=metadata)

            with ole:
                text, structure, failure = _parse_cfb_powerpoint(ole)

            if structure:
                metadata["ppt_cfb_structure"] = structure
                metadata["heading_candidates"] = []  # styles are not parsed; no guessing

            if failure is not None:
                metadata["parse_failed"] = True
                metadata["parse_error"] = failure
                text = ""

            # Security carrier census (same mounting as the pptx
            # contract): ppt pulls body text from CFB binary
            # streams instead of the base _read_and_diagnose path,
            # so the census mounts on the extracted body-text view;
            # failure tiers have text=="" so the census is naturally
            # empty (no body text, no census surface, no keys
            # written).  Non-zero structured counts are written to
            # metadata["security_carriers"], and fact codes are
            # appended to warnings.
            _carrier_census = _detect_security_carriers(text)
            if _carrier_census:
                metadata["security_carriers"] = _carrier_census
                metadata.setdefault("warnings", []).extend(
                    _build_carrier_facts(_carrier_census)
                )

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            # Working-text sanitization (pptx contract): only
            # Document.text is normalized (ppt is a binary
            # container, so the source_text contract is None);
            # structural facts were already built on the raw view
            # above, and the sanitization is disclosed honestly via
            # metadata["sanitization"] + warning.
            sanitized_text, _sanitization = _sanitize_invisible_chars(text)
            if _sanitization:
                metadata["sanitization"] = _sanitization
                _normalized = sum(
                    _sanitization.get(key, 0)
                    for key in (
                        "zero_width_space", "bidi_resolved",
                        "bidi_unresolved", "zero_width", "mid_file_bom",
                        "ansi_escape", "control_char", "form_feed",
                    )
                )
                if _normalized:
                    metadata.setdefault("warnings", []).append(
                        f"{_normalized} invisible characters normalized "
                        "(see metadata.sanitization)"
                    )
            return Document(text=sanitized_text, metadata=metadata)
        except Exception as e:  # never crash
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            return Document(text="", metadata=metadata)


# ------------------------------------------------------------
# Structural parsing (module-level pure functions: no instance
# state, directly testable)
# ------------------------------------------------------------

def _read_record_header(buf: bytes, off: int) -> Tuple[int, int, int]:
    """Read the 8-byte record header: returns (ver_inst, rec_type, rec_len).

    Out-of-range access (off < 0 or off+8 > len) raises IndexError —
    the caller maps it to the failure tier of the current step.
    """
    if off < 0 or off + 8 > len(buf):
        raise IndexError(f"record header out of range at {off}")
    ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", buf, off)
    return ver_inst, rec_type, rec_len


def _stream_records(buf: bytes) -> List[Tuple[int, int, int]]:
    """Top-level record walk: returns [(ver_inst, rec_type, off)].

    The _STREAM_MAX_RECORDS count cap guards against pathological
    streams; a record crossing the end stops the walk (tail
    truncation returns the facts reached, nothing is guessed).
    Feeds container counting and picture registration.
    """
    out: List[Tuple[int, int, int]] = []
    off = 0
    total = len(buf)
    while off + 8 <= total and len(out) < _STREAM_MAX_RECORDS:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", buf, off)
        end = off + 8 + rec_len
        if end > total:
            break  # tail truncation: complete records only, no partial guesses
        out.append((ver_inst, rec_type, off))
        off = end
    return out


def _merge_persist_directory(
    doc: bytes, uea_offsets: List[int],
) -> Dict[int, int]:
    """Merge the UserEditAtom chain (new→old) into the persist
    directory {pid: offset}.

    Each UEA points at one PPIB (0x1772); a PPIB entry is 4 bytes
    = low 20 bits persist id + high 12 bits cPersist, where
    cPersist is the number of consecutive offsets the entry
    carries (no +1).  The same pid prefers the newest edit
    (setdefault semantics; the chain is already new→old).
    Structural corruption raises IndexError/KeyError for the
    caller to degrade.
    """
    directory: Dict[int, int] = {}
    for uea_off in uea_offsets:
        _, rec_type, _ = _read_record_header(doc, uea_off)
        if rec_type != _REC_USER_EDIT:
            continue
        ppib_off = struct.unpack_from("<I", doc, uea_off + 8 + 12)[0]
        _, ppib_type, ppib_len = _read_record_header(doc, ppib_off)
        if ppib_type != _REC_PERSIST_PTR:
            continue
        i = ppib_off + 8
        end = ppib_off + 8 + ppib_len
        while i + 4 <= end:
            entry = struct.unpack_from("<I", doc, i)[0]
            pid = entry & 0xFFFFF
            count = entry >> 20
            i += 4
            if pid == 0 or count == 0:
                continue  # pid 0 reserved / empty entry: skip without consuming offsets
            if i + 4 * count > end:
                break  # out of range: stop on a corrupt PPIB
            for k in range(count):
                offset = struct.unpack_from("<I", doc, i + 4 * k)[0]
                # pid is the start of a consecutive series: the
                # entry's k-th offset belongs to pid+k (flat
                # semantics would collapse pids 2..N all onto the
                # first offset)
                directory.setdefault(pid + k, offset)
            i += 4 * count
    return directory


def _uea_chain(doc: bytes, first_offset: int) -> List[int]:
    """Backwalk the UserEditAtom chain: returns offsets (new→old).

    offsetLastEdit=0 marks the chain tail; the _UEA_CHAIN_MAX depth
    cap guards against self-referential loops (common in
    encrypted/corrupt files).  Structural corruption raises
    IndexError.
    """
    offsets: List[int] = []
    off = first_offset
    while off and len(offsets) < _UEA_CHAIN_MAX:
        _, rec_type, _ = _read_record_header(doc, off)
        if rec_type != _REC_USER_EDIT:
            raise IndexError(
                f"no UserEditAtom record at offset {off}"
            )
        offsets.append(off)
        off = struct.unpack_from("<I", doc, off + 8 + 8)[0]
    return offsets


def _extract_slides(
    slwt: bytes, slwt_start: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Walk the SLWT (instance 0) groups: returns (slide groups,
    text-run stats).

    Each SlidePersistAtom (0x03F3) opens a group
    (persistIdRef@payload+0 per [MS-PPT] 2.4.14.5;
    slideIdentifier@payload+12 is the SLWT2 notes pairing key);
    group order is slide order.  Inside a group, a TextHeaderAtom
    opens a text run and TextCharsAtom (0x0FA0) / TextBytesAtom
    (0x0FA8) carry the text; InteractiveInfo (0x0FF2) container
    anchors plus the immediately following sibling
    TxInteractiveInfoAtom (0x0FDF) range are collected into
    group["interactive"]; other records (Style/SpecInfo etc.) are
    skipped without consuming.  Returns slide groups
    [{persist_id_ref, runs: [raw-str]}] and run-level stats
    [{atom, chars}].  Raw text is not sanitized here (the
    sanitization state machine is the single choke point).
    """
    slides: List[Dict[str, Any]] = []
    atom_stats: List[Dict[str, Any]] = []
    ver_inst, rec_type, rec_len = _read_record_header(slwt, slwt_start)
    assert rec_type == _REC_SLIDE_LIST_WITH_TEXT  # caller has verified
    off = slwt_start + 8
    end = slwt_start + 8 + rec_len
    current: Optional[Dict[str, Any]] = None
    pending_interactive: Optional[Dict[str, Any]] = None
    while off + 8 <= end:
        ver_inst, child_type, child_len = _read_record_header(slwt, off)
        child_end = off + 8 + child_len
        if child_end > end:
            break  # tail truncation: complete records only
        if (
            pending_interactive is not None
            and child_type != _REC_TX_INTERACTIVE
            and current is not None
        ):
            current["interactive"].append(pending_interactive)
            pending_interactive = None
        payload = slwt[off + 8: child_end]
        if child_type == _REC_SLIDE_PERSIST:
            if current is not None:
                slides.append(current)
            current = {
                "persist_id_ref": struct.unpack_from(
                    "<I", payload, 0,
                )[0] if len(payload) >= 4 else None,
                "slide_identifier": struct.unpack_from(
                    "<I", payload, 12,
                )[0] if len(payload) >= 16 else None,
                "runs": [],
                "interactive": [],
            }
        elif current is not None:
            if child_type == _REC_TEXT_CHARS:
                current["runs"].append(
                    payload.decode("utf-16-le", errors="replace"),
                )
                atom_stats.append(
                    {"atom": "chars", "chars": len(payload) // 2},
                )
            elif child_type == _REC_TEXT_BYTES:
                current["runs"].append(
                    payload.decode("cp1252", errors="replace"),
                )
                atom_stats.append(
                    {"atom": "bytes", "chars": len(payload)},
                )
            elif (
                child_type == _REC_INTERACTIVE_INFO
                and (ver_inst & 0xF) == 0xF
            ):
                info = _parse_interactive_info(slwt, off, child_end)
                if info is not None:
                    pending_interactive = info
            elif (
                child_type == _REC_TX_INTERACTIVE
                and child_len >= 8
                and pending_interactive is not None
            ):
                pending_interactive["text_start"] = struct.unpack_from(
                    "<I", payload, 0,
                )[0]
                pending_interactive["text_end"] = struct.unpack_from(
                    "<I", payload, 4,
                )[0]
                current["interactive"].append(pending_interactive)
                pending_interactive = None
            # TextHeaderAtom/Style/SpecInfo etc.: run boundaries and
            # styling; they do not change concatenation semantics
            # (runs join with \n) and are not consumed.
        off = child_end
    if pending_interactive is not None and current is not None:
        current["interactive"].append(pending_interactive)
    if current is not None:
        slides.append(current)
    return slides, atom_stats


def _sanitize_ppt_text(text: str) -> Tuple[str, Dict[str, int]]:
    """PPT text sanitization state machine (same family semantics
    as the docx _sanitize_doc_text).

    0x0D paragraph marks / 0x0B soft line breaks become newlines;
    tabs are kept; other 0x00-0x1F control characters are dropped
    (placeholder/reference text is not rendered).
    """
    counts = {
        "paragraph_count": 0,
        "soft_break_count": 0,
        "tab_count": 0,
    }
    out: List[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x0D:
            counts["paragraph_count"] += 1
            out.append("\n")
        elif code == 0x0B:
            counts["soft_break_count"] += 1
            out.append("\n")
        elif code == 0x09:
            counts["tab_count"] += 1
            out.append("\t")
        elif code < 0x20:
            continue
        else:
            out.append(ch)
    return "".join(out), counts


def _find_slwt(
    doc: bytes, doc_offset: int, doc_end: int, instance: int,
) -> Optional[int]:
    """Find the SLWT (0x0FF0) offset of the given instance inside
    the DocumentContainer.

    instance = the high 12 bits of ver_inst (0 = slide list,
    2 = notes list).  Returns None when absent (a missing
    instance 2 means no notes — a normal state, not a failure).
    """
    off = doc_offset + 8
    while off + 8 <= doc_end:
        ver_inst, child_type, child_len = struct.unpack_from(
            "<HHI", doc, off,
        )
        child_end = off + 8 + child_len
        if child_end > doc_end:
            break
        if (
            child_type == _REC_SLIDE_LIST_WITH_TEXT
            and (ver_inst >> 4) == instance
        ):
            return off
        off = child_end
    return None


def _looks_like_url(text: str) -> bool:
    """URL-shape heuristic (for slotting a lone CString).

    scheme/www. prefixes or email shapes count as URLs; other
    shapes fall into the title slot.
    """
    lowered = text.lower()
    if lowered.startswith((
        "http://", "https://", "ftp://", "mailto:", "www.",
        "file://", "news://", "gopher://",
    )):
        return True
    return (
        "@" in text and "." in text and " " not in text
        and len(text) <= 254
    )


def _find_ex_obj_list(
    doc: bytes, doc_offset: int, doc_end: int,
) -> Optional[int]:
    """Find the ExObjList (0x0409) offset inside the DocumentContainer.

    It is mounted inside the Document (depth ≤ 2), not at stream
    top level.  Container check: low 4 bits of ver_inst == 0xF.
    Returns None when absent (no external-object registry = a
    normal state: zero keys, no failure).
    """
    off = doc_offset + 8
    while off + 8 <= doc_end:
        ver_inst, child_type, child_len = struct.unpack_from(
            "<HHI", doc, off,
        )
        child_end = off + 8 + child_len
        if child_end > doc_end:
            break
        if (
            child_type == _REC_EX_OBJ_LIST
            and (ver_inst & 0xF) == 0xF
        ):
            return off
        off = child_end
    return None


def _parse_interactive_info(
    buf: bytes, start: int, container_end: int,
) -> Optional[Dict[str, Any]]:
    """InteractiveInfo (0x0FF2) container → anchor facts.

    The child InteractiveInfoAtom (0x0FF3) has len=16:
    [soundRef@0][hyperlinkID@4][action/oleVerb/jump/flags@8]
    [linkType@12] (byte 0 of the third dword: 0x04 =
    ACTION_HYPERLINK; byte 0 of the fourth dword: 0x08 =
    LINK_Url).  Returns None when there is no atom.
    """
    off = start + 8
    while off + 8 <= container_end:
        _, rec_type, rec_len = _read_record_header(buf, off)
        child_end = off + 8 + rec_len
        if child_end > container_end:
            break
        if (
            rec_type == _REC_INTERACTIVE_INFO_ATOM
            and rec_len >= 16
        ):
            base = off + 8
            return {
                "hyperlink_id": struct.unpack_from(
                    "<I", buf, base + 4,
                )[0],
                "action": buf[base + 8],
                "link_type": buf[base + 12],
            }
        off = child_end
    return None


def _extract_hyperlink_registry(
    doc: bytes, exobj_start: int,
) -> Tuple[Dict[int, Dict[str, str]], int]:
    """Walk ExObjList child records → (hyperlink registry, ExEmbed
    count).

    An ExHyperlink (0x0FD7) container is [ExHyperlinkAtom (id@0) +
    CString×N] (first CString = title, second = url; a lone
    CString is slotted by URL shape).  CString payloads are raw
    UTF-16LE with no prefix (the count lives in the header
    ver_inst, not in the payload — slicing the payload would
    truncate the text).  A link with zero CStrings registers a
    placeholder entry (the id counts toward hyperlink_count; no
    url is invented without evidence).  ExEmbed (0x0FCC) is
    counted, not parsed.  Out-of-range access stops the walk
    (optional channel, never breaks the whole parse).
    """
    registry: Dict[int, Dict[str, str]] = {}
    embed_count = 0
    _, _, rec_len = _read_record_header(doc, exobj_start)
    off = exobj_start + 8
    end = exobj_start + 8 + rec_len
    while off + 8 <= end:
        ver_inst, rec_type, child_len = struct.unpack_from(
            "<HHI", doc, off,
        )
        child_end = off + 8 + child_len
        if child_end > end:
            break
        if rec_type == _REC_EX_EMBED:
            embed_count += 1
        elif (
            rec_type == _REC_EX_HYPERLINK
            and (ver_inst & 0xF) == 0xF
        ):
            link_id = None
            cstrings: List[str] = []
            o = off + 8
            while o + 8 <= child_end:
                _, rt, rl = _read_record_header(doc, o)
                ce = o + 8 + rl
                if ce > child_end:
                    break
                if rt == _REC_EX_HYPERLINK_ATOM and rl >= 4:
                    link_id = struct.unpack_from("<I", doc, o + 8)[0]
                elif rt == _REC_CSTRING and rl:
                    text = doc[o + 8: ce].decode(
                        "utf-16-le", errors="replace",
                    ).rstrip("\x00")[:_CSTRING_MAX_TEXT]
                    if text:
                        cstrings.append(text)
                o = ce
            if link_id is not None:
                entry: Dict[str, str] = {}
                if len(cstrings) >= 2:
                    if cstrings[0]:
                        entry["title"] = cstrings[0]
                    if cstrings[1]:
                        entry["url"] = cstrings[1]
                elif cstrings:
                    # lone CString: slot by URL shape, else title
                    if _looks_like_url(cstrings[0]):
                        entry["url"] = cstrings[0]
                    else:
                        entry["title"] = cstrings[0]
                registry[link_id] = entry
        off = child_end
    return registry, embed_count


def _parse_sp_container(
    buf: bytes, start: int, end: int,
) -> Optional[Dict[str, Any]]:
    """SpContainer (0xF004) child records → shape facts.

    Three sources: Sp (0xF00A) [spid@0][flags@4] (instance =
    shapeType); the ClientData (0xF011) container holding an
    OEPlaceholderAtom (3011) [placementId@0 int][placeholderId@4
    ubyte]; the ClientTextbox (0xF00D) container — a non-empty
    TextChars/TextBytes means has_text.  Shapes with the
    GROUP/PATRIARCH flags are excluded (background/master shells
    are not slide shapes).  spid is namespaced per slide, so
    slide_index is composed by the caller.  No Sp means a broken
    shape: return None (neither counted nor fatal).
    Out-of-range access stops the walk (optional channel).
    """
    spid: Optional[int] = None
    flags = 0
    shape_type = 0
    ph_id: Optional[int] = None
    has_text = False
    off = start
    while off + 8 <= end:
        ver_inst, rec_type, rec_len = _read_record_header(buf, off)
        child_end = off + 8 + rec_len
        if child_end > end:
            break
        if rec_type == _ESCHER_SP and rec_len >= 8:
            spid = struct.unpack_from("<I", buf, off + 8)[0]
            flags = struct.unpack_from("<I", buf, off + 12)[0]
            shape_type = ver_inst >> 4  # instance = shapeType
        elif (
            rec_type == _ESCHER_CLIENT_DATA
            and (ver_inst & 0xF) == 0xF
        ):
            o = off + 8
            while o + 8 <= child_end:
                _, rt, rl = _read_record_header(buf, o)
                ce = o + 8 + rl
                if ce > child_end:
                    break
                if rt == _REC_OE_PLACEHOLDER and rl >= 6:
                    ph_id = buf[o + 8 + 4]  # placeholderId@4 ubyte
                o = ce
        elif (
            rec_type == _ESCHER_CLIENT_TEXTBOX
            and (ver_inst & 0xF) == 0xF
        ):
            o = off + 8
            while o + 8 <= child_end:
                _, rt, rl = _read_record_header(buf, o)
                ce = o + 8 + rl
                if ce > child_end:
                    break
                if (
                    rt in (_REC_TEXT_CHARS, _REC_TEXT_BYTES)
                    and rl
                ):
                    has_text = True
                o = ce
        off = child_end
    if spid is None:
        return None
    if flags & (_SP_FLAG_GROUP | _SP_FLAG_PATRIARCH):
        return None
    fact: Dict[str, Any] = {
        "shape_id": spid,
        "shape_type": shape_type,
        "has_text": has_text,
    }
    if ph_id is not None:
        fact["placeholder_id"] = ph_id
    return fact


def _walk_sp_containers(
    buf: bytes, off: int, end: int,
    facts: List[Dict[str, Any]], depth: int = 0,
) -> None:
    """Recursively locate SpContainers in the Escher container tree.

    The PPDrawing top level is checked directly (a slide's
    PPDrawing holds SpContainer/DgContainer shapes as direct
    children); the container check is low 4 bits of ver_inst ==
    0xF with the type in the Escher space (≥ 0xF000) — the
    [MS-ODRAW] container range 0xF000-0xF11F never collides with
    the [MS-PPT] atom types (same check as _walk_text_records).
    Depth-capped; out-of-range stops the walk.  spid is
    namespaced per slide: entries get slide_index composed by
    the main loop.
    """
    if depth > _RECORD_WALK_MAX_DEPTH:
        return
    while off + 8 <= end:
        ver_inst, rec_type, rec_len = struct.unpack_from(
            "<HHI", buf, off,
        )
        child_end = off + 8 + rec_len
        if child_end > end:
            break
        if (
            rec_type == _ESCHER_SP_CONTAINER
            and (ver_inst & 0xF) == 0xF
        ):
            fact = _parse_sp_container(buf, off + 8, child_end)
            if fact is not None:
                facts.append(fact)
        elif (
            rec_type >= 0xF000
            and (ver_inst & 0xF) == 0xF
        ):
            _walk_sp_containers(
                buf, off + 8, child_end, facts, depth + 1,
            )
        off = child_end


def _extract_shape_facts(
    doc: bytes, sld_offset: int,
) -> List[Dict[str, Any]]:
    """Look for PPDrawing (0x040C) as a direct child of the
    SlideContainer → shape facts.

    PPDrawing sits directly under the SLD container (no nesting).
    Returns the slide's shape facts (group/patriarch already
    excluded); empty when there is no PPDrawing.  Out-of-range
    access stops the walk (optional channel).
    """
    _, _, sld_len = _read_record_header(doc, sld_offset)
    sld_end = sld_offset + 8 + sld_len
    facts: List[Dict[str, Any]] = []
    off = sld_offset + 8
    while off + 8 <= sld_end:
        _, rec_type, rec_len = _read_record_header(doc, off)
        child_end = off + 8 + rec_len
        if child_end > sld_end:
            break
        if rec_type == _REC_PP_DRAWING:
            _walk_sp_containers(doc, off + 8, child_end, facts)
        off = child_end
    return facts


def _walk_text_records(
    buf: bytes, off: int, end: int, state: Dict[str, Any], depth: int = 0,
) -> None:
    """Recursively collect TextChars/TextBytes runs + notesID from
    a container record tree.

    Reused by the SlideContainer recovery (direct SLD children +
    the Escher containers inside PPDrawing (0x040C): Dg/Spgr/Sp/
    ClientTextbox) and by the NotesContainer text walk: the
    container check is low 4 bits of ver_inst == 0xF (MS-PPT atoms
    carry ver ≤ 3, so the two type spaces never collide); a
    SlideAtom (0x03EF) records notesID@16 (first occurrence wins);
    InteractiveInfo (0x0FF2) anchors plus the immediately
    following sibling TxInteractiveInfoAtom (0x0FDF) range are
    collected; out-of-range/truncation stops the walk (complete
    records only); depth-capped.  Never raises — recovery and
    notes are both optional channels: a single corrupt page keeps
    its reached facts without breaking the whole parse.
    """
    if depth > _RECORD_WALK_MAX_DEPTH:
        return
    pending_interactive: Optional[Dict[str, Any]] = None
    while off + 8 <= end:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", buf, off)
        child_end = off + 8 + rec_len
        if child_end > end:
            break
        if (
            pending_interactive is not None
            and rec_type != _REC_TX_INTERACTIVE
        ):
            state["interactive"].append(pending_interactive)
            pending_interactive = None
        if rec_type == _REC_TEXT_CHARS:
            state["runs"].append(
                buf[off + 8: child_end].decode(
                    "utf-16-le", errors="replace",
                ),
            )
            state["chars_atoms"] += 1
        elif rec_type == _REC_TEXT_BYTES:
            state["runs"].append(
                buf[off + 8: child_end].decode(
                    "cp1252", errors="replace",
                ),
            )
            state["bytes_atoms"] += 1
        elif (
            rec_type == _REC_SLIDE_ATOM
            and state["notes_id"] is None
            and rec_len >= 20
        ):
            state["notes_id"] = struct.unpack_from(
                "<I", buf, off + 8 + 16,
            )[0]
        elif (
            rec_type == _REC_INTERACTIVE_INFO
            and (ver_inst & 0xF) == 0xF
        ):
            info = _parse_interactive_info(buf, off, child_end)
            if info is not None:
                pending_interactive = info
        elif (
            rec_type == _REC_TX_INTERACTIVE
            and rec_len >= 8
            and pending_interactive is not None
        ):
            pending_interactive["text_start"] = struct.unpack_from(
                "<I", buf, off + 8,
            )[0]
            pending_interactive["text_end"] = struct.unpack_from(
                "<I", buf, off + 12,
            )[0]
            state["interactive"].append(pending_interactive)
            pending_interactive = None
        elif (ver_inst & 0xF) == 0xF:
            _walk_text_records(buf, off + 8, child_end, state, depth + 1)
        off = child_end
    if pending_interactive is not None:
        state["interactive"].append(pending_interactive)


def _parse_cfb_powerpoint(
    ole: "olefile.OleFileIO",
) -> Tuple[str, Dict[str, Any], Any]:
    """Full CFB parse: returns (body text, structure keys, failure
    message or None).

    Structure keys write only non-zero counters; failure tiers
    still return the facts reached (the caller decides key
    writes).  Any structural step failure raises _PptCfbError,
    converted here to (text="", structure, failure message).
    """
    try:
        entries = ole.listdir()
        stream_names = sorted({e[-1] for e in entries})
        lowered = [e[-1].lower() for e in entries]
        has_ppt_doc = "powerpoint document" in lowered
        has_current_user = "current user" in lowered
        structure: Dict[str, Any] = {
            "extractor": "ppt_cfb_structure_v5",
            "streams": stream_names,
            "powerpoint_document_stream": has_ppt_doc,
            "current_user_stream": has_current_user,
        }
        if not has_ppt_doc:
            structure["note"] = (
                "opened as OLE/CFB; no PowerPoint Document stream "
                "(not a legacy PowerPoint binary; container facts only)"
            )
            raise _PptCfbError(
                "not_powerpoint_binary",
                "no PowerPoint Document stream (not a legacy "
                "PowerPoint binary; container facts registered)",
                structure,
            )
        if not has_current_user:
            structure["note"] = (
                "PowerPoint Document stream present but no Current "
                "User stream (record walk entry point unreachable)"
            )
            raise _PptCfbError(
                "cu_missing",
                "no Current User stream (record walk entry point "
                "unreachable; honest failure)",
                structure,
            )
        doc = ole.openstream("PowerPoint Document").read()
        cu = ole.openstream("Current User").read()
        # OLE SummaryInformation is not read: olefile.get_metadata
        # pathologically spins on corrupt streams (exceptions cannot
        # intercept it); the never-hang contract takes priority.

        # 3. Current User record (8-byte header + payload
        #    size(4)+header_token(4)+offsetToCurrentEdit(4))
        _, cu_type, _ = _read_record_header(cu, 0)
        if cu_type != _REC_CURRENT_USER or len(cu) < 20:
            raise _PptCfbError(
                "cu_record_invalid",
                f"Current User stream is not a valid record "
                f"(recType=0x{cu_type:04X}, len {len(cu)}); "
                "honest failure",
                structure,
            )
        header_token = struct.unpack_from("<I", cu, 8 + 4)[0]
        structure["cu_header_token"] = f"0x{header_token:08X}"
        if header_token != _CU_TOKEN_PPT97:
            raise _PptCfbError(
                "unsupported_cu_token",
                f"Current User header token 0x{header_token:08X} is "
                "not PowerPoint 97+ (0xE391C05F); PowerPoint 95 or "
                "unknown binary not supported, honest failure",
                structure,
            )
        edit_offset = struct.unpack_from("<I", cu, 8 + 8)[0]

        # 4. UserEditAtom chain → persist directory
        uea_offsets = _uea_chain(doc, edit_offset)
        if not uea_offsets:
            raise _PptCfbError(
                "user_edit_invalid",
                f"offsetToCurrentEdit {edit_offset} does not point "
                "at a UserEditAtom record; honest failure",
                structure,
            )
        structure["user_edit_count"] = len(uea_offsets)
        directory = _merge_persist_directory(doc, uea_offsets)
        if not directory:
            raise _PptCfbError(
                "empty_persist",
                "empty persist object directory (no "
                "PersistPtrIncrementalBlock entries reachable)",
                structure,
            )
        structure["persist_entry_count"] = len(directory)

        # Top-level container/media counting: slides/notes/masters
        # come from the PowerPoint Document stream top-level walk;
        # pictures count the top-level BLIP records in the Pictures
        # stream ([MS-PPT] 2.1.7); a missing Pictures stream means
        # zero pictures and no key
        top_records = _stream_records(doc)
        for key, value in {
            "slide_count": sum(
                1 for _, t, _ in top_records if t == _REC_SLIDE
            ),
            "notes_count": sum(
                1 for _, t, _ in top_records if t == _REC_NOTES
            ),
            "master_count": sum(
                1 for _, t, _ in top_records if t == _REC_MAIN_MASTER
            ),
        }.items():
            if value:
                structure[key] = value
        if "pictures" in lowered:
            try:
                pics = ole.openstream("Pictures").read()
            except Exception:
                pics = b""
            pic_count = sum(
                1
                for _, t, _ in _stream_records(pics)
                if t in _BLIP_TYPES
            )
            if pic_count:
                structure["picture_count"] = pic_count

        # 5. DocumentContainer (docPersistIdRef@payload+16)
        doc_pid = struct.unpack_from(
            "<I", doc, uea_offsets[0] + 8 + 16,
        )[0]
        doc_offset = directory.get(doc_pid)
        if doc_offset is None:
            raise _PptCfbError(
                "document_not_found",
                f"docPersistIdRef {doc_pid} not in persist object "
                "directory; honest failure",
                structure,
            )
        _, doc_type, doc_len = _read_record_header(doc, doc_offset)
        if doc_type != _REC_DOCUMENT:
            raise _PptCfbError(
                "document_invalid",
                f"persist object {doc_pid} is not a DocumentContainer "
                f"(recType=0x{doc_type:04X}); honest failure",
                structure,
            )
        doc_end = doc_offset + 8 + doc_len
        if doc_end > len(doc):
            raise _PptCfbError(
                "document_truncated",
                f"DocumentContainer [{doc_offset}, {doc_end}) exceeds "
                f"stream length {len(doc)}; honest failure",
                structure,
            )

        # 6. SlideListWithText instance 0 (the slide-order
        #    carrier; the shared _find_slwt also serves the
        #    instance 2 notes channel)
        slwt_start = _find_slwt(doc, doc_offset, doc_end, 0)
        if slwt_start is None:
            structure["note"] = (
                "DocumentContainer has no SlideListWithText "
                "instance 0 (no slide list; P0 walk boundary)"
            )
            raise _PptCfbError(
                "no_slide_list",
                "no SlideListWithText (instance 0) in "
                "DocumentContainer; slide order unreachable, "
                "honest failure",
                structure,
            )
        slides, atom_stats = _extract_slides(doc, slwt_start)
        structure["slides_with_text"] = sum(
            1 for s in slides if s["runs"]
        )
        chars_atoms = sum(1 for a in atom_stats if a["atom"] == "chars")
        bytes_atoms = sum(1 for a in atom_stats if a["atom"] == "bytes")
        if chars_atoms:
            structure["text_chars_atoms"] = chars_atoms
        if bytes_atoms:
            structure["text_bytes_atoms"] = bytes_atoms
        high_bytes = sum(
            a["chars"] for a in atom_stats if a["atom"] == "bytes"
        )
        if high_bytes:
            structure["high_byte_count"] = high_bytes

        # 7. Concatenation (page order = SPAtom group order; the
        #    blank line between pages keeps page boundaries).
        #    Sanitize per run first, then join with \n/\n\n: the
        #    sanitization state machine deletes 0x0A, so joining
        #    first would swallow the separators (runs would glue
        #    together into one line).
        # 7a. SLWT2 notes reference table: slideIdentifier@12 →
        #     {pid, runs} (in-group inline text wins; the
        #     NotesContainer is a symmetric copy, recovered only
        #     when the group is empty — the same anti-duplication
        #     gate).  A missing instance 2 = no notes, a normal
        #     state.
        slwt2_start = _find_slwt(doc, doc_offset, doc_end, 2)
        notes_refs: Dict[int, Dict[str, Any]] = {}
        notes_chars = notes_bytes = 0
        if slwt2_start is not None:
            notes_slides, notes_atom_stats = _extract_slides(
                doc, slwt2_start,
            )
            for group in notes_slides:
                ident = group.get("slide_identifier")
                if ident is not None:
                    notes_refs[ident] = {
                        "pid": group["persist_id_ref"],
                        "runs": group["runs"],
                    }
            notes_chars = sum(
                1 for a in notes_atom_stats if a["atom"] == "chars"
            )
            notes_bytes = sum(
                1 for a in notes_atom_stats if a["atom"] == "bytes"
            )

        # 7d. ExObjList hyperlink registry (located inside the
        #     DocumentContainer, not at stream top level); absence
        #     = no external-object registry, a normal state with
        #     zero keys and no failure
        hyperlink_registry: Dict[int, Dict[str, str]] = {}
        embed_count = 0
        exobj_off = _find_ex_obj_list(doc, doc_offset, doc_end)
        if exobj_off is not None:
            try:
                hyperlink_registry, embed_count = (
                    _extract_hyperlink_registry(doc, exobj_off)
                )
            except (IndexError, struct.error):
                hyperlink_registry = {}  # optional channel: keep reached facts on corruption

        page_texts: List[str] = []
        sanitize_counts = {
            "paragraph_count": 0,
            "soft_break_count": 0,
            "tab_count": 0,
        }
        recovered_count = 0
        notes_with_text = 0
        hyperlink_anchors: List[Tuple[int, Dict[str, Any]]] = []
        shape_facts: List[Tuple[int, Dict[str, Any]]] = []
        for slide_index, slide in enumerate(slides):
            runs = slide["runs"]
            had_inline_runs = bool(runs)  # gate: SLWT0 group non-empty
            notes_id: Optional[int] = None
            sld_state: Optional[Dict[str, Any]] = None
            # 7b. SlideContainer channel: the notesID pairing key
            #     (per slide) + Escher recovery (only when this
            #     slide's SLWT0 group has zero runs — a healthy
            #     file's SLD Escher text duplicates SLWT0, so
            #     unconditional recovery would double the body
            #     text)
            pid = slide.get("persist_id_ref")
            sld_offset = directory.get(pid) if pid is not None else None
            if sld_offset is not None:
                # Shape facts channel (independent try — does not
                # depend on the text walk succeeding; a single
                # corrupt page does not break the whole parse)
                try:
                    for shape in _extract_shape_facts(
                        doc, sld_offset,
                    ):
                        shape_facts.append((slide_index, shape))
                except (IndexError, struct.error):
                    pass
                try:
                    _, sld_type, sld_len = _read_record_header(
                        doc, sld_offset,
                    )
                    sld_end = sld_offset + 8 + sld_len
                    if (
                        sld_type == _REC_SLIDE
                        and sld_end <= len(doc)
                    ):
                        state: Dict[str, Any] = {
                            "runs": [], "notes_id": None,
                            "chars_atoms": 0, "bytes_atoms": 0,
                            "interactive": [],
                        }
                        _walk_text_records(
                            doc, sld_offset + 8, sld_end, state,
                        )
                        sld_state = state
                        notes_id = state["notes_id"]
                        if not runs and state["runs"]:
                            runs = state["runs"]
                            recovered_count += 1
                except (IndexError, struct.error):
                    pass  # single corrupt slide: recovery is optional, keep going
            # Per-slide anchors = the InteractiveInfo from the
            # SLWT0 group (+ the immediately following
            # TxInteractiveInfoAtom range); SLD anchors are
            # consumed only by recovered slides (healthy pages'
            # SLD is a duplicate copy, same anti-duplication gate)
            group_anchors = list(slide.get("interactive") or [])
            if not had_inline_runs and sld_state is not None:
                group_anchors.extend(
                    sld_state.get("interactive") or [],
                )
            for anchor in group_anchors:
                hyperlink_anchors.append((slide_index, anchor))
            # 7c. notes section: notesID@16 ↔ SLWT2
            #     slideIdentifier@12; SLWT2 in-group runs win, an
            #     empty group falls back to the NotesContainer;
            #     prompt templates live in unreferenced containers
            #     and are naturally excluded by following only the
            #     reference relation (no language filtering)
            notes_line = ""
            if notes_id:
                ref = notes_refs.get(notes_id)
                if ref is not None:
                    n_runs = ref["runs"]
                    if not n_runs and ref["pid"] is not None:
                        n_off = directory.get(ref["pid"])
                        if n_off is not None:
                            try:
                                _, n_type, n_len = _read_record_header(
                                    doc, n_off,
                                )
                                n_end = n_off + 8 + n_len
                                if (
                                    n_type == _REC_NOTES
                                    and n_end <= len(doc)
                                ):
                                    n_state: Dict[str, Any] = {
                                        "runs": [], "notes_id": None,
                                        "chars_atoms": 0,
                                        "bytes_atoms": 0,
                                        "interactive": [],
                                    }
                                    _walk_text_records(
                                        doc, n_off + 8, n_end, n_state,
                                    )
                                    n_runs = n_state["runs"]
                                    notes_chars += n_state["chars_atoms"]
                                    notes_bytes += n_state["bytes_atoms"]
                            except (IndexError, struct.error):
                                pass  # notes container corrupt: no notes, keep going
                    cleaned_notes: List[str] = []
                    for raw in n_runs:
                        clean, counts = _sanitize_ppt_text(raw)
                        cleaned_notes.append(clean)
                        for key, value in counts.items():
                            sanitize_counts[key] += value
                    # pptx contract mirror: notes join into a single line with spaces
                    notes_text = " ".join(cleaned_notes).strip()
                    if notes_text:
                        notes_with_text += 1
                        notes_line = f"Notes: {notes_text}"
            cleaned_runs: List[str] = []
            for raw in runs:
                clean, counts = _sanitize_ppt_text(raw)
                cleaned_runs.append(clean)
                for key, value in counts.items():
                    sanitize_counts[key] += value
            joined = "\n".join(cleaned_runs)
            if joined and notes_line:
                page_texts.append(f"{joined}\n{notes_line}")
            elif joined:
                page_texts.append(joined)
            elif notes_line:
                # A page with empty body but notes: the notes line is
                # the page's output ("success must be non-empty" —
                # either the recovery or the notes channel being
                # non-empty counts as body text)
                page_texts.append(notes_line)
        text = "\n\n".join(page_texts)
        for key, value in sanitize_counts.items():
            if value:
                structure[key] = value
        if recovered_count:
            structure["recovered_slide_count"] = recovered_count
        if notes_with_text:
            structure["notes_with_text"] = notes_with_text
        if notes_chars:
            structure["notes_text_chars_atoms"] = notes_chars
        if notes_bytes:
            structure["notes_text_bytes_atoms"] = notes_bytes
        # 7e. Anchor → registry join + object-level fact keys:
        #     only anchors with action==HYPERLINK and linkType in
        #     url/otherppt/otherfile resolve a target (internal-
        #     jump links carry no URL; forcing a resolve would
        #     invent values); join misses register only
        #     id/action/link_type — no target is invented without
        #     evidence.  Anchors without a range are registered too
        #     (a range is not a necessary condition; honest-fact
        #     semantics)
        hyperlink_records: List[Dict[str, Any]] = []
        anchored_url_count = 0
        for anchor_slide_index, anchor in hyperlink_anchors:
            entry: Dict[str, Any] = {
                "slide_index": anchor_slide_index,
                "hyperlink_id": anchor["hyperlink_id"],
                "action": anchor["action"],
                "link_type": anchor["link_type"],
            }
            if "text_start" in anchor:
                entry["text_start"] = anchor["text_start"]
                entry["text_end"] = anchor["text_end"]
            link = hyperlink_registry.get(anchor["hyperlink_id"])
            url = link.get("url") if link else None
            if (
                url
                and anchor["action"] == _HL_ACTION_HYPERLINK
                and anchor["link_type"] in _HL_LINK_URL_TYPES
            ):
                entry["target"] = url
                anchored_url_count += 1
            hyperlink_records.append(entry)
        if hyperlink_registry:
            structure["hyperlink_count"] = len(hyperlink_registry)
            registry_url_count = sum(
                1 for v in hyperlink_registry.values() if v.get("url")
            )
            if registry_url_count:
                structure["hyperlink_url_count"] = registry_url_count
        if embed_count:
            structure["embed_count"] = embed_count
        if hyperlink_records:
            structure["hyperlink_anchor_count"] = len(hyperlink_records)
            if anchored_url_count:
                structure[
                    "hyperlink_anchored_url_count"
                ] = anchored_url_count
            structure["hyperlinks"] = hyperlink_records[
                :_HYPERLINK_MAX_RECORDS
            ]
        # 7f. Shape fact key family: shape_count + placeholder
        #     counts + bounded shapes list (cap 64); group/
        #     patriarch already excluded at extraction; spid is
        #     per-slide namespaced, so entries carry the composite
        #     slide_index; placeholder semantic words only for
        #     table values, others stay numeric; shape_type
        #     likewise numeric with a conservative vocabulary
        if shape_facts:
            structure["shape_count"] = len(shape_facts)
            ph_total = sum(
                1 for _, s in shape_facts if "placeholder_id" in s
            )
            if ph_total:
                structure["placeholder_shape_count"] = ph_total
            shape_records: List[Dict[str, Any]] = []
            for shape_slide_index, shape in shape_facts:
                shape_entry: Dict[str, Any] = {
                    "slide_index": shape_slide_index,
                    "shape_id": shape["shape_id"],
                    "shape_type": shape["shape_type"],
                    "has_text": shape["has_text"],
                }
                if "placeholder_id" in shape:
                    shape_entry["placeholder_id"] = shape[
                        "placeholder_id"
                    ]
                    ph_name = _PLACEHOLDER_NAMES.get(
                        shape["placeholder_id"],
                    )
                    if ph_name:
                        shape_entry["placeholder"] = ph_name
                if shape["shape_type"] in _SHAPE_TYPE_NAMES:
                    shape_entry["shape"] = _SHAPE_TYPE_NAMES[
                        shape["shape_type"]
                    ]
                shape_records.append(shape_entry)
            structure["shapes"] = shape_records[:_SHAPES_MAX_RECORDS]
        # 8-bit run code-page ambiguity disclosure: high_byte_count
        # carries the total TextBytesAtom characters; the format
        # declares no body code page and none is guessed

        if not text:
            # All three channels — SLWT0 inline, SlideContainer
            # Escher recovery, notes — are zero (an image-only deck
            # without notes): zero content is not dressed up as
            # success ("success must be non-empty" contract)
            raise _PptCfbError(
                "empty_slide_text",
                "no slide text in SlideListWithText or SlideContainer "
                "Escher recovery or notes (image-only deck without "
                "notes; honest failure)",
                structure,
            )
        return text, structure, None

    except _PptCfbError as e:
        # Degradation matrix: reached facts go into keys, text
        # stays empty
        structure = dict(e.facts)
        return "", structure, f"{e.stage}: {e}"
    except (IndexError, struct.error, KeyError) as e:
        # Spec walk out of range/corruption (common in encrypted
        # files): honest failure
        structure.setdefault("note", "record walk hit truncated/corrupt "
                           "structure; facts as far as reached")
        return "", structure, f"walk_failed: record walk out of range ({e})"
