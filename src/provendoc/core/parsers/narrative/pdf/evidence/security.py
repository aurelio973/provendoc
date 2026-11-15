"""Evidence-only extraction of PDF interactive & security objects.

B7 scope (gap register): annotations, forms (AcroForm/XFA), signatures,
redaction marks, JavaScript actions, embedded files / portfolios, rich
media, optional-content layers, and encryption/permission facts.

Everything here is *evidence*: JavaScript is never executed, field
calculation orders are never evaluated, signatures are never
cryptographically verified, and no content is decrypted.  A locked
document (user password required) reports the lock and marks the
remaining sections unavailable instead of guessing.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]


def _perm_bit(name: str) -> int:
    """pymupdf permission-bit constants (older fitz names differ/missing → treated as 0, i.e. deny)."""
    return int(getattr(fitz, name, 0))


# Caps keep pathological documents from monopolizing the run: totals are
# still counted (bounded by the page scan cap) while samples stay small.
_MAX_PAGES_SCANNED = 200
_MAX_ANNOT_SAMPLES = 100
_MAX_FIELD_SAMPLES = 100
_MAX_EMBEDDED_SAMPLES = 100
_MAX_TEXT_CHARS = 200

# Attachments beyond this size are registered but not hashed in full —
# resource-exhaustion safety (spec §20, security class); the skip is explicit.
_MAX_EMBEDDED_HASH_BYTES = 64 * 1024 * 1024

# Annotation subtypes that carry audio/video/3D/multimedia payloads.
_MEDIA_SUBTYPES = {"Screen", "Movie", "Sound", "RichMedia", "3D"}

# /Subtype /Redact — a *mark*; the text under the rect tells whether the
# content was actually removed.
_REDACT_SUBTYPE = "Redact"

# Visual obscuration caps: drawn near-black filled rectangles large
# enough to cover text, with body text still extractable underneath.
_MAX_OBSCURATION_SAMPLES = 20
_MIN_OBSCURATION_AREA = 100.0
_MIN_OBSCURATION_SIDE = 8.0
_NEAR_BLACK_CHANNEL_MAX = 0.1

_BYTE_RANGE_RE = re.compile(r"ByteRange\s*\[([^\]]*)\]")
#: /ByteRange body as an indirect reference (adversarial round 3 deep-dive P22: the array object lives
#: separately; scanning the V-dict body directly never matches) — capture the reference string, then follow one hop.
_BYTE_RANGE_INDIRECT_RE = re.compile(r"ByteRange\s+(\d+\s+\d+\s+R)")
_LAYER_NAME_RE = re.compile(r"/Name\s*\(([^)]*)\)")
_OCG_TYPE_RE = re.compile(r"/Type\s*/OCG\b")
_REF_RE = re.compile(r"\d+\s+\d+\s+R\b")

# pdf_security_semantics_prereg_v1 §1: the /S /JavaScript subtype markers on action mount points
# (a structural-audit fact, not a behavioral verdict). Loose whitespace matching covers both
# <</S/JavaScript/JS ...>> and /S /JavaScript serializations; \b guards
# against in-string false hits (e.g. "JavaScripty").
_JS_SUBTYPE_RE = re.compile(r"/S\s*/JavaScript\b")

# An integer value is a single indirect reference ('N 0 R'): the precondition for following one hop.
_INDIRECT_REF_RE = re.compile(r"^(\d+)\s+\d+\s+R$")

# Follow cap for indirect elements inside a single mount point (array form, /Next chain, /AA sub-actions)
# — a pathological document cannot turn structural auditing into unbounded traversal.
_MAX_ACTION_REF_FOLLOWS = 16

# Adversarial round 6: a census of non-JS action subtypes (/S /Launch, /S /SubmitForm,
# /S /GoToR and other TTP carriers of APT weaponized documents). Same mount point, same discipline:
# read-only xref tables, one-hop indirect-reference following, bounded expansion of arrays/Next chains;
# the JavaScript subtype belongs to the javascript fact section and is not double-registered here. The name charset after /S
# is loose ([A-Za-z0-9]+); unknown subtypes are recorded as-is (over-record rather than guess).
_ACTION_SUBTYPE_RE = re.compile(r"/S\s*/([A-Za-z0-9]+)")

# Action-target literal strings (/F (calc.exe), /URI (https://…)): take the first match only within
# a small window after the /S hit, avoiding cross-action misattribution inside one mount body.
# /F may also take array form (SubmitForm with multiple exfil targets) — allow an optional '[' prefix and
# register the first array element as-is (adversarial round 7 T5).
_ACTION_TARGET_LITERAL_RE = re.compile(
    r"/(?:URI|F)\s*(?:\[\s*)?\(([^)\r\n]{0,120})\)"
)
_ACTION_TARGET_WINDOW = 200

# Census sample cap: count records the full truth; samples are capped for disclosure.
_MAX_ACTION_SAMPLES = 50

# pdf_security_semantics_prereg_v1 §2 (D2) + §6.2 (Q2-A decision):
# the extraction layer is unreliable for visibility semantics (four-form drops, position-dependent Tr3
# three-state, visible text squeezable out of the extraction layer by hidden text) — the only source of truth is direct content-stream audit. This
# section registers disclosure facts only: no rendering, no extraction-layer changes, no byte rewrites.
_INVISIBLE_SAMPLE_CAP = 20
_TINY_FONT_SIZE_MAX = 1.0
_MEDIA_BOX_EPS = 0.01

# String literals in content streams are stripped before operator scanning: "Tj"/"Tr" and the like in string data
# no longer masquerade as operators, keeping counts free of in-string noise. The replacement string carries a trailing
# space: real corpora contain the no-space form `(\x04)Tj`; if stripped to `STRTj`, `\b`-style word boundaries vanish
# and entire show events go uncounted (PMC13529368 page 10 is exactly this form; it once caused
# the Tr3 census 21 vs detection 20 reconciliation gap of 1).
_STRING_LITERAL_RE = re.compile(r"\((?:\\.|[^\\()])*\)")

# Text-block location (bytes-level find, C-level memmem): all state-machine output happens
# inside BT..ET ranges — state fully resets at BT, show events are registered only
# while in_text, so graphics regions outside BT contribute zero. Block first, then run the state-machine regex
# only on text blocks — strictly equivalent semantics: vector-heavy pages (measured 49.8MB of content stream
# on one page, 52% numeric bytes) no longer turn the scan into minutes. Word boundaries (\b) are
# checked manually at the find layer, avoiding the regex alternation's per-position backtracking.
_BLOCK_WORD_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)

# Keyword scan of visibility-relevant operators (the operator stream after literal stripping): inside BT/ET blocks
# track rendering mode, font size, white fill and text origin; Tj/TJ/'/" are text show
# events. The original 12-branch alternation with numeric prefixes backtracked branch by branch at every scan position
# (measured 2.93s on one file); the keyword-style alternation is ~2 orders of magnitude faster
# with an unchanged match set: operand-bearing branches (Tr/Tf/rg/RG/g/Tm/Td/TD) in the
# original regex all required "operand immediately followed by operator, whitespace only between" and matched terminally at the operator,
# so operand semantics are now parsed by taking a fixed-length tail before the operator + \Z anchoring, strictly equivalent to the original
# backtracking match (window truncation only affects illegal streams whose operand-operator whitespace exceeds the window). Word
# boundaries replicate the original \b with \w lookarounds (\w in str regexes includes Unicode letters).
_OPS_TOKEN_RE = re.compile(
    r"(?<!\w)(BT|ET|Tr|Tf|Tm|Td|TD|Tj|TJ|rg|RG|g)(?!\w)"
    r"|[\x27\x22]"
)

# Operator tail parsing: token-style. The original used \Z-anchored search, backtracking the whole
# num pattern at every start position (cProfile: 26µs per call, 2.1s per file); now
# finditer collects numeric tokens in the window and takes the last `count` of them, verifying
# whitespace-only separation — position-by-position equivalent to each original branch's
# `num(\s+num)*\s+operator` adjacency constraint (branch priority at the same start position
# was also audited: g merges into a take-tail whiteness check; rg counts as white only when
# all three operand literals are "1", any other three operands clear whiteness). Window truncation only affects illegal streams whose operand-operator whitespace exceeds the window.
_NUM_TOKEN_RE = re.compile(r"-?[0-9]*\.?[0-9]+")


def _tail_numbers(tail: str, count: int) -> list[str] | None:
    """Take the `count` numeric strings immediately preceding the operator (whitespace-only separation required); None if absent.

    Reverse manual parsing, position-by-position equivalent to taking the tail `count`
    of `_NUM_TOKEN_RE.findall` then verifying whitespace-only separation (covering pathological
    splits like `1.5.2` → tail token `.2`, `x.5` → `.5`,
    `-.2`: after the trailing digits, at most one dot; digits before the dot meeting another dot are spat back to the previous token). cProfile: the finditer+list version over the full corpus
    made ~1.76M calls, about half the total runtime; the reverse version uses zero regex, zero allocation.
    """
    if not tail or not tail[-1].isspace():
        return None
    i = len(tail)
    out: list[str] = []
    for _ in range(count):
        # num and the operator / previous num must be separated by >=1 whitespace (the original `\s+`).
        j = i
        while j > 0 and tail[j - 1].isspace():
            j -= 1
        if j == i:
            return None
        i = j
        # Reverse-parse one `-?[0-9]*\.?[0-9]+` token; it must end with a digit.
        k = i
        if k == 0 or not tail[k - 1].isdigit():
            return None
        while k > 0 and tail[k - 1].isdigit():
            k -= 1
        if k > 0 and tail[k - 1] == ".":
            k -= 1
            frac_start = k
            while k > 0 and tail[k - 1].isdigit():
                k -= 1
            if k > 0 and tail[k - 1] == "." and frac_start != k:
                # Digits before the dot meeting another dot: they belong to the previous token
                # (e.g. the tail token of `1.5.2` is `.2`); spit them back before the dot.
                k = frac_start
        if k > 0 and tail[k - 1] == "-":
            k -= 1
        out.append(tail[k:i])
        i = k
    out.reverse()
    return out

_INVISIBLE_FORMS = (
    "render_mode_3",
    "white_on_white",
    "tiny_font",
    "off_page",
)

# pdf_security_semantics_prereg_v1 §3 (D3) + §6.3 (Q3-A decision):
# ToUnicode CMaps can lie — the rendering layer shows A while the extraction layer returns other characters. Audit
# layering: base14 Latin fonts (Helvetica/Times/Courier families) under
# WinAnsi/StandardEncoding, the digit/letter ranges (0x30-0x39/0x41-0x5A/
# 0x61-0x7A) have an identity-code-value subset; a ToUnicode mapping of an identity code to another
# character is a structural contradiction (cmap_mismatch). Symbol/ZapfDingbats, CID/Type0,
# embedded subsets and /Differences encodings have no ground truth → unauditable, recorded without guessing.
# The identity range is the conservative common core of both encodings across the WinAnsi table; zero false positives first.
_BASE14_IDENTITY_FONTS = {
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-Oblique",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Bold",
    "Times-Italic",
    "Times-BoldItalic",
    "Courier",
    "Courier-Bold",
    "Courier-Oblique",
    "Courier-BoldOblique",
}
_IDENTITY_CODES = frozenset(
    list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x61, 0x7B))
)
_KNOWN_BASE14_ENCODINGS = {"/WinAnsiEncoding", "/StandardEncoding"}
_MAX_CMAP_MISMATCH_SAMPLES = 20

# ToUnicode CMap parsing: bfchar blocks and bfrange blocks (the single-dst incremental form
# and the dst-array form). Only 4-hex-digit (single Unicode code point) dsts are taken; multi-code-point
# strings (e.g. <00410041>) have no identity-comparison meaning and are conservatively skipped.
_BFCHAR_BLOCK_RE = re.compile(r"beginbfchar\s*(.*?)\s*endbfchar", re.S)
_BFRANGE_BLOCK_RE = re.compile(r"beginbfrange\s*(.*?)\s*endbfrange", re.S)
_BFCHAR_ENTRY_RE = re.compile(r"<([0-9A-Fa-f]{1,4})>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_ENTRY_RE = re.compile(
    r"<([0-9A-Fa-f]{1,4})>\s*<([0-9A-Fa-f]{1,4})>\s*(<[0-9A-Fa-f]+>|\[)"
)
_BFRANGE_ARRAY_ITEM_RE = re.compile(r"<([0-9A-Fa-f]+)>")

_NUM_RE = re.compile(r"-?[0-9]*\.?[0-9]+")


def _unavailable(reason: str) -> dict[str, Any]:
    return {"unavailable": reason}


def extract_security_evidence(file_path: str | Path) -> dict[str, Any]:
    """Collect interactive/security evidence from one PDF file.

    The function never raises: any unreadable section degrades to an
    ``unavailable`` marker so the rest of the evidence survives.
    """
    try:
        document = fitz.open(file_path)
    except Exception as error:
        return {"available": False, "error": str(error)}
    try:
        return _collect(document, Path(file_path))
    except Exception as error:  # Defensive: evidence must never be fatal.
        return {"available": False, "error": str(error)}
    finally:
        document.close()


def _collect(document: fitz.Document, path: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {"available": True}

    locked = bool(document.needs_pass)
    evidence["locked"] = locked
    evidence["encryption"] = _encryption_facts(document, locked)
    # Revision-chain registration does not depend on content access (trailer + raw bytes); locked-down documents still report.
    evidence["incremental_updates"] = _incremental_update_facts(
        document, path
    )

    if locked:
        # Without the user password every content-bearing section is
        # inaccessible; the honest answer is a per-section marker, not
        # fabricated empties.
        reason = "user password required; document content not accessible"
        for section in (
            "annotations",
            "redactions",
            "media",
            "forms",
            "signatures",
            "embedded_files",
            "layers",
            "invisible_text",
            "cmap_audit",
        ):
            evidence[section] = _unavailable(reason)
        evidence["javascript"] = _unavailable(reason)
        return evidence

    evidence["annotations"] = _annotation_facts(document)
    evidence["redactions"] = _redaction_facts(document, evidence["annotations"])
    evidence["media"] = _media_facts(document, evidence["annotations"])
    evidence["forms"] = _form_facts(document)
    evidence["signatures"] = _signature_facts(document, path)
    evidence["javascript"] = _javascript_facts(document)
    evidence["document_actions"] = _document_action_facts(document)
    evidence["invisible_text"] = _invisible_text_facts(document)
    evidence["cmap_audit"] = _cmap_audit_facts(document)
    evidence["embedded_files"] = _embedded_file_facts(document)
    evidence["layers"] = _layer_facts(document)
    return evidence


# ------------------------------------------------------------
# encryption & permissions
# ------------------------------------------------------------

def _encryption_facts(document: fitz.Document, locked: bool) -> dict[str, Any]:
    # ``is_encrypted`` flips to False once MuPDF auto-authenticates with
    # an empty user password, so the trailer /Encrypt entry is the
    # factual "an encryption dictionary exists" source.
    try:
        trailer = str(document.pdf_trailer())
    except Exception:
        trailer = ""
    encrypted = bool(document.is_encrypted) or locked or "/Encrypt" in trailer
    facts: dict[str, Any] = {
        "encrypted": encrypted,
        "needs_user_password": locked,
    }
    try:
        permissions = int(document.permissions)
    except Exception:
        permissions = 0
    facts["permissions_raw"] = permissions
    facts["permissions"] = {
        "print": bool(permissions & _perm_bit("PDF_PERM_PRINT")),
        "modify": bool(permissions & _perm_bit("PDF_PERM_MODIFY")),
        "copy": bool(permissions & _perm_bit("PDF_PERM_COPY")),
        "annotate": bool(permissions & _perm_bit("PDF_PERM_ANNOTATE")),
        "form_filling": bool(permissions & _perm_bit("PDF_PERM_FORM")),
        "accessibility": bool(permissions & _perm_bit("PDF_PERM_ACCESSIBILITY")),
    }
    if locked:
        facts["restricted"] = True
        facts["note"] = (
            "document requires the user (open) password; permission bits "
            "are reported as denied because nothing was authenticated"
        )
        return facts

    decoded = facts["permissions"]
    restricted = not all(
        (
            decoded["print"],
            decoded["modify"],
            decoded["copy"],
            decoded["annotate"],
        )
    )
    facts["restricted"] = restricted
    if document.is_encrypted or restricted:
        facts["note"] = (
            "document opens without a password but carries permission "
            "restrictions (owner password protected); extraction records "
            "the bits and never tries to lift them"
        )
    return facts


# ------------------------------------------------------------
# annotations (single pass over pages)
# ------------------------------------------------------------

def _scan_annotations(document: fitz.Document) -> dict[str, Any]:
    """One bounded pass collecting annotation facts per page.

    Returns counts by subtype, samples with author/content/associated
    body text, and the page-scan truncation flag.
    """
    by_type: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    total = 0
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    truncated_pages = int(document.page_count) > pages_scanned

    for page_index in range(pages_scanned):
        try:
            page = document[page_index]
            annots = list(page.annots())
        except Exception:
            continue
        for annot in annots:
            try:
                subtype = str(annot.type[1])
            except Exception:
                subtype = "Unknown"
            total += 1
            by_type[subtype] = by_type.get(subtype, 0) + 1
            if len(samples) >= _MAX_ANNOT_SAMPLES:
                continue
            sample: dict[str, Any] = {
                "page_number": page_index + 1,
                "type": subtype,
            }
            try:
                sample["rect"] = [round(float(v), 2) for v in annot.rect]
            # annot.rect may raise anything on damaged annotations; omitting the rect field means the evidence is absent.
            except Exception:
                pass
            try:
                info = dict(annot.info or {})
                sample["author"] = str(info.get("title") or "") or None
                content = str(info.get("content") or "").strip()
                sample["content"] = content[:_MAX_TEXT_CHARS] or None
            except Exception:
                sample["author"] = None
                sample["content"] = None
            # Association with the body text: what a reader sees under the
            # annotation's rectangle.  Factual clip, never inferred.
            try:
                associated = page.get_text("text", clip=annot.rect)
                sample["associated_body_text"] = (
                    associated.strip()[:_MAX_TEXT_CHARS] or None
                )
            except Exception:
                sample["associated_body_text"] = None
            samples.append(sample)

    return {
        "count": total,
        "by_type": by_type,
        "samples": samples,
        "truncated": (
            truncated_pages
            or total > _MAX_ANNOT_SAMPLES
        ),
        "note": (
            f"counts cover the first {pages_scanned} page(s); at most "
            f"{_MAX_ANNOT_SAMPLES} samples are kept"
        ),
    }


def _annotation_facts(document: fitz.Document) -> dict[str, Any]:
    return _scan_annotations(document)


def _redaction_facts(document: fitz.Document, annotations: dict[str, Any]) -> dict[str, Any]:
    """Distinguish redaction *marks* from actual content removal.

    A ``/Subtype /Redact`` annotation is only an intention; the factual
    test is whether body text still exists under its rectangle.
    Independently, *visual obscuration* — a drawn near-black rectangle
    over text with no Redact annotation at all — is recorded with its
    own semantics: the underlying text is fully recoverable by
    extraction, which is precisely what makes it dangerous to treat
    as a real redaction in legal/compliance settings.
    """
    count = int(annotations.get("by_type", {}).get(_REDACT_SUBTYPE, 0))
    facts: dict[str, Any] = {
        "count": count,
        "note": (
            "Redact annotations are marks, not removals; "
            "body_text_still_present tells whether the underlying "
            "content survived"
        ),
    }
    marks: list[dict[str, Any]] = []
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    for page_index in range(pages_scanned):
        if len(marks) >= _MAX_ANNOT_SAMPLES:
            break
        try:
            page = document[page_index]
            annots = list(page.annots())
        except Exception:
            continue
        for annot in annots:
            if len(marks) >= _MAX_ANNOT_SAMPLES:
                break
            try:
                if str(annot.type[1]) != _REDACT_SUBTYPE:
                    continue
            except Exception:
                continue
            try:
                remaining = page.get_text("text", clip=annot.rect).strip()
            except Exception:
                remaining = ""
            marks.append(
                {
                    "page_number": page_index + 1,
                    "rect": [round(float(v), 2) for v in annot.rect],
                    "body_text_still_present": bool(remaining),
                    # Whether text under a redaction mark is still extractable — same source and meaning
                    # as body_text_still_present, but with independent semantic naming: compliance consumers use this field
                    # to judge "visually redacted but not actually deleted".
                    "text_recoverable": bool(remaining),
                    "remaining_text": remaining[:_MAX_TEXT_CHARS] or None,
                }
            )
    if marks:
        facts["marks"] = marks

    facts["visual_obscuration"] = _visual_obscuration_facts(document)
    return facts


def _visual_obscuration_facts(document: fitz.Document) -> dict[str, Any]:
    """Drawn near-black rectangles hiding text without a Redact annotation.

    The test is factual, never inferred: a filled rectangle whose color
    is near-black, large enough to cover text, with body text still
    extractable underneath.  Decorative shapes and table-cell fills
    without text underneath never qualify.  The recorded semantic is
    the compliance-critical one — ``underlying_text_exists`` and
    ``text_recoverable`` are both True, so this is *not* a redaction.
    """
    samples: list[dict[str, Any]] = []
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    truncated = False

    for page_index in range(pages_scanned):
        if len(samples) >= _MAX_OBSCURATION_SAMPLES:
            # Sample cap hit before the page scan finished: further
            # obscuration rectangles (if any) are counted as truncated.
            truncated = True
            break
        try:
            page = document[page_index]
            drawings = page.get_drawings()
        except Exception:  # Evidence gathering is never fatal.
            continue
        for item in drawings:
            if len(samples) >= _MAX_OBSCURATION_SAMPLES:
                truncated = True
                break
            if str(item.get("type")) not in ("f", "fs"):
                continue
            fill = item.get("fill")
            if not fill or any(
                float(channel) > _NEAR_BLACK_CHANNEL_MAX for channel in fill
            ):
                continue
            try:
                rect = fitz.Rect(item["rect"])
            except Exception:
                continue
            if (
                rect.width < _MIN_OBSCURATION_SIDE
                or rect.height < _MIN_OBSCURATION_SIDE
                or abs(rect.width * rect.height) < _MIN_OBSCURATION_AREA
            ):
                continue
            try:
                underlying = page.get_text("text", clip=rect).strip()
            except Exception:
                underlying = ""
            if not underlying:
                # A black shape with no text underneath is decoration,
                # not obscuration.
                continue
            samples.append(
                {
                    "page_number": page_index + 1,
                    "rect": [
                        round(float(rect.x0), 2),
                        round(float(rect.y0), 2),
                        round(float(rect.x1), 2),
                        round(float(rect.y1), 2),
                    ],
                    "fill": [round(float(channel), 3) for channel in fill],
                    "underlying_text_exists": True,
                    "text_recoverable": True,
                    "recoverable_text": underlying[:_MAX_TEXT_CHARS] or None,
                    "note": (
                        "drawn rectangle hides text visually; the text is "
                        "still extractable and was NOT removed — this is "
                        "not a true redaction"
                    ),
                }
            )

    return {
        "count": len(samples),
        "truncated": truncated,
        "samples": samples,
        "note": (
            "near-black filled rectangles over recoverable text; recorded "
            "as compliance evidence, content is never rewritten"
        ),
    }


def _media_facts(document: fitz.Document, annotations: dict[str, Any]) -> dict[str, Any]:
    by_type = {
        subtype: count
        for subtype, count in annotations.get("by_type", {}).items()
        if subtype in _MEDIA_SUBTYPES
    }
    facts: dict[str, Any] = {
        "count": sum(by_type.values()),
        "by_type": by_type,
        "note": (
            "multimedia annotations are recorded as evidence only; "
            "audio/video/3D content is never rendered or executed"
        ),
    }
    if by_type:
        facts["locations"] = [
            {
                "page_number": sample["page_number"],
                "type": sample["type"],
                "rect": sample.get("rect"),
            }
            for sample in annotations.get("samples", [])
            if sample.get("type") in _MEDIA_SUBTYPES
        ]
    return facts


# ------------------------------------------------------------
# forms
# ------------------------------------------------------------

def _form_facts(document: fitz.Document) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "is_form_pdf": bool(document.is_form_pdf),
        "note": (
            "field values are recorded verbatim; calculation orders are "
            "counted and never evaluated"
        ),
    }
    xfa = _catalog_key(document, "AcroForm/XFA")
    facts["xfa_present"] = xfa is not None
    # <script> elements inside an XFA package are not /S /JavaScript action mount points;
    # the javascript facts don't cover them; structural presence is registered here (additive).
    facts["xfa_scripts"] = _xfa_script_facts(document, xfa)
    # Adversarial round 11: XFA <submit> is a form-data exfil channel (event-driven
    # direct target-URI output) — the same exfil family as the SubmitForm action /F, but
    # mounted inside the XFA package rather than an action dictionary; structural registration (additive).
    facts["xfa_submits"] = _xfa_submit_facts(document, xfa)
    # Adversarial round 12: the connectionSet endpoint channel (same exfil family, independent fact key).
    facts["xfa_connections"] = _xfa_connection_facts(document, xfa)

    calc = _catalog_key(document, "AcroForm/CO")
    if calc is None:
        facts["calculation_order_count"] = 0
    else:
        facts["calculation_order_count"] = len(_REF_RE.findall(calc))

    fields: list[dict[str, Any]] = []
    total = 0
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    for page_index in range(pages_scanned):
        try:
            widgets = list(document[page_index].widgets())
        except Exception:
            continue
        for widget in widgets:
            total += 1
            if len(fields) >= _MAX_FIELD_SAMPLES:
                continue
            field: dict[str, Any] = {"page_number": page_index + 1}
            for attr in ("field_name", "field_type_string", "field_label"):
                try:
                    value = getattr(widget, attr, None)
                except Exception:
                    value = None
                field[attr.replace("field_", "", 1)] = (
                    str(value) if value not in (None, "") else None
                )
            try:
                value = widget.field_value
            except Exception:
                value = None
            field["value"] = str(value) if value not in (None, "") else None
            # PyMuPDF 1.28.2 Widget exposes no default-value attribute; the
            # AcroForm /DV entry is the factual source.  (Evidence only —
            # no value is ever submitted back into the form.)
            try:
                kind, dv = document.xref_get_key(widget.xref, "DV")
            except Exception:
                kind, dv = "null", None
            field["default_value"] = (
                str(dv) if kind not in ("null", "unknown") and dv not in (None, "") else None
            )
            fields.append(field)

    facts["field_count"] = total
    facts["fields"] = fields
    facts["truncated"] = total > _MAX_FIELD_SAMPLES
    return facts


# ------------------------------------------------------------
# Structural registration of <script> elements inside XFA packages (adversarial round 3 finding: XFA streams
# can hide executable scripts while javascript.present stays False, misleading consumers into trusting
# the whole document has no JS. No script decoding, no XML semantics — element presence counted only)
# ------------------------------------------------------------

_XFA_SCRIPT_RE = re.compile(r"<script\b", re.IGNORECASE)

# Adversarial round 11: exfil-target literals of XFA <submit> elements (both target/url attribute forms
# have appeared in the spec; the first hit is recorded verbatim — over-record rather than guess).
_XFA_SUBMIT_RE = re.compile(r"<submit\b[^>]*>", re.IGNORECASE)
_XFA_SUBMIT_TARGET_RE = re.compile(
    r"(?:target|url)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_MAX_XFA_SUBMIT_TARGETS = 20

# Adversarial round 12: external endpoint literals of XFA connectionSet — both <uri> element text
# and the uri= attribute are forms that appeared in the spec, recorded verbatim without guessing; <connection element
# presence is counted separately (count stays non-zero when endpoints are declared without inlined literals).
_XFA_CONNECTION_RE = re.compile(r"<connection\b", re.IGNORECASE)
_XFA_URI_ELEM_RE = re.compile(
    r"<uri\b[^>]*>([^<]+)</uri>", re.IGNORECASE
)
_XFA_URI_ATTR_RE = re.compile(
    r"\buri\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_MAX_XFA_URIS = 20


def _xfa_script_facts(document: fitz.Document, xfa: str | None) -> dict[str, Any]:
    """Count <script> elements inside AcroForm/XFA stream packets.

    The XFA entry may be a single stream reference, an array of packet
    references, or a plain string; only reference forms carry streams
    worth scanning.  Follow depth is bounded and failures are skipped
    explicitly (no silent zeros beyond "nothing declared").
    """
    facts: dict[str, Any] = {"count": 0, "sources": []}
    if not xfa:
        return facts
    refs = _REF_RE.findall(xfa)[:_MAX_ACTION_REF_FOLLOWS]
    for ref in refs:
        xref = int(ref.split()[0])
        try:
            data = document.xref_stream(xref)
        except Exception:
            continue
        if not data:
            continue
        hits = len(_XFA_SCRIPT_RE.findall(data.decode("latin-1", "replace")))
        if hits:
            facts["count"] += hits
            facts["sources"].append(f"AcroForm/XFA@{xref}")
    return facts


def _xfa_submit_facts(document: fitz.Document, xfa: str | None) -> dict[str, Any]:
    """Count <submit> elements and their target literals in XFA packets.

    The <submit> element of an XFA form (event-driven) carries the form-data exfil target
    URI — an exfil channel of the same family as the action-dictionary /S /SubmitForm, but hidden
    inside the XFA package where neither the action census nor the JS audit reaches (adversarial round 11). No decoding of
    package-content semantics; element presence is counted and the target/url attribute literals are recorded verbatim;
    reference following is bounded in depth and failures skip explicitly (no silent zeros).
    """
    facts: dict[str, Any] = {"count": 0, "targets": [], "sources": []}
    if not xfa:
        return facts
    refs = _REF_RE.findall(xfa)[:_MAX_ACTION_REF_FOLLOWS]
    for ref in refs:
        xref = int(ref.split()[0])
        try:
            data = document.xref_stream(xref)
        except Exception:
            continue
        if not data:
            continue
        text = data.decode("latin-1", "replace")
        tags = _XFA_SUBMIT_RE.findall(text)
        if not tags:
            continue
        facts["count"] += len(tags)
        facts["sources"].append(f"AcroForm/XFA@{xref}")
        for tag in tags:
            match = _XFA_SUBMIT_TARGET_RE.search(tag)
            if match is None:
                continue
            if len(facts["targets"]) >= _MAX_XFA_SUBMIT_TARGETS:
                # Target samples are capped for disclosure (count records the truth).
                facts["truncated"] = True
                break
            facts["targets"].append(match.group(1))
    return facts


def _xfa_connection_facts(
    document: fitz.Document, xfa: str | None
) -> dict[str, Any]:
    """Count <connection> elements and endpoint literals in XFA packets.

    An XFA connectionSet declares connections between the form and external endpoints (<connection> elements'
    <uri> child text or uri= attribute carries the literal) — an endpoint channel for data import/export,
    hidden inside the XFA package where neither the action census nor the JS audit reaches
    (adversarial round 12). No decoding of package-content semantics; element presence is counted and endpoint
    literals are recorded verbatim; reference following is bounded in depth and failures skip explicitly (no silent zeros).
    """
    facts: dict[str, Any] = {"count": 0, "uris": [], "sources": []}
    if not xfa:
        return facts
    refs = _REF_RE.findall(xfa)[:_MAX_ACTION_REF_FOLLOWS]
    for ref in refs:
        xref = int(ref.split()[0])
        try:
            data = document.xref_stream(xref)
        except Exception:
            continue
        if not data:
            continue
        text = data.decode("latin-1", "replace")
        connections = len(_XFA_CONNECTION_RE.findall(text))
        endpoints = _XFA_URI_ELEM_RE.findall(text) + _XFA_URI_ATTR_RE.findall(
            text
        )
        if not connections and not endpoints:
            continue
        facts["count"] += connections
        facts["sources"].append(f"AcroForm/XFA@{xref}")
        for endpoint in endpoints:
            literal = endpoint.strip()
            if not literal:
                continue
            if len(facts["uris"]) >= _MAX_XFA_URIS:
                # Endpoint samples are capped for disclosure (count records the truth).
                facts["truncated"] = True
                break
            facts["uris"].append(literal)
    return facts


# ------------------------------------------------------------
# signatures
# ------------------------------------------------------------

def _signature_facts(document: fitz.Document, path: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "note": (
            "signature dictionaries are recorded as evidence; "
            "cryptographic validity is never verified"
        ),
    }
    sig_flags = _catalog_key(document, "AcroForm/SigFlags")
    facts["sig_flags"] = None if sig_flags is None else sig_flags.strip()

    signature_fields: list[dict[str, Any]] = []
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    for page_index in range(pages_scanned):
        try:
            widgets = list(document[page_index].widgets())
        except Exception:
            continue
        for widget in widgets:
            try:
                is_signature = str(widget.field_type_string) == "Signature"
            except Exception:
                continue
            if not is_signature:
                continue
            field: dict[str, Any] = {
                "page_number": page_index + 1,
                "name": getattr(widget, "field_name", None),
            }
            value_dict = None
            try:
                value_dict = document.xref_get_key(widget.xref, "V")
            # A failed widget V-key read is treated as no value (observation never blocks).
            except Exception:
                pass
            if value_dict is not None and value_dict[0] != "null":
                raw = value_dict[1]
                # Indirect-reference form (/V 12 0 R — the standard form of real signing tools):
                # xref_get_key only yields the 'N 0 R' reference string, which the ByteRange regex can never
                # scan — one hop must be followed to reach the signature dictionary body (same root
                # cause and fix as _action_mount_has_javascript in the JS section; a low-report fix point).
                indirect = _INDIRECT_REF_RE.match(raw.strip())
                if indirect:
                    raw = _object_body(document, int(indirect.group(1)))
                field["value_dict"] = raw[: _MAX_TEXT_CHARS * 4]
                numbers: list[int] | None = None
                match = _BYTE_RANGE_RE.search(raw)
                if match:
                    try:
                        numbers = [int(v) for v in match.group(1).split()]
                    except ValueError:
                        numbers = None
                else:
                    # The /ByteRange body is an indirect reference (adversarial round 3 deep-dive P22):
                    # follow one hop to get the array object body '[0 100 400 100]'.
                    indirect_br = _BYTE_RANGE_INDIRECT_RE.search(raw)
                    if indirect_br:
                        br_body = _object_body(
                            document, int(indirect_br.group(1).split()[0])
                        ).strip()
                        inner = br_body.lstrip("[").rstrip("]")
                        try:
                            numbers = [int(v) for v in inner.split()]
                        except ValueError:
                            numbers = None
                if numbers:
                    try:
                        # PDF 32000-1 §12.8.1: ByteRange pairs are
                        # (byte offset, length); coverage = Σ length.
                        # (The old code treated pairs as (start,end) and took differences — a real
                        # signature [0 5000 5600 100] would compute a negative number.)
                        byte_range = [
                            [numbers[i], numbers[i + 1]]
                            for i in range(0, len(numbers) - 1, 2)
                        ]
                    except (ValueError, IndexError):
                        byte_range = None
                    field["byte_range"] = byte_range
                    if byte_range:
                        covered = sum(
                            length for _offset, length in byte_range
                        )
                        try:
                            file_size = path.stat().st_size
                        except OSError:
                            file_size = None
                        field["covered_bytes"] = covered
                        if file_size:
                            field["coverage_fraction"] = round(
                                covered / file_size, 4
                            )
                        if file_size and covered < file_size:
                            field["note"] = (
                                "ByteRange covers only part of the file; "
                                "content outside the range was modified "
                                "after signing (or the signature is a "
                                "placeholder) — recorded, not judged"
                            )
            signature_fields.append(field)

    facts["signature_field_count"] = len(signature_fields)
    facts["signature_fields"] = signature_fields
    return facts


# ------------------------------------------------------------
# JavaScript actions
# ------------------------------------------------------------
# pdf_security_semantics_prereg_v1 §1 (D1) + §6.1 (Q1-A decision):
# Covers the indirect-reference form (xref_get_key returns only the reference string for an indirect
# OpenAction — the low-report root cause of "/JavaScript" in "6 0 R" always being False) and the OpenAction array
# form, plus the three mount buckets: page-object /AA (O/C/I), annotation /AA, widget-field /AA
# (K/V/F/C). Read-only xref tables throughout: the structure is counted, actions
# are never decoded, never executed.

def _object_body(document: fitz.Document, xref: int) -> str:
    """One indirect object's raw body; empty string on any failure."""
    try:
        return str(document.xref_object(xref))
    except Exception:
        return ""


