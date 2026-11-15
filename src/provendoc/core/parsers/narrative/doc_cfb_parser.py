# ============================================================
# Narrative Domain
# DOC/CFB (.doc/.dot) Parser — legacy Word binary extraction
# ============================================================
# Contract source: docx_word_cfb_prereg_v1.md (frozen);
# Word 6/95 simple region: docx_word6_simple_prereg_v1.md
# (frozen, p2e).
#
# Fact boundary (Word-family content format; same as DOCX, not source-evidence):
#   - Document.text = main text domain [0, ccpText) rendered body (ccpText@0x4C
#     evidenced: header/footer/footnote domains sit at the CP tail and do not enter the body — same frozen
#     semantics as RTF);
#   - metadata["doc_cfb_structure"] = structural evidence registration (written after
#     olefile opens + the FIB header is readable; failure tiers also write the reached facts — structural
#     evidence is the deliverable, same precedent as docx_package registering on failure);
#   - text location goes entirely through the [MS-DOCMD] spec structures (FIB flags/CLX/PlcfPcd/
#     Pcd.fc); text is never guessed out of binary (spec contract kept);
#   - heading_candidates = [] (style table unparsed, no heading guessing — better absent than fabricated);
#   - Word 6/95 (wIdent=0xA5DC, nFib=101): no piece table;
#     the main text domain is the FibBase fcMin@0x18..fcMac@0x1C simple region; fExtChar
#     bit10 decides 1-byte cp1252 / UTF-16LE double width; the format declares no body
#     codepage (chs=0; probes evidenced that lid is not the content language); 8bit takes the ansi default
#     cp1252 + high_byte_count discloses the ambiguity — no codepage guessing;
#     fComplex=1 (old-style piece table) has no corpus anchor and degrades to unsupported
#     (p2e prereg §2.2/§4);
#   - failures degrade silently (honest failure): non-CFB/unopenable/encrypted/
#     structurally damaged tiers each give text==="" with the tier name in parse_error;
#   - no decryption (fEncrypted degradation is terminal); no macro execution (Project storage
#     is never read); ObjectPool embedded documents are register-only counts without recursion
#     (consistent with the p2b strategy).
#
# Dependency: olefile (CFB container IO, pyproject extra `doc`); text location
# is an in-house spec state machine throughout — no external extractors like antiword.
import hashlib
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

# FIB constants ([MS-DOCMD]; offsets evidenced by probes v2-v4)
_FIB_MIN = 0x1AA          # 97+ FIB readability floor (incl. fcClx@0x1A2/lcbClx@0x1A6)
_WORD97_IDENT = 0xA5EC    # wIdent: Word 97 and later
_WORD6_IDENT = 0xA5DC     # wIdent: Word 6/95 (simple-region path, p2e)
_FLAGS_EXTCHAR = 1 << 10  # flags@10 bit10 fExtChar (word6 character width)
_FIB_MIN_WORD6 = 0x20     # word6 FIB readability floor (fcMac ends at 0x20)
_FIB_OFF_FCMIN = 0x18     # FibBase fcMin (word6 main-text-domain first-character offset)
_CSW_EXPECTED = 0x0E      # csw@0x20: number of FibRgW97 word groups
_FLAGS_ENCRYPTED = 1 << 8   # flags@10 bit8 fEncrypted
_FLAGS_COMPLEX = 1 << 2     # flags@10 bit2 fComplex
_FLAGS_WHICH_TBL = 1 << 9   # flags@10 bit9 fWhichTblStm
_FIB_OFF_FLAGS = 10
_FIB_OFF_CSW = 0x20
_FIB_OFF_CCPLW = 0x4C     # FibRgLw97 field 4: ccpText
_FIB_OFF_FCCLX = 0x1A2    # fcClx（FibRgFcLcb97 pair 33）
_PCD_FC_COMPRESSED = 0x40000000  # Pcd.fc bit30 fCompressed


class _DocCfbError(Exception):
    """Structural failure: carries the tier name and the reached facts (prereg §2.4 degradation matrix)."""

    def __init__(self, stage: str, message: str, facts: Dict[str, Any]):
        super().__init__(message)
        self.stage = stage
        self.facts = facts


