"""Evidence-only extraction of DOCX package/container-level facts.

B9 scope (gap register): OOXML conformance flavor (strict vs transitional
root namespace), attached external templates, ``w:altChunk`` external
content declarations, XML entity/DTD declarations (the billion-laughs
family — declared, never expanded), zip-bomb compression ratios (declared
sizes, never inflated to verify), missing declared parts, and the
optional glossary (AutoText) / customXml parts.

Everything runs at the raw-zip level on purpose: a strict-namespace or
missing-part package cannot even be opened by python-docx, so this
summary must be recordable before the opening attempt, and every anomaly
is reported as evidence, never repaired.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

_W_NS_TRANSITIONAL = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_NS_STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"

_CONTENT_TYPES = "[Content_Types].xml"
_DOCUMENT_PART = "word/document.xml"

# Compressed parts above these declared sizes are only scanned for DTD/
# entity markers in their head bytes, never fully read.
_XML_SCAN_LIMIT = 4 << 20
_XML_SCAN_HEAD = 64 << 10

# A compression ratio above this is zip-bomb-shaped; recorded as declared
# evidence (file_size vs compress_size headers), never inflated to verify.
_COMPRESSION_RATIO_FLAG = 100
_HUGE_UNCOMPRESSED = 100 << 20

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_ENTITY_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)
_OVERRIDE_RE = re.compile(r'PartName="([^"]+)"')

_ALTCHUNK_RE = re.compile(
    r"<(?:[^:>]+:)?altChunk[^>]*r:id=" r'"([^"]+)"'
)
_REL_TARGET_RE = re.compile(
    r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"'
)
_EXTERNAL_TARGET_RE = re.compile(r'TargetMode="External"')
_TEMPLATE_TARGET_RE = re.compile(r"attachedTemplate", re.IGNORECASE)

_MAX_ALTCHUNK_SAMPLES = 10


def _read_part(package: zipfile.ZipFile, name: str, limit: int | None = None) -> bytes:
    try:
        with package.open(name) as handle:
            if limit is None:
                return handle.read()
            return handle.read(limit)
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
        return b""


def _conformance_facts(document_bytes: bytes) -> dict[str, Any]:
    """Classify the wordprocessingml root namespace flavor.

    A strict-namespace package is the single most common reason python-docx
    rejects an otherwise valid .docx; recording the flavor as evidence keeps
    that failure honest and attributable.
    """
    if _W_NS_STRICT.encode() in document_bytes:
        flavor = "strict"
    elif _W_NS_TRANSITIONAL.encode() in document_bytes:
        flavor = "transitional"
    elif document_bytes:
        flavor = "unknown"
    else:
        flavor = None
    return {
        "flavor": flavor,
        "transitional_namespace": _W_NS_TRANSITIONAL.encode() in document_bytes,
        "strict_namespace": _W_NS_STRICT.encode() in document_bytes,
    }


def _entity_facts(package: zipfile.ZipFile, part_names: list[str]) -> dict[str, Any]:
    """Declared DTD/DOCTYPE/ENTITY usage inside XML parts (never expanded)."""
    carriers: list[str] = []
    has_doctype = False
    has_entity = False
    for name in part_names:
        if not name.endswith((".xml", ".rels")):
            continue
        info = package.getinfo(name)
        if info.file_size > _XML_SCAN_LIMIT:
            head = _read_part(package, name, _XML_SCAN_HEAD)
        else:
            head = _read_part(package, name, _XML_SCAN_LIMIT)
        if not head:
            continue
        doctype = bool(_DOCTYPE_RE.search(head))
        entity = bool(_ENTITY_RE.search(head))
        if doctype or entity:
            carriers.append(name)
        has_doctype = has_doctype or doctype
        has_entity = has_entity or entity
    return {
        "doctype_declared": has_doctype,
        "entity_declared": has_entity,
        "carrier_parts": carriers[:_MAX_ALTCHUNK_SAMPLES],
        "note": (
            "declarations recorded as evidence; entities are never expanded "
            "so an entity-expansion (billion-laughs) payload is inert here"
        )
        if has_entity
        else None,
    }


def _zip_bomb_facts(package: zipfile.ZipFile) -> dict[str, Any]:
    """Declared compression ratios and oversized parts (never inflated)."""
    flagged: list[dict[str, Any]] = []
    for info in package.infolist():
        if info.compress_size <= 0:
            continue
        ratio = info.file_size / info.compress_size
        if ratio >= _COMPRESSION_RATIO_FLAG or info.file_size >= _HUGE_UNCOMPRESSED:
            flagged.append(
                {
                    "part": info.filename,
                    "declared_size_bytes": info.file_size,
                    "compressed_size_bytes": info.compress_size,
                    "declared_compression_ratio": round(ratio, 1),
                }
            )
    return {
        "flagged_parts": flagged[:_MAX_ALTCHUNK_SAMPLES],
        "note": (
            "ratios come from declared zip headers; nothing is inflated to "
            "verify, so a forged header cannot become a real bomb here"
        )
        if flagged
        else None,
    }


def _missing_parts_facts(
    package: zipfile.ZipFile, part_names: list[str]
) -> dict[str, Any]:
    """Parts declared by [Content_Types].xml that are absent from the zip."""
    content_types = _read_part(package, _CONTENT_TYPES).decode("utf-8", "replace")
    declared = _OVERRIDE_RE.findall(content_types)
    present = set(part_names)
    missing = sorted(name.lstrip("/") for name in declared if name.lstrip("/") not in present)
    required_missing = [name for name in (_DOCUMENT_PART,) if name not in present]
    return {
        "declared_override_count": len(declared),
        "declared_but_missing": missing,
        "required_parts_missing": required_missing,
    }


def _external_template_facts(package: zipfile.ZipFile) -> dict[str, Any]:
    """word/_rels/settings.xml.rels pointing to an external attachedTemplate."""
    rels = _read_part(package, "word/_rels/settings.xml.rels").decode(
        "utf-8", "replace"
    )
    if not rels:
        return {"attached_template_external": False}
    found: list[str] = []
    for match in re.finditer(r"<Relationship\b[^>]*>", rels):
        node = match.group(0)
        if not _TEMPLATE_TARGET_RE.search(node):
            continue
        if _EXTERNAL_TARGET_RE.search(node):
            target = re.search(r'Target="([^"]+)"', node)
            found.append(target.group(1) if target else node)
    return {
        "attached_template_external": bool(found),
        "external_template_targets": found,
    }


def _altchunk_facts(package: zipfile.ZipFile) -> dict[str, Any]:
    """w:altChunk placeholders whose content lives in other-format parts."""
    document = _read_part(package, _DOCUMENT_PART).decode("utf-8", "replace")
    if not document:
        return {"count": 0, "targets": []}
    rels = _read_part(package, "word/_rels/document.xml.rels").decode(
        "utf-8", "replace"
    )
    rel_targets = dict(_REL_TARGET_RE.findall(rels))
    targets: list[str] = []
    for rel_id in _ALTCHUNK_RE.findall(document):
        targets.append(rel_targets.get(rel_id, f"unresolved rId: {rel_id}"))
    return {
        "count": len(targets),
        "targets": targets[:_MAX_ALTCHUNK_SAMPLES],
        "note": (
            "altChunk placeholders point at non-OXML content (e.g. HTML "
            "parts); the placeholder text is recorded, the external part "
            "is never rendered into the narrative"
        )
        if targets
        else None,
    }


def _optional_part_facts(part_names: list[str]) -> dict[str, Any]:
    """Presence of glossary (AutoText), customXml, and docProps parts.

    Adversarial round 6 adds ``docprops_custom_parts``:
    ``docProps/custom.xml`` (the custom document-properties part) is
    a classic payload-hiding spot for APT weaponized documents, and is
    a different part family from the Word data store ``customXml/``
    — previously only the latter was scanned, leaving the former
    wholly invisible. Only the presence fact is registered here; the
    content is not registered.
    """
    return {
        "glossary_parts": sorted(
            name for name in part_names if name.startswith("word/glossary/")
        ),
        "custom_xml_parts": sorted(
            name for name in part_names if name.startswith("customXml/")
        ),
        "docprops_custom_parts": sorted(
            name
            for name in part_names
            if name.lower().startswith("docprops/custom")
        ),
    }


# source-application family derivation (docx_source_application_prereg_v1
# §2): frozen word list, evidence-derived classification, never a guess —
# values outside the list register as "other".
_APPLICATION_FAMILIES = (
    ("Microsoft Office Word", "microsoft_office_word"),
    ("Microsoft Macintosh Word", "microsoft_macintosh_word"),
    ("Microsoft Office Outlook", "microsoft_office_outlook"),
    ("LibreOffice", "libreoffice"),
    ("WPS", "wps_office"),
)


def _application_family(application: str) -> str:
    for needle, family in _APPLICATION_FAMILIES:
        if needle in application:
            return family
    return "other"


def _source_application_facts(package: zipfile.ZipFile) -> dict[str, Any] | None:
    """docProps/app.xml source-software declaration, verbatim.

    docx_source_application_prereg_v1 §2: raw declaration values only
    (Application/AppVersion, outer whitespace stripped — LibreOffice
    build strings keep internal whitespace); ``application_family`` is a
    frozen-list derivation from the declared string, not a guess.  The
    sub-key is written only when the part exists and at least one field
    is non-empty; every read failure degrades silently (no sub-key).
    r25 face-4 (8): ``TotalTime`` (total editing minutes; string
    recorded verbatim, never cast or interpreted — forged/zeroed
    values are evidence too) joins the same verbatim loop, key name
    ``total_editing_minutes``.
    r26: app.xml declaration chain completed — the eleven keys
    Pages/Words/Characters/CharactersWithSpaces/Paragraphs/Lines/
    DocSecurity/HyperlinksChanged/LinksUpToDate/ScaleCrop/SharedDoc
    get the same verbatim treatment (r25 recon flagged the
    words/pages SRC as substring false positives; the probe
    confirmed the whole chain was unregistered); write only when
    non-empty — clean documents get zero new keys.
    """
    raw = _read_part(package, "docProps/app.xml", _XML_SCAN_LIMIT)
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    facts: dict[str, Any] = {}
    for key, tag in (
        ("application", "Application"),
        ("application_version", "AppVersion"),
        ("total_editing_minutes", "TotalTime"),
        ("pages", "Pages"),
        ("words", "Words"),
        ("characters", "Characters"),
        ("characters_with_spaces", "CharactersWithSpaces"),
        ("paragraphs", "Paragraphs"),
        ("lines", "Lines"),
        ("doc_security", "DocSecurity"),
        ("hyperlinks_changed", "HyperlinksChanged"),
        ("links_up_to_date", "LinksUpToDate"),
        ("scale_crop", "ScaleCrop"),
        ("shared_doc", "SharedDoc"),
    ):
        match = re.search(
            rf"<{tag}>([^<]*)</{tag}>", text, re.DOTALL
        )
        if match:
            value = match.group(1).strip()
            if value:
                facts[key] = value
    if not facts:
        return None
    if "application" in facts:
        facts["application_family"] = _application_family(facts["application"])
    return facts


def extract_package_summary(file_path: str | Path) -> dict[str, Any]:
    """Collect package-level evidence before python-docx opens the file.

    Never raises on a malformed package: every failure degrades to the
    recorded ``zip_error`` field so the caller can still report evidence.
    """
    summary: dict[str, Any] = {
        "zip_container": False,
        "part_count": 0,
        "conformance": {},
        "entities": {},
        "compression": {},
        "parts": {},
        "external_template": {},
        "alt_chunks": {},
        "optional_parts": {},
    }
    try:
        package = zipfile.ZipFile(file_path)
    except (zipfile.BadZipFile, OSError) as error:
        summary["zip_error"] = str(error)
        return summary
    try:
        names = package.namelist()
        summary["zip_container"] = True
        summary["part_count"] = len(names)

        document_bytes = _read_part(package, _DOCUMENT_PART, _XML_SCAN_HEAD)
        summary["conformance"] = _conformance_facts(document_bytes)
        summary["entities"] = _entity_facts(package, names)
        summary["compression"] = _zip_bomb_facts(package)
        summary["parts"] = _missing_parts_facts(package, names)
        summary["external_template"] = _external_template_facts(package)
        summary["alt_chunks"] = _altchunk_facts(package)
        summary["optional_parts"] = _optional_part_facts(names)
        # Write only when non-empty (prereg §2): packages with app.xml
        # missing / entirely empty / unreadable get no sub-key.
        source_application = _source_application_facts(package)
        if source_application is not None:
            summary["source_application"] = source_application
        return summary
    finally:
        package.close()


__all__ = ["extract_package_summary"]