def _action_mount_has_javascript(
    document: fitz.Document, raw: str | None
) -> bool:
    """Structural test: does this action mount carry /S /JavaScript?

    Handles direct dictionaries, indirect references (one hop), and
    array forms whose elements may themselves be indirect.  Only the
    xref table is consulted — no stream is decoded, nothing is
    executed, and the follow depth is bounded.
    """
    if not raw:
        return False
    text = raw.strip()
    match = _INDIRECT_REF_RE.match(text)
    if match:
        # Indirect reference: xref_get_key yields only the 'N 0 R' reference string; one hop must be followed
        # to reach the action dictionary body (low-report root-cause fix point).
        text = _object_body(document, int(match.group(1)))
    if _JS_SUBTYPE_RE.search(text):
        return True
    # Array form: inline dictionaries were covered by the direct scan above; elements that are still reference strings are
    # each followed (bounded); indirect sub-actions inside /AA mount points take the same path.
    for ref in _REF_RE.findall(text)[:_MAX_ACTION_REF_FOLLOWS]:
        body = _object_body(document, int(ref.split()[0]))
        if _JS_SUBTYPE_RE.search(body):
            return True
    return False


def _xref_key_string(document: fitz.Document, xref: int, key: str) -> str | None:
    """Raw string value of one object key; None when absent/null."""
    try:
        kind, value = document.xref_get_key(xref, key)
    except Exception:
        return None
    if kind in ("null", "unknown") or value in (None, ""):
        return None
    return str(value)