class DocCfbParser(BaseParser):
    """Legacy Word binary (OLE/CFB) parser.

    Word 97+ (wIdent=0xA5EC, nFib>=193) takes the full piece-table path:
    CLX scan → PlcfPcd → Pcd.fc dual decoding by the fCompressed bit
    (UTF-16LE / CP1252 half offsets) → truncation to the main text domain ccpText → the sanitization
    state machine (paragraphs/cells/field instructions). fComplex=1 extracts on the same path without degradation
    (probe v3 evidenced Bug33519's 421 pieces extracted correctly); Word 6/95
    (wIdent=0xA5DC) takes the FibBase fcMin..fcMac simple-region dual-width extraction
    (p2e prereg); encryption and structural damage each go to their honest-failure tier
    (structural facts are still registered).
    """

    def parse(self, file_path) -> Document:
        """Parse .doc/.dot into a Document (main-text-domain body + structural evidence)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "doc"
        start_time = time.perf_counter()

        try:
            raw = Path(file_path).read_bytes()
            metadata["sha256"] = hashlib.sha256(raw).hexdigest()

            # 1. CFB header validation (prereg §2.2 step 1): non-OLE/CFB containers
            #    honest failure — covering Graphviz DOT text (suffix ambiguity:
            #    content language not guessed) and truncated files; no keys written before FIB is reached.
            container_facts = classify_container(raw)
            if not container_facts["ole_container"]:
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "not an OLE/CFB container (legacy Word binary "
                    "expected; format mismatch honest failure)"
                )
                metadata["parse_duration_ms"] = round(
                    (time.perf_counter() - start_time) * 1000, 3,
                )
                return Document(text="", metadata=metadata)

            # 2. olefile open (step 2)
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
                text, structure, failure = _parse_cfb_word(ole)

            if structure:
                metadata["doc_cfb_structure"] = structure
                metadata["heading_candidates"] = []  # styles unparsed, no guessing

            if failure is not None:
                metadata["parse_failed"] = True
                metadata["parse_error"] = failure
                text = ""

            # Security-carrier census (invisible_carrier_v1,
            # narrative_security_closure prereg F2c; same bypass contract as pptx):
            # doc extracts the body from CFB binary streams, not through the base
            # _read_and_diagnose path, and mounts the same census on the post-extraction body view;
            # on failure tiers with text=="" the census is naturally empty and zero keys are written (an empty body has no
            # census surface). When structured counts are non-zero,
            # metadata["security_carriers"] is written and the fact codes are appended into warnings.
            _carrier_census = _detect_security_carriers(text)
            if _carrier_census:
                metadata["security_carriers"] = _carrier_census
                metadata.setdefault("warnings", []).extend(
                    _build_carrier_facts(_carrier_census)
                )

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            # Working-text sanitization (pptx contract): normalizes Document.text only (doc
            # is a binary container; the source_text contract is None); structural facts are already
            # done above on the raw view, and the sanitization action is
            # disclosed as-is via metadata["sanitization"] + a warning.
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
        except Exception as e:  # never crashes (real_public contract)
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            return Document(text="", metadata=metadata)


# ------------------------------------------------------------
# Structural parsing (module-level pure functions: no instance state; directly testable)
# ------------------------------------------------------------

def _parse_cfb_word(ole: "olefile.OleFileIO") -> Tuple[str, Dict[str, Any], Any]:
    """Full CFB parse: returns (body, structure keys, failure message or None).

    Structure keys are count entries written only when non-zero per prereg §2.1; failure tiers also return
    the reached facts (the caller decides key writes). Any structural step failing raises _DocCfbError,
    converted by this function to (text="", structure, failure message).
    """
    try:
        entries = ole.listdir()  # all-level stream entries (case-variant corpus;
        # olefile name comparison is naturally case-insensitive, S9)
        stream_names = sorted({e[-1] for e in entries})
        storages = ole.listdir(streams=False, storages=True)
        pool_count = sum(
            1 for e in storages
            if any(c.lower() == "objectpool" for c in e[:-1])
        )
        structure: Dict[str, Any] = {
            "extractor": "doc_cfb_structure_v1",
            "streams": stream_names,
        }
        if pool_count:
            structure["object_pool_count"] = pool_count

        # 3. FIB header (steps 3-5)
        if not ole.exists("WordDocument"):
            raise _DocCfbError(
                "missing_worddocument",
                "no WordDocument stream (not a Word binary)",
                structure,
            )
        wd = ole.openstream("WordDocument").read()
        if len(wd) < _FIB_MIN_WORD6:
            raise _DocCfbError(
                "fib_truncated",
                f"WordDocument stream too short ({len(wd)} < 0x20)",
                structure,
            )
        w_ident, n_fib = struct.unpack_from("<HH", wd, 0)
        flags = struct.unpack_from("<H", wd, _FIB_OFF_FLAGS)[0]
        structure["word_ident"] = w_ident
        structure["nfib"] = n_fib
        structure["f_encrypted"] = bool(flags & _FLAGS_ENCRYPTED)
        structure["f_complex"] = bool(flags & _FLAGS_COMPLEX)

        # 3a. Word 6/95 dispatch (p2e prereg §2.2 W1): the old FIB has no
        #     piece table; the main text domain is the fcMin..fcMac simple region.
        if w_ident == _WORD6_IDENT:
            text = _parse_word6_simple(wd, flags, structure)
            return text, structure, None

        if len(wd) < _FIB_MIN:
            raise _DocCfbError(
                "fib_truncated",
                f"WordDocument stream too short ({len(wd)} < 0x1AA)",
                structure,
            )
        if w_ident != _WORD97_IDENT or n_fib < 193:
            raise _DocCfbError(
                "unsupported_word6_95",
                f"unrecognized legacy Word binary "
                f"(wIdent={w_ident}, nFib={n_fib}); only 97+ piece "
                "table and Word 6/95 simple zone are supported",
                structure,
            )
        if flags & _FLAGS_ENCRYPTED:
            raise _DocCfbError(
                "encrypted",
                "encrypted document (fEncrypted); decryption is out "
                "of scope, honest failure",
                structure,
            )
        csw = struct.unpack_from("<H", wd, _FIB_OFF_CSW)[0]
        if csw != _CSW_EXPECTED:
            raise _DocCfbError(
                "fib_invalid",
                f"unexpected csw {csw} (FIB layout unreadable)",
                structure,
            )

        # 6. Table-stream routing (fWhichTblStm bit, S2)
        tbl_name = "1Table" if flags & _FLAGS_WHICH_TBL else "0Table"
        if not ole.exists(tbl_name):
            raise _DocCfbError(
                "missing_table_stream",
                f"no {tbl_name} stream (piece table unreachable)",
                structure,
            )
        tbl = ole.openstream(tbl_name).read()
        structure["table_stream"] = tbl_name

        # 7. CLX（fcClx/lcbClx）
        fc_clx, lcb_clx = struct.unpack_from("<II", wd, _FIB_OFF_FCCLX)
        if lcb_clx == 0:
            structure["note"] = "empty piece table (lcbClx=0)"
            return "", structure, (
                "empty_main_text: empty piece table (lcbClx=0); "
                "no text domain to extract"
            )
        if fc_clx + lcb_clx > len(tbl):
            raise _DocCfbError(
                "clx_out_of_range",
                f"CLX [{fc_clx}, {fc_clx + lcb_clx}) exceeds "
                f"{tbl_name} stream length {len(tbl)}",
                structure,
            )
        clx = tbl[fc_clx: fc_clx + lcb_clx]
        i = 0
        plcf: bytes | None = None
        while i < len(clx):
            tag = clx[i]
            if tag == 0x01:  # prefix block (GrpPrl): skip
                if i + 3 > len(clx):
                    raise _DocCfbError(
                        "clx_corrupt", "truncated Prc prefix block",
                        structure,
                    )
                i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
            elif tag == 0x02:  # PlcfPcd block
                if i + 5 > len(clx):
                    raise _DocCfbError(
                        "clx_corrupt", "truncated PlcfPcd header",
                        structure,
                    )
                lcb = struct.unpack_from("<I", clx, i + 1)[0]
                plcf = clx[i + 5: i + 5 + lcb]
                break
            else:
                raise _DocCfbError(
                    "clx_corrupt",
                    f"unknown CLX tag 0x{tag:02x} at offset {i}",
                    structure,
                )
        if plcf is None:
            raise _DocCfbError(
                "clx_corrupt", "no PlcfPcd block in CLX", structure,
            )

        # 8. PlcfPcd: n+1 aCP entries + n×8 bytes of aPcd
        if len(plcf) < 16 or (len(plcf) - 4) % 12 != 0:
            raise _DocCfbError(
                "plcf_corrupt",
                f"PlcfPcd size {len(plcf)} not a valid (n+1)·4+n·8 "
                "layout",
                structure,
            )
        n = (len(plcf) - 4) // 12
        if n == 0:
            structure["piece_count"] = 0
            structure["note"] = "empty piece table (n=0)"
            return "", structure, (
                "empty_main_text: empty piece table (n=0); "
                "no text domain to extract"
            )
        cps = struct.unpack_from(f"<{n + 1}I", plcf, 0)
        if any(cps[k + 1] < cps[k] for k in range(n)):
            raise _DocCfbError(
                "cp_non_monotonic",
                "aCP array is non-monotonic (text order unreliable)",
                structure,
            )
        ccp_total = cps[-1] - cps[0]
        structure["piece_count"] = n
        structure["ccp_total"] = ccp_total

        # 9. piece decoding (fCompressed → CP1252 half offsets / UTF-16LE)
        chars: List[str] = []
        unicode_pieces = ansi_pieces = 0
        out_of_range: int | None = None
        for k in range(n):
            count = cps[k + 1] - cps[k]
            if count <= 0:
                continue
            pcd_off = 4 * (n + 1) + 8 * k
            fc = struct.unpack_from("<I", plcf, pcd_off + 2)[0]
            if fc & _PCD_FC_COMPRESSED:
                start = (fc & 0x3FFFFFFF) >> 1
                end = start + count
                if end > len(wd):
                    out_of_range = k
                    break
                unicode_ = False
            else:
                start = fc & 0x3FFFFFFF
                end = start + 2 * count
                if end > len(wd):
                    out_of_range = k
                    break
                unicode_ = True
            if unicode_:
                unicode_pieces += 1
                seg = wd[start:end].decode("utf-16-le", errors="replace")
            else:
                ansi_pieces += 1
                seg = wd[start:end].decode("cp1252", errors="replace")
            chars.append(seg)
        if out_of_range is not None:
            raise _DocCfbError(
                "piece_out_of_range",
                f"piece {out_of_range} fc range exceeds WordDocument "
                "stream (truncated stream; text not guessable)",
                structure,
            )
        if unicode_pieces:
            structure["unicode_pieces"] = unicode_pieces
        if ansi_pieces:
            structure["ansi_pieces"] = ansi_pieces

        # 10. Main-text-domain truncation (ccpText@0x4C, evidenced by S5)
        ccp_text = struct.unpack_from("<I", wd, _FIB_OFF_CCPLW)[0]
        structure["ccp_text"] = ccp_text
        if ccp_text > ccp_total:
            structure["ccp_text"] = ccp_total
            structure["note"] = (
                f"ccpText {ccp_text} exceeds piece-table total "
                f"{ccp_total}; truncated to total (piece table is "
                "authoritative)"
            )
            ccp_text = ccp_total
        text = "".join(chars)[:ccp_text]
        if ccp_text == 0:
            raise _DocCfbError(
                "empty_main_text",
                "empty main text domain (ccpText=0)",
                structure,
            )

        # 11. Sanitization state machine
        text, counts = _sanitize_doc_text(text)
        for key, value in counts.items():
            if value:
                structure[key] = value
        if not text:
            # The main text domain contains only control characters (image placeholders / field codes; Crash067 evidence:
            # ccpText=1 with the single character 0x03) — degrade as an empty main document,
            # matching the BINARY_CONTAINER_FORMATS empty-variant semantics (prereg
            # §2.4: zero content is not dressed up as success).
            raise _DocCfbError(
                "empty_main_text",
                "main text domain is empty after control-character "
                "sanitization (placeholder/field codes only)",
                structure,
            )
        return text, structure, None

    except _DocCfbError as e:
        # Degradation matrix (prereg §2.4): reached facts go into keys; text is always empty
        structure = dict(e.facts)
        return "", structure, f"{e.stage}: {e}"


def _parse_word6_simple(
    wd: bytes, flags: int, structure: Dict[str, Any],
) -> str:
    """Word 6/95 simple-region extraction (prereg docx_word6_simple_prereg_v1 §2.2).

    The old FIB (wIdent=0xA5DC, nFib=101) has no piece table: the main text domain is
    FibBase fcMin@0x18..fcMac@0x1C; fExtChar bit10 decides the character width
    (1-byte cp1252 / UTF-16LE). The format declares no body codepage (chs=0;
    probes evidenced lid=0x0409 on Russian corpus — lid is the UI language, not the content language);
    8bit takes the ansi default cp1252 + high_byte_count discloses the ambiguity —
    no codepage guessing (prereg §4 frozen). Sanitization reuses the 97+ state machine
    (probes evidenced control characters are only 0x0D/0x09, the same semantic family). Failure raises
    _DocCfbError and the caller degrades (§2.3 matrix).
    """
    f_ext_char = bool(flags & _FLAGS_EXTCHAR)
    structure["f_ext_char"] = f_ext_char
    if flags & _FLAGS_COMPLEX:
        # Old-style piece table (not isomorphic to the 97 CLX): zero corpus
        # evidence — no guessing, degrade to unsupported (prereg §2.3).
        raise _DocCfbError(
            "unsupported_word6_complex",
            "Word 6 complex (fast-saved) old piece table not "
            "supported (no corpus anchor; honest failure)",
            structure,
        )
    if flags & _FLAGS_ENCRYPTED:
        raise _DocCfbError(
            "encrypted",
            "encrypted document (fEncrypted); decryption is out "
            "of scope, honest failure",
            structure,
        )
    fc_min, fc_mac = struct.unpack_from("<II", wd, _FIB_OFF_FCMIN)
    structure["fc_min"] = fc_min
    structure["fc_mac"] = fc_mac
    if fc_mac > len(wd) or fc_min > fc_mac:
        raise _DocCfbError(
            "simple_zone_out_of_range",
            f"simple zone [{fc_min}, {fc_mac}) exceeds WordDocument "
            f"stream length {len(wd)} (truncated; text not guessable)",
            structure,
        )
    if fc_min == fc_mac:
        raise _DocCfbError(
            "empty_main_text",
            "empty simple zone (fcMin==fcMac); no text domain",
            structure,
        )
    raw = wd[fc_min:fc_mac]
    if f_ext_char:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        text = raw.decode("cp1252", errors="replace")
        structure["charset"] = "cp1252"
        high = sum(1 for b in raw if b >= 0x80)
        if high:
            structure["high_byte_count"] = high
    text, counts = _sanitize_doc_text(text)
    for key, value in counts.items():
        if value:
            structure[key] = value
    if not text:
        # The simple region holds only placeholder/field control characters — zero content is not dressed up as success (same
        # semantics as the 97+ empty_main_text tier, prereg §2.3).
        raise _DocCfbError(
            "empty_main_text",
            "simple zone is empty after control-character "
            "sanitization (placeholder/field codes only)",
            structure,
        )
    return text


def _sanitize_doc_text(text: str) -> Tuple[str, Dict[str, int]]:
    """DOC text sanitization state machine (prereg §2.3 frozen table).

    Paragraph/cell/line-break/page-break → newline (table text flows out linearly without loss,
    the spec's lowest tier "raw text preserved"); field instructions (0x13..0x14)
    are dropped and field results (0x14..0x15) kept (same semantics as RTF fldinst/fldrslt);
    the remaining 0x00-0x1F control characters are deleted ([MS-DOCMD] placeholders/reference
    marks are not rendered text); tabs kept.
    """
    counts = {
        "paragraph_count": 0,
        "line_break_count": 0,
        "page_break_count": 0,
        "cell_end_count": 0,
        "field_count": 0,
        "tab_count": 0,
    }
    out: List[str] = []
    # Field-frame stack: False = instruction domain (before 0x13..0x14), True = result domain
    # (0x14..0x15). Emit iff the stack is empty or all frames are True — nested
    # fields inside an outer instruction domain count as instruction wholesale (conservative
    # direction; probe corpus has only flat fields).
    field_frames: List[bool] = []
    for ch in text:
        code = ord(ch)
        if code == 0x13:
            field_frames.append(False)
            counts["field_count"] += 1
            continue
        if code == 0x14:
            if field_frames:
                field_frames[-1] = True
            continue
        if code == 0x15:
            if field_frames:
                field_frames.pop()
            continue
        if field_frames and not all(field_frames):
            continue  # instruction domain dropped ('PAGE 12' etc. never enters the body)
        if code == 0x0D:
            counts["paragraph_count"] += 1
            out.append("\n")
        elif code == 0x07:
            counts["cell_end_count"] += 1
            out.append("\n")
        elif code == 0x0B:
            counts["line_break_count"] += 1
            out.append("\n")
        elif code == 0x0C:
            counts["page_break_count"] += 1
            out.append("\n")
        elif code == 0x09:
            counts["tab_count"] += 1
            out.append("\t")
        elif code < 0x20:
            continue  # placeholder/reference control characters: deleted, not counted
        else:
            out.append(ch)
    return "".join(out).rstrip("\n"), counts