def _xref_aa_has_javascript(document: fitz.Document, xref: int) -> bool:
    """Does one object's /AA additional-action mount carry JavaScript?"""
    return _action_mount_has_javascript(
        document, _xref_key_string(document, xref, "AA")
    )


def _page_annot_refs(document: fitz.Document, page: fitz.Page) -> list[str]:
    """Direct /Annots reference list of one page (xref-table read).

    pymupdf's ``page.annots()`` does not return Link annotations and ``page.widgets()``
    only sees AcroForm fields — both high-level channels structurally filter the mount buckets.
    A direct audit of the page dictionary's /Annots array covers every annotation-type object, no omission no double-counting (including
    Link and Widget).
    """
    raw = _xref_key_string(document, page.xref, "Annots")
    if raw is None:
        return []
    match = _INDIRECT_REF_RE.match(raw.strip())
    if match:
        raw = _object_body(document, int(match.group(1)))
    return _REF_RE.findall(raw)


def _aa_bucket_scan(document: fitz.Document) -> dict[str, Any]:
    """Bounded pass over page/annotation/widget /AA mounts.

    The PDF spec allows three object classes to carry /AA additional-action dictionaries (pages: O/C/I;
    annotations and widgets: K/V/F/C etc.). Each object is checked for /AA and given the same structural audit,
    counting only mount points where a JS sub-action is found, reusing the page-scan cap and sample discipline.
    Annotations and widgets also carry the /A direct action key (the standard carrier position for Link annotations);
    adversarial round 7 addition: same structural audit, an independent *_action bucket prevents mixing with /AA counts.
    Adversarial round 12 addition: RichMedia annotations' activation actions nest inside the /Activation
    dictionary's /A key (neither the top-level /A nor /AA reaches them) — the whole dictionary text goes through the same
    structural audit (only /A carries an action inside the dictionary, /Condition is a name), an independent
    *_activation bucket prevents mixed counts.
    """
    counts = {
        "page": 0,
        "annotation": 0,
        "widget": 0,
        "annotation_action": 0,
        "widget_action": 0,
        "annotation_activation": 0,
        "widget_activation": 0,
    }
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    for page_index in range(pages_scanned):
        try:
            page = document[page_index]
        except Exception:
            continue
        if _xref_aa_has_javascript(document, page.xref):
            counts["page"] += 1
        try:
            refs = _page_annot_refs(document, page)
        except Exception:
            refs = []
        for ref in refs:
            annot_xref = int(ref.split()[0])
            subtype = _xref_key_string(document, annot_xref, "Subtype") or ""
            is_widget = "/Widget" in subtype
            if _xref_aa_has_javascript(document, annot_xref):
                if is_widget:
                    counts["widget"] += 1
                else:
                    counts["annotation"] += 1
            if _action_mount_has_javascript(
                document, _xref_key_string(document, annot_xref, "A")
            ):
                if is_widget:
                    counts["widget_action"] += 1
                else:
                    counts["annotation_action"] += 1
            if _action_mount_has_javascript(
                document, _xref_key_string(document, annot_xref, "Activation")
            ):
                if is_widget:
                    counts["widget_activation"] += 1
                else:
                    counts["annotation_activation"] += 1
    return {
        "counts": counts,
        "truncated": int(document.page_count) > pages_scanned,
    }


def _mount_non_js_actions(
    document: fitz.Document, raw: str | None
) -> list[tuple[str, str | None]]:
    """(subtype, target) pairs for non-JS /S actions under one mount.

    Same structural discipline as ``_action_mount_has_javascript``:
    the indirect reference is followed one hop, array /Next elements
    are bounded, and only the xref table is consulted.  The target
    literal is searched in a small window after the /S hit so two
    actions sharing one mount body cannot misattribute each other's
    /F or /URI value.
    """
    found: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    bodies: list[str] = []
    if raw:
        text = raw.strip()
        match = _INDIRECT_REF_RE.match(text)
        if match:
            text = _object_body(document, int(match.group(1)))
        bodies.append(text)
        for ref in _REF_RE.findall(text)[:_MAX_ACTION_REF_FOLLOWS]:
            body = _object_body(document, int(ref.split()[0]))
            if body:
                bodies.append(body)
    for body in bodies:
        for hit in _ACTION_SUBTYPE_RE.finditer(body):
            subtype = hit.group(1)
            if subtype == "JavaScript":
                # Already covered by the javascript fact section; not counted twice.
                continue
            window = body[hit.end() : hit.end() + _ACTION_TARGET_WINDOW]
            target_match = _ACTION_TARGET_LITERAL_RE.search(window)
            target = target_match.group(1) if target_match else None
            pair = (subtype, target)
            if pair not in seen:
                seen.add(pair)
                found.append(pair)
    return found


def _document_action_facts(document: fitz.Document) -> dict[str, Any]:
    """Census of non-JavaScript action subtypes across action mounts.

    Mounts mirror the JavaScript audit (catalog /OpenAction plus the
    page/annotation/widget /AA buckets); the /S subtype of every other
    action family is counted with a bounded sample trail.  Recorded as
    evidence only — nothing is launched, submitted, or fetched.
    Adversarial round 12 addition: annotation /Activation /A activation-action nested mounting (the
    standard RichMedia carrier position, previously missed by an entire layer).
    """
    counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    total = 0

    def _record(mount: str, raw: str | None) -> None:
        nonlocal total
        for subtype, target in _mount_non_js_actions(document, raw):
            counts[subtype] = counts.get(subtype, 0) + 1
            total += 1
            if len(samples) < _MAX_ACTION_SAMPLES:
                sample: dict[str, Any] = {"mount": mount, "kind": subtype}
                if target is not None:
                    sample["target"] = target
                samples.append(sample)

    open_action = _catalog_key(document, "OpenAction")
    if open_action is not None:
        _record("catalog/OpenAction", open_action)

    # Adversarial round 7 addition: two document-level mount points isomorphic to the JS audit
    # (catalog /AA and AcroForm /AA).
    catalog_aa = _catalog_key(document, "AA")
    if catalog_aa is not None:
        _record("catalog/AA", catalog_aa)

    acroform_aa = _catalog_key(document, "AcroForm/AA")
    if acroform_aa is not None:
        _record("AcroForm/AA", acroform_aa)

    # Adversarial round 8 addition: outline-entry /A (bookmark actions; bounded walk of the First/Next chain)
    # — a mount bucket isomorphic to the JS audit.
    for item_xref in _outline_item_xrefs(document):
        raw_a = _xref_key_string(document, item_xref, "A")
        if raw_a is not None:
            _record("outline/A", raw_a)

    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    for page_index in range(pages_scanned):
        try:
            page = document[page_index]
        except Exception:
            continue
        _record("page/AA", _xref_key_string(document, page.xref, "AA"))
        try:
            refs = _page_annot_refs(document, page)
        except Exception:
            refs = []
        for ref in refs:
            annot_xref = int(ref.split()[0])
            subtype = _xref_key_string(document, annot_xref, "Subtype") or ""
            is_widget = "/Widget" in subtype
            raw_aa = _xref_key_string(document, annot_xref, "AA")
            if raw_aa is not None:
                _record(
                    "widget/AA" if is_widget else "annotation/AA", raw_aa
                )
            # Adversarial round 7 addition: the /A direct action key (the standard Link-annotation carrier position)
            # is registered in an independent mount bucket, preventing mixing with /AA.
            raw_a = _xref_key_string(document, annot_xref, "A")
            if raw_a is not None:
                _record(
                    "widget/A" if is_widget else "annotation/A", raw_a
                )
            # Adversarial round 12 addition: RichMedia activation-action nested mounting
            # /Activation /A (the whole dictionary text goes through the same structural audit; inside the dictionary
            # only /A carries an action, /Condition is a name).
            raw_activation = _xref_key_string(document, annot_xref, "Activation")
            if raw_activation is not None:
                _record(
                    "widget/Activation/A"
                    if is_widget
                    else "annotation/Activation/A",
                    raw_activation,
                )

    facts: dict[str, Any] = {
        "present": total > 0,
        "counts": counts,
        "samples": samples,
        "sample_truncated": total > len(samples),
        "note": (
            "non-JavaScript action subtypes (Launch, SubmitForm, GoToR, "
            "URI, ...) are recorded as evidence only; nothing is launched, "
            "submitted, or fetched"
        ),
    }
    if int(document.page_count) > pages_scanned:
        # The same page-cap truncation-disclosure discipline as the /AA bucket scan.
        facts["truncated"] = True
        facts["truncated_note"] = (
            f"action mount scan covered the first {pages_scanned} page(s) only"
        )
    return facts


def _javascript_facts(document: fitz.Document) -> dict[str, Any]:
    sources: list[str] = []
    counts: dict[str, int] = {}

    open_action = _catalog_key(document, "OpenAction")
    if open_action is not None and _action_mount_has_javascript(
        document, open_action
    ):
        sources.append("catalog/OpenAction")
        counts["catalog/OpenAction"] = 1

    # Adversarial round 7 addition: catalog /AA (document-level additional actions, /WC /DS /WS etc.)
    # and AcroForm /AA (form-level F/K/V events) — two document-level mount points.
    catalog_aa = _catalog_key(document, "AA")
    if catalog_aa is not None and _action_mount_has_javascript(
        document, catalog_aa
    ):
        sources.append("catalog/AA")
        counts["catalog/AA"] = 1

    acroform_aa = _catalog_key(document, "AcroForm/AA")
    if acroform_aa is not None and _action_mount_has_javascript(
        document, acroform_aa
    ):
        sources.append("AcroForm/AA")
        counts["AcroForm/AA"] = 1

    # Adversarial round 8 addition: outline-entry /A (bookmark actions, First/Next chains).
    outline_js = 0
    for item_xref in _outline_item_xrefs(document):
        if _action_mount_has_javascript(
            document, _xref_key_string(document, item_xref, "A")
        ):
            outline_js += 1
    if outline_js:
        sources.append("outline/A")
        counts["outline/A"] = outline_js

    names_js = _catalog_key(document, "Names/JavaScript")
    if names_js is not None:
        sources.append("catalog/Names/JavaScript")
        counts["catalog/Names/JavaScript"] = 1

    aa = _aa_bucket_scan(document)
    for bucket in ("page", "annotation", "widget"):
        if aa["counts"][bucket]:
            sources.append(f"{bucket}/AA")
            counts[f"{bucket}/AA"] = aa["counts"][bucket]
    for bucket in ("annotation", "widget"):
        if aa["counts"][f"{bucket}_action"]:
            sources.append(f"{bucket}/A")
            counts[f"{bucket}/A"] = aa["counts"][f"{bucket}_action"]
    # Adversarial round 12: RichMedia activation-action mounting /Activation /A (independent bucket).
    for bucket in ("annotation", "widget"):
        if aa["counts"][f"{bucket}_activation"]:
            sources.append(f"{bucket}/Activation/A")
            counts[f"{bucket}/Activation/A"] = aa["counts"][
                f"{bucket}_activation"
            ]

    facts: dict[str, Any] = {
        "present": bool(sources),
        "sources": sources,
        "counts": counts,
        "note": (
            "JavaScript actions are recorded as evidence only and are "
            "never executed"
        ),
    }
    if aa["truncated"]:
        # The /AA bucket scan explicitly discloses truncation by the page cap (the same truncation
        # discipline as the annotations section).
        facts["truncated"] = True
        facts["truncated_note"] = (
            f"/AA mount scan covered the first "
            f"{min(int(document.page_count), _MAX_PAGES_SCANNED)} page(s) only"
        )
    return facts


# ------------------------------------------------------------
# invisible text (content-stream audit)
# ------------------------------------------------------------
# pdf_security_semantics_prereg_v1 §2 (D2): the extraction layer (stext device) is unreliable for
# visibility semantics — four-form drops, Tr3 position-dependent three-state, visible text squeezable
# out by hidden text. This section bypasses the extraction layer and audits the content stream directly: after literal
# stripping it tracks, operator by operator, rendering mode (Tr), font size (Tf), fill color (rg/RG) and text origin
# (Td/TD/Tm state machine), classifying and registering at text show events (Tj/TJ/'/").
# Disclosure only: no rendering, no extraction-layer changes.

def _media_box(document: fitz.Document, page: fitz.Page) -> tuple[float, float, float, float]:
    """Page MediaBox in PDF user space; MuPDF fallback, letter default."""
    raw = _xref_key_string(document, page.xref, "MediaBox")
    if raw:
        numbers = _NUM_RE.findall(raw)
        if len(numbers) >= 4:
            return tuple(float(v) for v in numbers[:4])  # type: ignore[return-value]
    try:
        box = page.mediabox
        return (box.x0, box.y0, box.x1, box.y1)
    except Exception:
        return (0.0, 0.0, 612.0, 792.0)


def _word_positions(raw: bytes, word: bytes) -> list[int]:
    """All \b-bounded word occurrence positions in a content stream.

    ``bytes.find`` runs at C speed (memmem); the word-boundary check
    replaces the regex alternation whose per-position backtracking
    made pathological graphics-heavy pages crawl.
    """
    positions: list[int] = []
    start = 0
    length = len(raw)
    while True:
        idx = raw.find(word, start)
        if idx < 0:
            return positions
        before_ok = idx == 0 or raw[idx - 1] not in _BLOCK_WORD_BYTES
        after = idx + len(word)
        after_ok = after >= length or raw[after] not in _BLOCK_WORD_BYTES
        if before_ok and after_ok:
            positions.append(idx)
        start = idx + 1


def _text_block_spans(raw: bytes) -> list[tuple[int, int]]:
    """BT..ET byte spans in one content stream (unclosed BT to end).

    Semantics: BT opens a block (nested BT keeps the outer block open,
    matching the reset-at-BT state machine), ET closes it.  Everything
    outside the spans is graphics-only for this audit — the state
    machine resets fully at BT and registers nothing outside text.
    """
    bt_positions = _word_positions(raw, b"BT")
    et_positions = _word_positions(raw, b"ET")
    spans: list[tuple[int, int]] = []
    start: int | None = None
    bt_index = et_index = 0
    while et_index < len(et_positions):
        if bt_index < len(bt_positions) and (
            bt_positions[bt_index] < et_positions[et_index]
        ):
            if start is None:
                start = bt_positions[bt_index]
            bt_index += 1
        else:
            if start is not None:
                spans.append((start, et_positions[et_index] + 2))
                start = None
            et_index += 1
    if start is not None:
        spans.append((start, len(raw)))
    return spans


def _invisible_text_facts(document: fitz.Document) -> dict[str, Any]:
    forms: dict[str, dict[str, Any]] = {
        name: {"count": 0, "samples": []} for name in _INVISIBLE_FORMS
    }
    text_events = 0
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    truncated = int(document.page_count) > pages_scanned

    for page_index in range(pages_scanned):
        try:
            page = document[page_index]
            raw = page.read_contents()
        except Exception:  # Evidence gathering is never fatal.
            continue
        if not raw:
            continue
        media_box = _media_box(document, page)
        # Page-boundary precomputation for the show fast path, avoiding repeated indexing and
        # subtraction per event; sample dicts use pre-rounded values instead of re-rounding
        # media_box per hit (cProfile once measured round at 445k calls on one file).
        mb_min_x = media_box[0] - _MEDIA_BOX_EPS
        mb_min_y = media_box[1] - _MEDIA_BOX_EPS
        mb_max_x = media_box[2] + _MEDIA_BOX_EPS
        mb_max_y = media_box[3] + _MEDIA_BOX_EPS
        mb_rounded = [round(v, 2) for v in media_box]

        for bt_start, et_end in _text_block_spans(raw):
            block = raw[bt_start:et_end]
            stream = _STRING_LITERAL_RE.sub(
                "STR ", block.decode("latin-1", "replace")
            )

            render_mode = 0
            font_size: float | None = None
            white_fill = False
            origin_x = 0.0
            origin_y = 0.0
            in_text = False
            sampled: set[str] = set()

            for match in _OPS_TOKEN_RE.finditer(stream):
                token = match.group(1)
                if token is None or token in ("Tj", "TJ"):
                    # Text show events: Tj/TJ, plus ' and " (no word boundary,
                    # consistent with the original show branches). Fast path: when none of the four forms
                    # hit, skip with zero allocation (corpus show events number in the millions; the constant
                    # overhead was once the second-biggest cost after the op scan).
                    if not in_text:
                        continue
                    text_events += 1
                    if not (
                        render_mode == 3
                        or (white_fill and render_mode == 0)
                        or (
                            font_size is not None
                            and 0 < font_size < _TINY_FONT_SIZE_MAX
                        )
                        or origin_x < mb_min_x
                        or origin_y < mb_min_y
                        or origin_x > mb_max_x
                        or origin_y > mb_max_y
                    ):
                        continue
                    outside = (
                        origin_x < mb_min_x
                        or origin_y < mb_min_y
                        or origin_x > mb_max_x
                        or origin_y > mb_max_y
                    )
                    # Hit registration: record the name first; sample-dict construction is deferred until inside the truncation window
                    # (off_page-heavy files once built a dict per hit
                    # event and re-rounded media_box each time).
                    hits: list[str] = []
                    if render_mode == 3:
                        hits.append("render_mode_3")
                    if white_fill and render_mode == 0:
                        hits.append("white_on_white")
                    if font_size is not None and 0 < font_size < _TINY_FONT_SIZE_MAX:
                        hits.append("tiny_font")
                    if outside:
                        hits.append("off_page")
                    for name in hits:
                        forms[name]["count"] += 1
                        if name not in sampled and len(forms[name]["samples"]) < _INVISIBLE_SAMPLE_CAP:
                            sample: dict[str, Any] = {"page_number": page_index + 1}
                            if name == "render_mode_3":
                                sample["render_mode"] = 3
                            elif name == "white_on_white":
                                sample["fill"] = [1.0, 1.0, 1.0]
                            elif name == "tiny_font":
                                sample["font_size"] = font_size
                            else:
                                sample["origin"] = [
                                    round(origin_x, 2),
                                    round(origin_y, 2),
                                ]
                                sample["media_box"] = mb_rounded
                            forms[name]["samples"].append(sample)
                            sampled.add(name)
                    continue

                pos = match.start()
                if token == "BT":
                    in_text = True
                    render_mode = 0
                    font_size = None
                    white_fill = False
                    origin_x = origin_y = 0.0
                    sampled = set()
                elif token == "ET":
                    in_text = False
                elif token == "Tr":
                    nums = _tail_numbers(
                        stream[max(0, pos - 48):pos], 1
                    )
                    if nums is not None:
                        try:
                            render_mode = int(float(nums[0]))
                        except ValueError:
                            pass
                elif token == "Tf":
                    nums = _tail_numbers(
                        stream[max(0, pos - 48):pos], 1
                    )
                    if nums is not None:
                        try:
                            font_size = float(nums[0])
                        except ValueError:
                            pass
                elif token == "rg" or token == "RG":
                    nums = _tail_numbers(
                        stream[max(0, pos - 128):pos], 3
                    )
                    if nums is not None:
                        # white (1 1 1 rg/RG) takes priority over color (any three
                        # operands); branch priority equals: white only when all three
                        # numeric-string literals are "1", any other three operands clear whiteness. With fewer than
                        # three operands (illegal streams) neither branch hits and state is unchanged.
                        white_fill = nums == ["1", "1", "1"]
                elif token == "g":
                    # whitegray (1 g) / gray (any numeric g) merged semantics: judge whiteness from the taken-tail
                    # value, anything else resets — avoiding leftover
                    # white_fill false positives across color switches; no hit without operands.
                    nums = _tail_numbers(
                        stream[max(0, pos - 48):pos], 1
                    )
                    if nums is not None:
                        try:
                            white_fill = abs(float(nums[0]) - 1.0) < 1e-6
                        except ValueError:
                            white_fill = False
                elif token == "Tm":
                    nums = _tail_numbers(
                        stream[max(0, pos - 256):pos], 6
                    )
                    if nums is not None:
                        try:
                            origin_x = float(nums[4])
                            origin_y = float(nums[5])
                        except ValueError:
                            pass
                else:
                    # Td/TD: relative displacement within the line.
                    nums = _tail_numbers(
                        stream[max(0, pos - 96):pos], 2
                    )
                    if nums is not None:
                        try:
                            origin_x += float(nums[0])
                            origin_y += float(nums[1])
                        except ValueError:
                            pass

    facts: dict[str, Any] = {
        "present": any(f["count"] for f in forms.values()),
        "forms": forms,
        "text_show_events": text_events,
        "note": (
            "content-stream audit of visibility semantics; recorded as "
            "disclosure only — nothing is rendered, extraction behavior "
            "is never modified"
        ),
    }
    if truncated:
        facts["truncated"] = True
        facts["truncated_note"] = (
            f"invisible-text scan covered the first {pages_scanned} page(s) only"
        )
    return facts


# ------------------------------------------------------------
# ToUnicode CMap audit
# ------------------------------------------------------------
# pdf_security_semantics_prereg_v1 §3 (D3): structural-audit contradiction detects lies —
# a base14 identity-range code mapped by ToUnicode to another character = possible divergence between the rendering layer
# and the extraction layer. Read-only comparison, samples only; subset/CID/custom-encoding fonts have no
# ground truth and are registered as unauditable rather than guessed.

def _tounicode_pairs(cmap_text: str):
    """Yield (src_code, dst_codepoint) pairs from a ToUnicode CMap.

    Only single-codepoint destinations (4-hex-digit values) are
    yielded; multi-character destinations carry no identity-audit
    meaning and are skipped conservatively.
    """
    for block in _BFCHAR_BLOCK_RE.finditer(cmap_text):
        for src, dst in _BFCHAR_ENTRY_RE.findall(block.group(1)):
            # The bfchar capture group excludes the angle brackets: exactly 4 hex digits means a single code point (the old
            # implementation wrongly did dst[1:-1] with brackets included, silently swallowing single-code-point contradictions wholesale).
            if len(dst) == 4:
                yield int(src, 16), int(dst, 16)
    for block in _BFRANGE_BLOCK_RE.finditer(cmap_text):
        body = block.group(1)
        pos = 0
        while True:
            entry = _BFRANGE_ENTRY_RE.search(body, pos)
            if not entry:
                break
            lo = int(entry.group(1), 16)
            hi = int(entry.group(2), 16)
            dst = entry.group(3)
            if dst == "[":
                # array form: <lo> <hi> [ <d0> <d1> ... ] — one dst per code
                array_start = entry.end()
                array_end = body.find("]", array_start)
                if array_end == -1:
                    array_end = len(body)
                items = _BFRANGE_ARRAY_ITEM_RE.findall(
                    body[array_start:array_end]
                )
                for offset, item in enumerate(items):
                    src = lo + offset
                    if src > hi or len(item) != 4:
                        continue
                    yield src, int(item, 16)
                # Skip the whole array range: array entries have the same shape as the bfrange header
                # (<hh> <hh> <hh>); not advancing would re-parse them into ghost entries.
                pos = array_end + 1
                continue
            if dst.startswith("<") and dst.endswith(">") and len(dst) == 6:
                value = dst[1:-1]
                # single-dst form: consecutive codes increment the dst
                span = min(hi - lo + 1, 256)
                for offset in range(span):
                    yield lo + offset, int(value, 16) + offset
            pos = entry.end()


def _cmap_audit_facts(document: fitz.Document) -> dict[str, Any]:
    fonts_examined = 0
    audited = 0
    unauditable = 0
    mismatch_total = 0
    mismatches: list[dict[str, Any]] = []
    seen_font_xrefs: set[int] = set()
    pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
    truncated = int(document.page_count) > pages_scanned

    for page_index in range(pages_scanned):
        try:
            fonts = document.get_page_fonts(page_index, full=False)
        except Exception:
            continue
        for font in fonts:
            font_xref = int(font[0])
            if font_xref in seen_font_xrefs:
                continue
            seen_font_xrefs.add(font_xref)
            fonts_examined += 1
            basefont = str(font[3] or "")
            subtype = _xref_key_string(document, font_xref, "Subtype") or ""
            # Embedded glyph programs (FontFile*) and /Differences encodings change the ground
            # truth — no identity core to compare against; unauditable.
            embedded = any(
                _xref_key_string(document, font_xref, key) is not None
                for key in ("FontFile", "FontFile2", "FontFile3")
            )
            encoding = _xref_key_string(document, font_xref, "Encoding") or ""
            differences = (
                _xref_key_string(document, font_xref, "Differences") is not None
            )
            if not differences and encoding:
                indirect = _INDIRECT_REF_RE.match(encoding.strip())
                if indirect:
                    # An indirect encoding dictionary (adversarial round 3 deep-dive P21: judging unauditable
                    # directly would let hidden-character audits be entirely exempted by one level of indirection — same root cause as /V, /AA,
                    # OCProperties): a /Differences inside the body is truly unauditable;
                    # a /BaseEncoding that is a known identity encoding equals the direct form.
                    enc_body = _object_body(document, int(indirect.group(1)))
                    if "/Differences" in enc_body:
                        differences = True
                    else:
                        matched = [
                            name
                            for name in _KNOWN_BASE14_ENCODINGS
                            if name in enc_body
                        ]
                        encoding = matched[0] if matched else "unknown-indirect"
            if (
                "/Type1" not in subtype
                or basefont not in _BASE14_IDENTITY_FONTS
                or embedded
                or differences
                or (encoding and encoding not in _KNOWN_BASE14_ENCODINGS)
            ):
                unauditable += 1
                continue
            tounicode = _xref_key_string(document, font_xref, "ToUnicode")
            if tounicode is None:
                # No ToUnicode: the extraction layer falls back to a known encoding; nothing to hide.
                continue
            match = _INDIRECT_REF_RE.match(tounicode.strip())
            if not match:
                continue  # non-standard form; don't guess
            try:
                cmap_text = document.xref_stream(int(match.group(1))).decode(
                    "latin-1", "replace"
                )
            except Exception:
                continue
            audited += 1
            for src, dst in _tounicode_pairs(cmap_text):
                if src in _IDENTITY_CODES and dst != src:
                    # Counts keep accumulating truthfully; past the sample cap, count only without appending samples (same cap discipline).
                    mismatch_total += 1
                    if len(mismatches) < _MAX_CMAP_MISMATCH_SAMPLES:
                        mismatches.append(
                            {
                                "page_number": page_index + 1,
                                "font": basefont,
                                "code": f"0x{src:04X}",
                                "mapped_to": f"U+{dst:04X}",
                                "expected": f"U+{src:04X}",
                            }
                        )

    facts: dict[str, Any] = {
        "fonts_examined": fonts_examined,
        "audited_with_tounicode": audited,
        "unauditable_fonts": unauditable,
        "mismatch_count": mismatch_total,
        "cmap_mismatch": mismatches,
        "note": (
            "structural contradiction audit of ToUnicode mappings against "
            "base14 encoding identity; unauditable fonts (subsets, CID, "
            "custom encodings) are recorded, never guessed"
        ),
    }
    if truncated:
        facts["truncated"] = True
        facts["truncated_note"] = (
            f"cmap audit covered fonts of the first {pages_scanned} page(s) only"
        )
    return facts


# ------------------------------------------------------------
# Warning fact-code surfacing (pdf_security_semantics_prereg_v1 §4/§6.4,
# Q3-A/Q4-A decisions)
# ------------------------------------------------------------
# Same format as the G5 carrier: ``pdf_security:<signal>:<count>`` — one code per fact,
# document-level, count-placeholdered, aggregated after tiering normalization. Deep facts stay in the
# pdf_security evidence section; warnings are only the visibility channel for the compliance consumer side.

_SECURITY_SIGNAL_ORDER = (
    "javascript_present",
    "invisible_text",
    "redaction_text_recoverable",
    "visual_obscuration",
    "partial_byte_range",
    "cmap_mismatch",
    "xfa_script_present",
    "launch_action_present",
    "submitform_action_present",
    "remote_goto_action_present",
)


def build_security_warnings(evidence: dict[str, Any]) -> list[str]:
    """Render document-level compliance signal fact codes.

    Ten signals: JS action presence, invisible text presence,
    redaction marks with text still recoverable, drawn visual
    obscuration, partial signature ByteRange, ToUnicode CMap
    contradictions, <script> elements embedded in XFA packets
    (adversarial round 3: those are not /AA action mounts, so the JS
    signal alone would read as "no JavaScript anywhere"), and the
    three weaponized-document action families Launch / SubmitForm /
    GoToR (adversarial round 6: OS-command launch, form-data
    exfiltration, remote-document reference).
    Locked/unavailable sections contribute nothing — absence of
    disclosure there is already explicit in the evidence.
    """
    if not isinstance(evidence, dict) or not evidence.get("available"):
        return []

    counts: dict[str, int] = {}

    javascript = evidence.get("javascript")
    if isinstance(javascript, dict):
        mounts = sum(
            int(v) for v in (javascript.get("counts") or {}).values()
        ) or int(bool(javascript.get("sources")))
        if mounts:
            counts["javascript_present"] = mounts

    invisible = evidence.get("invisible_text")
    if isinstance(invisible, dict) and invisible.get("present"):
        events = sum(
            int(form.get("count") or 0)
            for form in (invisible.get("forms") or {}).values()
        )
        if events:
            counts["invisible_text"] = events

    redactions = evidence.get("redactions")
    if isinstance(redactions, dict):
        recoverable = sum(
            1
            for mark in redactions.get("marks") or []
            if mark.get("body_text_still_present") or mark.get("text_recoverable")
        )
        if recoverable:
            counts["redaction_text_recoverable"] = recoverable
        obscuration = redactions.get("visual_obscuration") or {}
        if isinstance(obscuration, dict) and obscuration.get("count"):
            counts["visual_obscuration"] = int(obscuration["count"])

    signatures = evidence.get("signatures")
    if isinstance(signatures, dict):
        partial = sum(
            1
            for field in signatures.get("signature_fields") or []
            if field.get("coverage_fraction") is not None
            and float(field["coverage_fraction"]) < 1.0
        )
        if partial:
            counts["partial_byte_range"] = partial

    cmap = evidence.get("cmap_audit")
    if isinstance(cmap, dict) and cmap.get("mismatch_count"):
        counts["cmap_mismatch"] = int(cmap["mismatch_count"])

    forms = evidence.get("forms")
    if isinstance(forms, dict):
        xfa_scripts = forms.get("xfa_scripts")
        if isinstance(xfa_scripts, dict) and xfa_scripts.get("count"):
            counts["xfa_script_present"] = int(xfa_scripts["count"])

    actions = evidence.get("document_actions")
    if isinstance(actions, dict):
        by_subtype = actions.get("counts")
        if isinstance(by_subtype, dict):
            launch = int(by_subtype.get("Launch") or 0)
            if launch:
                counts["launch_action_present"] = launch
            submit = int(by_subtype.get("SubmitForm") or 0)
            if submit:
                counts["submitform_action_present"] = submit
            goto = int(by_subtype.get("GoToR") or 0)
            if goto:
                counts["remote_goto_action_present"] = goto

    return [
        f"pdf_security:{signal}:{counts[signal]}"
        for signal in _SECURITY_SIGNAL_ORDER
        if counts.get(signal)
    ]


# ------------------------------------------------------------
# embedded files & portfolio
# ------------------------------------------------------------

def _embedded_file_facts(document: fitz.Document) -> dict[str, Any]:
    facts: dict[str, Any] = {"portfolio": False}
    try:
        count = int(document.embfile_count())
    except Exception:
        count = 0
    facts["count"] = count
    facts["portfolio"] = _catalog_key(document, "Collection") is not None
    if count == 0:
        # Adversarial round 9: the MuPDF embfile API returns 0 for balanced name trees (/Kids + /Limits
        # nodes) — a raw-xref manual walk of Names/EmbeddedFiles recovers the entries.
        _recover_kids_tree_entries(document, facts)
        return facts

    files: list[dict[str, Any]] = []
    try:
        names = list(document.embfile_names())
    except Exception:
        names = []
    for index in range(min(count, _MAX_EMBEDDED_SAMPLES)):
        entry: dict[str, Any] = {"name": names[index] if index < len(names) else None}
        try:
            info = dict(document.embfile_info(index) or {})
            entry["filename"] = info.get("filename")
            entry["description"] = info.get("description")
            entry["size"] = int(info.get("size") or 0)
        # embfile_info may raise anything on damaged attachments; missing fields stay empty, hashes are patched by later steps.
        except Exception:
            pass
        entry.update(_embedded_hash_and_mime(document, index, entry))
        files.append(entry)
    facts["files"] = files
    facts["truncated"] = count > _MAX_EMBEDDED_SAMPLES
    return facts


# Adversarial round 9: name-tree manual-walk constants — node-count cap (bounded + cycle-safe),
# raw-byte form matching of (name) reference pairs.
_MAX_NAME_TREE_NODES = 100
_NAME_TREE_PAIR_RE = re.compile(r"\(([^()\r\n]{0,120})\)\s+(\d+)\s+\d+\s+R")


def _name_tree_walk(
    document: fitz.Document, root_xref: int
) -> list[tuple[str, int]]:
    """(name, filespec_xref) pairs from a PDF name tree, /Kids included.

    The MuPDF embfile API covers only the flat /Names array form; balanced-tree nodes
    (/Kids + /Limits) are invisible at the API layer. Here we read the xref table directly and walk manually:
    take the (name) reference pairs of the node body's /Names array first, then recurse along /Kids references.
    Bounded (_MAX_NAME_TREE_NODES) + cycle-safe (a seen set).
    """
    pairs: list[tuple[str, int]] = []
    seen: set[int] = set()
    stack: list[int] = [root_xref]
    while stack and len(seen) < _MAX_NAME_TREE_NODES:
        xref = stack.pop()
        if xref in seen:
            continue
        seen.add(xref)
        body = _object_body(document, xref)
        if not body:
            continue
        for name, num in _NAME_TREE_PAIR_RE.findall(body):
            pairs.append((name, int(num)))
        kids = re.search(r"/Kids\s*\[(.*?)\]", body, re.S)
        if kids:
            for num in re.findall(r"(\d+)\s+\d+\s+R", kids.group(1))[:20]:
                stack.append(int(num))
    return pairs


def _recover_kids_tree_entries(
    document: fitz.Document, facts: dict[str, Any]
) -> None:
    """Recover embedded-file entries the embfile API cannot see.

    A structurally valid balanced name tree makes embfile_count() return 0 (only flat goes
    through the API). The recovery path is isomorphic to the API path: name / filename (/F) /
    description (/Desc) / size (/EF stream /Length) / sha256
    (bytes decoded via xref_stream). Sample-level facts are registered; nothing is ever extracted to disk.
    """
    root = _catalog_key(document, "Names/EmbeddedFiles")
    if not root:
        return
    match = _INDIRECT_REF_RE.match(root.strip())
    if not match:
        return
    entries: list[dict[str, Any]] = []
    for name, fs_xref in _name_tree_walk(document, int(match.group(1))):
        entry: dict[str, Any] = {"name": name}
        body = _object_body(document, fs_xref)
        if body:
            fmatch = re.search(r"/F\s*\(([^)\r\n]{0,200})\)", body)
            if fmatch:
                entry["filename"] = fmatch.group(1)
            dmatch = re.search(r"/Desc\s*\(([^)\r\n]{0,200})\)", body)
            if dmatch:
                entry["description"] = dmatch.group(1)
            ef = re.search(
                r"/EF\s*<<[^>]*?/F\s+(\d+)\s+\d+\s+R", body, re.S
            )
            if ef:
                stream_xref = int(ef.group(1))
                try:
                    kind, value = document.xref_get_key(
                        stream_xref, "Length"
                    )
                    if kind == "int":
                        entry["size"] = int(str(value))
                except Exception:
                    pass
                try:
                    data = document.xref_stream(stream_xref)
                    if data:
                        entry["sha256"] = hashlib.sha256(data).hexdigest()
                except Exception:
                    pass
        entries.append(entry)
    if not entries:
        return
    facts["count"] = len(entries)
    facts["files"] = entries[:_MAX_EMBEDDED_SAMPLES]
    facts["truncated"] = len(entries) > _MAX_EMBEDDED_SAMPLES
    facts["recovered_by"] = "name_tree_walk"
    facts["note"] = (
        "MuPDF embfile API reported 0 for a balanced (/Kids) name tree; "
        "entries recovered by a bounded manual xref-level walk of "
        "Names/EmbeddedFiles. sha256 computed from decoded stream bytes "
        "via xref_stream; sizes from the filespec /EF stream /Length."
    )


def _incremental_update_facts(
    document: fitz.Document, path: Path
) -> dict[str, Any]:
    """Incremental-revision (/Prev chain) registration.

    Adversarial round 9: incremental updates allow objects to be appended after the original file — redefined objects
    (an old OpenAction, old /AA, old embedded streams) survive in the revision chain, and most viewers
    merge them during parsing. Nothing was disclosed before. Facts register only the chain's existence and revision level:
    a /Prev appearing in the trailer marks the chain; %%EOF counting is a lower-bound
    estimate of the revision level (%%EOF bytes inside uncompressed streams over-count; the note declares this),
    and the /Prev chain is not walked level by level.
    """
    try:
        trailer = str(document.pdf_trailer())
    except Exception:
        trailer = ""
    has_prev = "/Prev" in trailer
    try:
        raw = path.read_bytes()
    except Exception:
        raw = b""
    eof_count = raw.count(b"%%EOF") if raw else 0
    if not (has_prev or eof_count > 1):
        return {"present": False}
    return {
        "present": True,
        "trailer_prev": has_prev,
        "eof_marker_count": eof_count,
        "revision_estimate": max(eof_count, 2),
        "note": (
            "incremental revisions appended after the original %%EOF; "
            "superseded objects survive in earlier revisions and "
            "readers merge the chain. %%EOF counting is an estimate "
            "(uncompressed streams containing literal %%EOF bytes can "
            "overcount); /Prev chains are not walked revision by revision"
        ),
    }


def _embedded_hash_and_mime(
    document: fitz.Document, index: int, entry: dict[str, Any]
) -> dict[str, Any]:
    """sha256 + MIME per attachment (spec §16); skips are explicit.

    MIME is guessed from the *filename* only and never from content sniffing
    — an unknown guess is recorded as ``application/octet-stream`` with its
    source marked, so downstream consumers can distrust it.
    """
    result: dict[str, Any] = {
        "sha256": None,
        "hash_status": "not_attempted",
        "mime_type": None,
        "mime_type_source": "filename_guess",
    }
    filename = entry.get("filename") or entry.get("name") or ""
    try:
        guessed = mimetypes.guess_type(str(filename))[0]
        result["mime_type"] = guessed or "application/octet-stream"
    # Failed mimetypes guessing falls back to octet-stream (never blocks hash and size recording).
    except Exception:
        pass
    size = int(entry.get("size") or 0)
    if size > _MAX_EMBEDDED_HASH_BYTES:
        result["hash_status"] = "skipped_size_exceeds_limit"
        return result
    try:
        data = document.embfile_get(index)
        result["sha256"] = hashlib.sha256(data).hexdigest()
        result["hash_status"] = "computed"
    except Exception:
        result["hash_status"] = "failed_to_read_bytes"
    return result


# ------------------------------------------------------------
# optional-content layers
# ------------------------------------------------------------

_OCGS_ARRAY_RE = re.compile(r"/OCGs\s*\[([^\]]*)\]")


def _layer_facts(document: fitz.Document) -> dict[str, Any]:
    oc_properties = _catalog_key(document, "OCProperties")
    if oc_properties is None:
        return {
            "present": False,
            "ocg_count": 0,
            "names": [],
        }
    # Indirect-reference form (the standard form of real writers): xref_get_key returns
    # only the 'N 0 R' reference string, and regex-scanning it always comes up empty — follow
    # one hop to get the object body (same root cause as the signature /V and action /AA).
    raw = oc_properties.strip()
    indirect = _INDIRECT_REF_RE.match(raw)
    if indirect:
        raw = _object_body(document, int(indirect.group(1)))
    # The raw OCProperties string may declare OCGs inline (/Type/OCG) or
    # via an indirect-reference array (/OCGs [n 0 R ...]); both are
    # counted, and each referenced object's /Name entry is read directly
    # from the xref table.  Factual description of what is declared,
    # nothing more.
    names = [match.group(1) for match in _LAYER_NAME_RE.finditer(raw)]
    ocg_refs: list[str] = []
    array = _OCGS_ARRAY_RE.search(raw)
    if array:
        ocg_refs = _REF_RE.findall(array.group(1))
    count = max(len(_OCG_TYPE_RE.findall(raw)), len(ocg_refs))
    for ref in ocg_refs[:_MAX_EMBEDDED_SAMPLES]:
        try:
            xref = int(ref.split()[0])
            kind, value = document.xref_get_key(xref, "Name")
        except Exception:
            continue
        if kind not in ("null", "unknown") and value:
            names.append(str(value).strip("()"))
    return {
        "present": True,
        "ocg_count": count,
        "names": names[:_MAX_EMBEDDED_SAMPLES],
        "note": "layer declarations recorded; visibility states are not evaluated",
    }


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def _catalog_key(document: fitz.Document, key: str) -> str | None:
    """Raw catalog key value, or None when the key is absent/null."""
    try:
        kind, value = document.xref_get_key(document.pdf_catalog(), key)
    except Exception:
        return None
    if kind in ("null", "unknown") or value in ("null", "None", ""):
        return None
    return str(value)


# Adversarial round 8: outline-entry cap (bounded walk of the First/Next chain + cycle safety).
_MAX_OUTLINE_ITEMS = 200


def _outline_item_xrefs(document: fitz.Document) -> list[int]:
    """Outline item xrefs via the /First -> /Next sibling chains.

    Bookmark-tree entries are organized as /First /Next chains per the spec; each may carry an /A action
    (mutually exclusive with /Dest). Same existing discipline: read-only xref tables, bounded, cycle-safe
    (a seen set guards against crafted loops). An absent/illegal outline tree root yields an empty list.
    """
    outlines = _catalog_key(document, "Outlines")
    if not outlines:
        return []
    match = _INDIRECT_REF_RE.match(outlines.strip())
    if not match:
        return []
    items: list[int] = []
    seen: set[int] = set()
    node = _xref_key_string(document, int(match.group(1)), "First")
    while node and len(items) < _MAX_OUTLINE_ITEMS:
        m = _INDIRECT_REF_RE.match(node.strip())
        if not m:
            break
        xref = int(m.group(1))
        if xref in seen:
            break
        seen.add(xref)
        items.append(xref)
        node = _xref_key_string(document, xref, "Next")
    return items
