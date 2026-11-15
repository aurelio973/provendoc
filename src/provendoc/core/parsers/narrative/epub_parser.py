# ============================================================
# Narrative Domain
# EPUB (.epub) Parser
# ============================================================
# Container fact chain (read-only, evidence first):
#   1. mimetype            — application/epub+zip expected value
#                            (recorded, never enforced)
#   2. META-INF/container.xml — rootfile@full-path → OPF location
#   3. OPF                 — dc:title/dc:creator/dc:language
#                            metadata, manifest (id →
#                            href/media-type), spine (idref order
#                            = reading order)
#   4. spine items         — XHTML chapters, ElementTree
#                            block-level walk → markdown-ish
#                            text (h1-h6 → #, p → paragraph,
#                            lists → -, tables → pipe rows,
#                            img → placeholder)
#
# Package contract evolution (epub_package v1 → v3, additive
# supersets): version detection / three-tier cover registration /
# deep DC profile / manifest coverage (missing + orphan) / spine
# XHTML link census (counts only) / defensive presence keys /
# OPF inference fallback (opf_source: inferred); then table
# structure / CSS presence profile / accessibility attributes /
# language and direction / fragment existence validation / SVG
# registration / structural block census (aggregate counts);
# later adversarial rounds added: spine XHTML HTML comment census
# (comments never enter the narrative; presence and body value
# are registered facts) + OPF <meta property="dcterms:modified">
# value registration (the EPUB3 modification clock).  All
# additive best-effort: no new parse failure modes, no part in
# tri-state derivation, zero text-contract drift.
#
# Fact boundaries:
#   - Only spine-declared chapters enter the narrative;
#     manifest nav/toc/cover/image items do not (navigation and
#     assets are representational facts, recorded under the
#     epub_navigation / epub_images namespaces).
#   - Chapter parse failure is a per-chapter honest failure:
#     the chapter is skipped, the error lands in
#     epub_chapters_failed, never sinks the whole book.
#   - Output text is markdown-family; HeadingCandidate is the
#     trusted heading fact provided by the parser (same contract
#     as tex/pptx).
import logging
import posixpath
import re
import time
import zipfile
from urllib.parse import unquote
from xml.etree import ElementTree

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts,
    detect_security_carriers,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)

# ZIP container guard (zip-bomb protection): central-directory
# limit checked before decompression
from provendoc.core.parsers.container_guard import (
    check_zip_package_quietly as _check_zip_package_safe,
)
from provendoc.core.parsers.container_guard import (
    zip_guard_error,
)
from provendoc.core.parsers.narrative.epub_unresolved import (
    derive_epub_unresolved,
)
from provendoc.core.parsers.narrative.shared import (
    localname as _localname,
)
from provendoc.core.parsers.narrative.shared import (
    markdown_table_lines as _markdown_table_lines,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.heading_patterns import HeadingCandidate
from provendoc.core.structure.special_modules import detect_special_modules
from provendoc.core.text.normalization import sanitize_invisible_chars

_CONTAINER_PATH = "META-INF/container.xml"
_MIMETYPE_PATH = "mimetype"
_EXPECTED_MIMETYPE = "application/epub+zip"

_MAX_SAMPLES = 5

# Deep DC profile bounds: single-value truncation and subject cap
_DC_VALUE_MAX = 100
_DC_SUBJECT_MAX = 10

_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"

_log = logging.getLogger(__name__)

# Common HTML entities are undeclared in XHTML and break strict
# XML parsing; on failure, substitute the unicode equivalents and
# retry once (conservative whitelist).
_HTML_ENTITIES = {
    "nbsp": "\u00a0",
    "ndash": "\u2013",
    "mdash": "\u2014",
    "hellip": "\u2026",
    "lsquo": "\u2018",
    "rsquo": "\u2019",
    "ldquo": "\u201c",
    "rdquo": "\u201d",
    "copy": "\u00a9",
    "trade": "\u2122",
    "reg": "\u00ae",
    "deg": "\u00b0",
    "middot": "\u00b7",
    "bull": "\u2022",
    "dagger": "\u2020",
    "sect": "\u00a7",
    "pound": "\u00a3",
    "euro": "\u20ac",
    "times": "\u00d7",
    "divide": "\u00f7",
}
_ENTITY_RE = re.compile(
    r"&(" + "|".join(_HTML_ENTITIES) + r");"
)

# Link census: <a href> anchor regex over raw spine XHTML bytes;
# split into counts (external = carries a URL scheme, e.g.
# http/https/mailto) and samples; no fragment/target existence
# validation here (that is the census's validation side).
_ANCHOR_HREF_RE = re.compile(
    rb"<a[\s>][^>]*?\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    re.IGNORECASE,
)
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

# Content census regex family: single-pass multi-regex counting
# over the same raw spine XHTML bytes as the link census; all
# aggregate counts, no DOM-level extraction.
_TABLE_RE = re.compile(rb"<table[\s>]", re.IGNORECASE)
_TABLE_ROW_RE = re.compile(rb"<tr[\s>]", re.IGNORECASE)
_TABLE_CELL_RE = re.compile(rb"<td[\s>]", re.IGNORECASE)
_TABLE_HEADER_CELL_RE = re.compile(rb"<th[\s>]", re.IGNORECASE)
_ROWSPAN_RE = re.compile(rb"\browspan\s*=", re.IGNORECASE)
_COLSPAN_RE = re.compile(rb"\bcolspan\s*=", re.IGNORECASE)
_STYLESHEET_LINK_RE = re.compile(
    rb"<link[^>]*\brel\s*=\s*[\"']stylesheet[\"']", re.IGNORECASE
)
_STYLE_ATTR_RE = re.compile(rb"\bstyle\s*=\s*[\"']", re.IGNORECASE)
_IMG_RE = re.compile(rb"<img[\s>]", re.IGNORECASE)
_IMG_ALT_RE = re.compile(
    rb"<img[^>]*\balt\s*=\s*[\"'][^\"']+[\"']", re.IGNORECASE
)
_ARIA_ATTR_RE = re.compile(rb"\baria-[a-z-]+\s*=", re.IGNORECASE)
_ROLE_VALUE_RE = re.compile(
    rb"\brole\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
# html@lang capture: lookbehind excludes xml:lang / data-lang
_HTML_LANG_RE = re.compile(
    rb"<html[^>]*(?<![\w:])lang\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_XML_LANG_RE = re.compile(rb"\bxml:lang\s*=", re.IGNORECASE)
_DIR_RTL_RE = re.compile(rb"\bdir\s*=\s*[\"']rtl[\"']", re.IGNORECASE)
_SVG_RE = re.compile(rb"<svg[\s>]", re.IGNORECASE)
_SCRIPT_TAG_RE = re.compile(rb"<script[\s>]", re.IGNORECASE)
# Non-script embedding carrier elements (object/embed/iframe) —
# security facts on par with <script>; presence is registered in
# the census, content is never taken, never executed.
_OBJECT_TAG_RE = re.compile(rb"<object[\s>]", re.IGNORECASE)
_EMBED_TAG_RE = re.compile(rb"<embed[\s>]", re.IGNORECASE)
_IFRAME_TAG_RE = re.compile(rb"<iframe[\s>]", re.IGNORECASE)
# Inline event attributes (onclick=/onerror= and other on*
# handlers) — presence counting, on par with
# style_attribute_count; never executed.
_EVENT_ATTR_RE = re.compile(rb'(?<=[\s"\x27])on[a-z]{3,20}\s*=')
# audio/video autoplay attributes — auto-trigger surface presence
# counting (registered yes, never played; the attribute value is
# neither parsed nor executed).
_AUDIO_VIDEO_TAG_RE = re.compile(
    rb"<(?:audio|video)\b[^>]*>", re.IGNORECASE
)
_SVG_TEXT_RE = re.compile(rb"<text[\s>]", re.IGNORECASE)
_BLOCKQUOTE_RE = re.compile(rb"<blockquote[\s>]", re.IGNORECASE)
_FIGURE_RE = re.compile(rb"<figure[\s>]", re.IGNORECASE)
_FIGCAPTION_RE = re.compile(rb"<figcaption[\s>]", re.IGNORECASE)
_ID_VALUE_RE = re.compile(
    rb"\bid\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
# spine XHTML HTML comment census — raw-bytes regex on the same
# channel as the link census; comments stay out of the narrative
# by contract, but their presence and body value are declared
# document facts (silent omission would be a gap).
_HTML_COMMENT_RE = re.compile(rb"<!--(.*?)-->", re.DOTALL)
_COMMENT_SNIPPET = 120  # comment body sample truncation cap

# CSS rules layer (presence counting over manifest text/css bytes)
_CSS_DISPLAY_NONE_RE = re.compile(
    rb"display\s*:\s*none", re.IGNORECASE
)
_CSS_VISIBILITY_HIDDEN_RE = re.compile(
    rb"visibility\s*:\s*hidden", re.IGNORECASE
)
_CSS_GENERATED_RE = re.compile(
    rb"::?(?:before|after)\b", re.IGNORECASE
)
_CSS_MEDIA_QUERY_RE = re.compile(rb"@media\b", re.IGNORECASE)
_CSS_FONT_FACE_RE = re.compile(rb"@font-face\b", re.IGNORECASE)
# CSS external-reference surface — @import / url() target
# counting; targets carrying a URL scheme (http/https/file/data …)
# get a separate remote count + bounded value samples (same
# surface as the manifest remote split); counting and registration
# only, never fetched.
_CSS_IMPORT_RE = re.compile(rb"@import\b", re.IGNORECASE)
_CSS_URL_RE = re.compile(rb"url\(\s*[\"']?", re.IGNORECASE)
_CSS_REMOTE_URL_RE = re.compile(
    rb"url\(\s*[\"']?([a-zA-Z][a-zA-Z0-9+.\-]*:[^\"')\s]*)",
    re.IGNORECASE,
)
_CSS_REMOTE_IMPORT_RE = re.compile(
    rb"@import\s+(?!url\b)[\"']?"
    rb"([a-zA-Z][a-zA-Z0-9+.\-]*:[^\"';\s)]*)",
    re.IGNORECASE,
)
# url() references inside inline style attributes on spine
# XHTML — existence counting (style_attribute_count family;
# count only, no value extraction, no fetching).
_INLINE_STYLE_URL_RE = re.compile(
    rb"style\s*=\s*\"[^\"]*\burl\("
    rb"|style\s*=\s*'[^']*\burl\(",
    re.IGNORECASE,
)


def _bump_bounded(counts, key):
    """Bounded counting dict: existing keys always accumulate,
    new keys are capped at _MAX_SAMPLES distinct values."""
    if key in counts or len(counts) < _MAX_SAMPLES:
        counts[key] = counts.get(key, 0) + 1

# Block-level elements → handling
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "head", "svg"}


class EpubParser(BaseParser):
    """EPUB (.epub) narrative parser: conservative zipfile +
    ElementTree extraction.

    Same narrative extended-format family as tex/pptx: outputs
    markdown-ish text + trusted HeadingCandidate; metadata
    carries epub package-level facts (spine length/manifest/skipped
    chapters/package metadata).
    """

    def __init__(self, encoding="utf-8", tolerant=True):
        """Initialize with encoding and tolerance switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    def parse(self, file_path):
        """Parse a .epub into a Document (markdown-ish body text
        + package-level facts)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "epub"

        # ZIP container guard (zip-bomb protection): central
        # directory only, verdict before decompression; a
        # violation is an honest failure; when the central
        # directory itself is unreadable, pass through and let the
        # zipfile parse fail naturally — the guard is never fatal.
        _zip_guard = _check_zip_package_safe(file_path)
        if _zip_guard is not None:
            metadata["zip_package_guard"] = _zip_guard
            if _zip_guard.get("verdict") == "violation":
                metadata["parse_failed"] = True
                metadata["parse_error"] = zip_guard_error(_zip_guard)
                return Document(text="", metadata=metadata)

        start_time = time.perf_counter()

        try:
            text, headings, package, extra = self._extract(file_path)

            metadata["epub_package"] = package
            metadata["epub_navigation"] = extra["navigation"]
            metadata["epub_images"] = extra["images"]
            # Per-chapter container spans: each chapter's
            # [start, end) range in the body text + href —
            # locality and provenance input for the
            # container-aware representation layer (read-only
            # facts, never part of the narrative).
            metadata["epub_chapter_spans"] = extra["chapter_spans"]
            # Object-level unresolved registry: derived from the
            # existing failed_chapters / skipped_spine_items /
            # navigation.error fact sources; never rewrites the
            # original keys, written only when non-empty. Placed
            # before the empty-body honest-failure check — a book
            # whose chapters all failed still gets its registry
            # (the missing-reason is exactly the fact a reader
            # needs most); guarded silent degradation, never
            # blocks the main parse.
            try:
                stamp_unresolved_registry(
                    metadata, "epub_unresolved",
                    derive_epub_unresolved(
                        failed_chapters=package.get("failed_chapters"),
                        skipped_spine_items=package.get(
                            "skipped_spine_items"
                        ),
                        navigation=extra["navigation"],
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("epub unresolved registry skipped: %s", exc)
            metadata["heading_candidates"] = headings
            metadata["special_modules"] = detect_special_modules(text)
            # Text-layer domain signals (same detector as
            # docx/md/txt/pdf/pptx)
            metadata["domain_signals"] = detect_domain_signals(text)
            metadata["heading_count"] = len(headings)
            metadata["chapter_count"] = package["chapter_count"]

            # Security carrier census: epub extracts its XHTML body
            # from the ZIP container and bypasses base's
            # _read_and_diagnose path, so the same census is mounted
            # here on the extracted body view; fact codes are
            # appended to warnings (setdefault guards the key),
            # structured counts land in
            # metadata["security_carriers"] only when non-zero.
            # Carriers can hide in chapter body text or headings —
            # read-only, never rewritten.
            carrier_census = detect_security_carriers(text)
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata.setdefault("warnings", []).extend(
                    build_carrier_facts(carrier_census)
                )

            metadata["parse_success"] = True

            # Honest failure: package structure intact but no
            # readable narrative at all (image-only book / every
            # spine item failed to parse).
            if not text.strip():
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "epub contains no extractable narrative text"
                )
                return Document(text="", metadata=metadata)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )

            # Working-text sanitization (same contract as
            # base._read_and_diagnose): normalizes Document.text
            # only; source_text keeps the extracted original;
            # heading and other facts were computed on the raw view
            # above; the sanitization action is disclosed via
            # metadata["sanitization"] + warning.
            sanitized_text, sanitization = sanitize_invisible_chars(text)
            if sanitization:
                metadata["sanitization"] = sanitization
                normalized = sum(
                    sanitization.get(key, 0)
                    for key in (
                        "zero_width_space", "bidi_resolved",
                        "bidi_unresolved", "zero_width", "mid_file_bom",
                        "ansi_escape", "control_char", "form_feed",
                    )
                )
                if normalized:
                    metadata.setdefault("warnings", []).append(
                        f"{normalized} invisible characters normalized "
                        "(see metadata.sanitization)"
                    )

            return Document(
                text=sanitized_text,
                metadata=metadata,
                source_text=text,
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            metadata["parse_success"] = False
            return Document(text="", metadata=metadata)

    # ========================================================
    # package walk: container → OPF → spine
    # ========================================================

    @staticmethod
    def _extract(file_path):
        with zipfile.ZipFile(file_path) as zf:
            names = set(zf.namelist())

            # mimetype is an expected fact: mismatch is recorded,
            # never rejected
            declared = None
            if _MIMETYPE_PATH in names:
                declared = zf.read(_MIMETYPE_PATH).decode(
                    "utf-8", "replace"
                ).strip()

            # 1. container.xml → OPF path (declared evidence).
            #    When container is missing/corrupt/has no readable
            #    rootfile, scan the zip for *.opf and take the first
            #    in lexicographic order as an inference fallback;
            #    opf_source discloses the origin; only a total
            #    absence of OPF is an honest failure.
            opf_path = None
            opf_source = "container"
            rootfile_count = 0
            try:
                if _CONTAINER_PATH in names:
                    container = EpubParser._parse_xml(
                        zf.read(_CONTAINER_PATH)
                    )
                    # The OCF namespace is
                    # urn:oasis:names:tc:opendocument:xmlns:container
                    # (no :1.0 suffix); real-world container.xml
                    # has namespace variants, so rootfile is matched
                    # leniently by localname, same as nav/NCX/body.
                    candidates = []
                    for element in container.iter():
                        if _localname(element.tag) != "rootfile":
                            continue
                        candidate = element.get("full-path") or ""
                        if candidate:
                            rootfile_count += 1
                            candidates.append(candidate)
                    for candidate in candidates:
                        if candidate in names:
                            opf_path = candidate
                            break
            except Exception:
                # A corrupt container counts as "no declared
                # evidence"; the inference fallback below decides.
                rootfile_count = 0
            if opf_path is None:
                inferred = sorted(
                    entry for entry in names
                    if entry.lower().endswith(".opf")
                )
                if inferred:
                    opf_path = inferred[0]
                    opf_source = "inferred"
            if opf_path is None:
                raise ValueError(
                    "epub package declares no readable rootfile"
                )

            # 2. OPF: metadata / manifest / spine
            opf = EpubParser._parse_xml(zf.read(opf_path))
            opf_dir = posixpath.dirname(opf_path)

            pkg_meta = EpubParser._opf_metadata(opf)
            manifest, remote_facts = EpubParser._opf_manifest(
                opf, opf_dir
            )
            spine_ids = EpubParser._opf_spine(opf)

            # 3. spine order → chapter text
            lines = []
            headings = []
            chapter_files = []
            skipped = []
            failed = []
            # Start offset of each successfully rendered chapter in
            # the joined text (same measure as _offset_of) — a
            # container span fact, letting the container-aware
            # representation layer crop per chapter and trace each
            # unit back to its chapter.
            chapter_starts = []  # [(href, start)]
            # Link census scope: all readable spine XHTML (never
            # depends on XML parse success; CRC-failed members are
            # skipped defensively when the census reads them).
            link_hrefs = []

            for itemref in spine_ids:
                item = manifest.get(itemref)
                if item is None:
                    skipped.append(
                        {"idref": itemref, "reason": "missing_manifest_item"}
                    )
                    continue
                href = item["href"]
                if item["media_type"] not in (
                    "application/xhtml+xml", "text/html",
                ):
                    skipped.append(
                        {
                            "idref": itemref,
                            "href": href,
                            "reason": "non_xhtml_spine_item",
                        }
                    )
                    continue
                if href not in names:
                    skipped.append(
                        {
                            "idref": itemref,
                            "href": href,
                            "reason": "missing_zip_member",
                        }
                    )
                    continue
                link_hrefs.append(href)

                try:
                    chapter = EpubParser._parse_chapter(zf.read(href))
                except Exception as e:
                    failed.append(
                        {
                            "href": href,
                            "error": str(e),
                        }
                    )
                    continue

                chapter_files.append(href)
                chapter_starts.append((href, _offset_of(lines)))
                chapter.render(lines, headings)

            text = "\n".join(part for part, _ in lines)

            # Spans tile the whole text: chapter i's end = chapter
            # i+1's start, the last chapter runs to the text end —
            # every character offset belongs to exactly one chapter
            # (untitled chapters get their own span too, no longer
            # flow into the previous chapter's tail section).
            chapter_spans = []
            for i, (href, start) in enumerate(chapter_starts):
                end = (
                    chapter_starts[i + 1][1]
                    if i + 1 < len(chapter_starts)
                    else len(text)
                )
                chapter_spans.append({
                    "chapter_index": i,
                    "href": href,
                    "start": start,
                    "end": end,
                })

            # Navigation and image evidence: the nav/NCX and image
            # assets declared by the manifest land in their own
            # namespaces (read-only facts, never part of the body).
            extra = {
                "navigation": EpubParser._navigation_evidence(
                    zf, names, manifest
                ),
                "images": EpubParser._image_evidence(manifest),
                "chapter_spans": chapter_spans,
            }

            # Package fact aggregation: all additive new keys, zero
            # drift on existing keys, no part in tri-state derivation.
            manifest_hrefs = {
                item["href"] for item in manifest.values()
            }
            # A remote href (a manifest item carrying a URL scheme)
            # is external-reference semantics and is excluded from
            # the missing count — remote is not a missing in-package
            # asset; the semantic split lives in _opf_manifest and
            # the remote_* keys.
            missing_hrefs = sorted(
                href for href in manifest_hrefs
                if href not in names
                and href not in remote_facts["pseudo_hrefs"]
            )
            # orphan: zip members never declared in the manifest,
            # excluding structural members (mimetype / META-INF/* /
            # the OPF itself) and directory entries.
            structural = (
                {_MIMETYPE_PATH, opf_path}
                | {
                    name for name in names
                    if name.startswith("META-INF/")
                }
            )
            orphan_entries = sorted(
                name for name in names
                if name not in manifest_hrefs
                and name not in structural
                and not name.endswith("/")
            )
            has_nav = any(
                "nav" in (item["properties"] or "").split()
                for item in manifest.values()
            )
            has_ncx = any(
                item["media_type"] == "application/x-dtbncx+xml"
                for item in manifest.values()
            )
            epub_version = opf.get("version") or None
            linear_no, dup_idref, ppd = EpubParser._opf_spine_facts(
                opf, spine_ids
            )
            content = EpubParser._content_census(
                zf, link_hrefs, names
            )
            css = EpubParser._css_facts(zf, manifest, names)
            cover = EpubParser._cover_evidence(
                opf, manifest, opf_dir, names
            )
            # Media overlay / autoplay surface (SMIL declarations +
            # spine media-overlay references + autoplay attributes)
            # — computed for non-zero writes only; registered yes,
            # never played.
            media_overlay_items = [
                item["href"] for item in manifest.values()
                if item["media_type"] == "application/smil+xml"
                or "media-overlays" in (
                    item["properties"] or ""
                ).split()
            ]
            media_overlay_refs = []
            for itemref in opf.iter(f"{{{_OPF_NS}}}itemref"):
                overlay_id = itemref.get("media-overlay")
                if overlay_id:
                    media_overlay_refs.append(
                        f"{itemref.get('idref')}->{overlay_id}"
                    )

            package = {
                "contract": "epub_package_v3",
                "declared_mimetype": declared,
                "mimetype_matches": (
                    declared == _EXPECTED_MIMETYPE
                    if declared is not None
                    else None
                ),
                "opf_path": opf_path,
                "opf_source": opf_source,
                # Version detection: package@version raw value +
                # features limited to the two nav/ncx booleans
                # (no values invented for unknown flags)
                "epub_version": epub_version,
                "version_source": (
                    "package_opf" if epub_version else None
                ),
                "features": {"nav": has_nav, "ncx": has_ncx},
                # Cover three tiers: properties > meta > guide;
                # no filename inference (no invented values)
                "cover": cover,
                "title": pkg_meta.get("title"),
                "creator": pkg_meta.get("creator"),
                "language": pkg_meta.get("language"),
                # Deep DC profile: single values truncated at 100
                # + subjects bounded ≤10; a zero-hit publisher
                # stays absent, no invented key
                "date": pkg_meta.get("date"),
                "identifier": pkg_meta.get("identifier"),
                "rights": pkg_meta.get("rights"),
                "source": pkg_meta.get("source"),
                "contributor": pkg_meta.get("contributor"),
                "subjects": pkg_meta.get("subjects"),
                # Manifest coverage: missing count + samples ≤5 +
                # orphan count (a zero result is pinned as zero)
                "manifest_item_count": len(manifest),
                "manifest_resolved_count": (
                    len(manifest) - len(missing_hrefs)
                ),
                "manifest_missing_count": len(missing_hrefs),
                "manifest_missing_samples": missing_hrefs[:_MAX_SAMPLES],
                "orphan_entry_count": len(orphan_entries),
                "spine_item_count": len(spine_ids),
                "chapter_count": len(chapter_files),
                "chapters": chapter_files,
                "skipped_spine_items": skipped,
                "failed_chapters": failed,
                # Link census: count split + samples
                "internal_link_count": content["internal_link_count"],
                "external_link_count": content["external_link_count"],
                "link_href_samples": content["link_href_samples"],
                # Table structure facts: document-level aggregate
                # counts; no cell-content extraction (the text
                # layer renders pipe rows already)
                "table_count": content["table_count"],
                "table_row_count": content["table_row_count"],
                "table_cell_count": content["table_cell_count"],
                "table_header_cell_count": content[
                    "table_header_cell_count"
                ],
                "rowspan_count": content["rowspan_count"],
                "colspan_count": content["colspan_count"],
                # CSS presence profile: file/reference/rule-layer
                # counts; selector parsing and visibility verdicts
                # are out of scope here
                "css_file_count": css["css_file_count"],
                "css_total_bytes": css["css_total_bytes"],
                "stylesheet_link_count": content[
                    "stylesheet_link_count"
                ],
                "style_attribute_count": content[
                    "style_attribute_count"
                ],
                "css_display_none_count": css["css_display_none_count"],
                "css_visibility_hidden_count": css[
                    "css_visibility_hidden_count"
                ],
                "css_generated_content_count": css[
                    "css_generated_content_count"
                ],
                "css_media_query_count": css["css_media_query_count"],
                "css_font_face_count": css["css_font_face_count"],
                # CSS/inline external-reference surface — @import /
                # url() totals + a separate remote (scheme-bearing)
                # count + bounded samples, never fetched; url()
                # inside inline style attributes counts the same way
                "css_import_count": css["css_import_count"],
                "css_url_reference_count": css["css_url_reference_count"],
                "css_remote_reference_count": css[
                    "css_remote_reference_count"
                ],
                "css_remote_reference_samples": css[
                    "css_remote_reference_samples"
                ],
                "inline_style_url_count": content["inline_style_url_count"],
                # Accessibility attributes: counts profile; the
                # coverage ratio is derived by the consumer as a
                # quotient of the two keys
                "image_element_count": content["image_element_count"],
                "image_alt_nonempty_count": content[
                    "image_alt_nonempty_count"
                ],
                "aria_attribute_count": content["aria_attribute_count"],
                "role_attribute_count": content["role_attribute_count"],
                "role_value_counts": content["role_value_counts"],
                # Language and direction: registration profile
                # (value distribution bounded ≤5)
                "html_lang_counts": content["html_lang_counts"],
                "xml_lang_attribute_count": content[
                    "xml_lang_attribute_count"
                ],
                "rtl_direction_count": content["rtl_direction_count"],
                # Fragment existence validation: broken count +
                # bounded samples; same-document href="#f" belongs
                # to the current document
                "fragment_link_count": content["fragment_link_count"],
                "broken_fragment_count": content[
                    "broken_fragment_count"
                ],
                "broken_fragment_samples": content[
                    "broken_fragment_samples"
                ],
                # SVG registration (register, do not extract) +
                # structural block census
                "svg_element_count": content["svg_element_count"],
                "svg_inline_text_count": content[
                    "svg_inline_text_count"
                ],
                # <script> is silently skipped at the render layer;
                # presence is registered in the census so it is
                # never silently omitted
                "script_element_count": content["script_element_count"],
                # object/embed/iframe embedding carriers + inline
                # event attributes (script precedent: presence
                # registration only, content never taken, never
                # executed) + the OCF remote-resource split (a
                # manifest item whose href carries a scheme — an
                # external reference by semantics, not a missing
                # in-package asset; a properties="remote-resources"
                # declaration is counted separately)
                "object_element_count": content["object_element_count"],
                "embed_element_count": content["embed_element_count"],
                "iframe_element_count": content["iframe_element_count"],
                "event_handler_attribute_count": content[
                    "event_handler_attribute_count"
                ],
                "manifest_remote_item_count": remote_facts["count"],
                "manifest_remote_item_samples": remote_facts["samples"],
                "remote_resource_property_count": sum(
                    1 for item in manifest.values()
                    if "remote-resources" in (
                        item["properties"] or ""
                    ).split()
                ),
                "blockquote_count": content["blockquote_count"],
                "figure_count": content["figure_count"],
                "figcaption_count": content["figcaption_count"],
                # Defensive presence keys: DRM minimal profile
                # (detect/register/never bypass) + rendition count
                # + spine attribute reads (zero pinned)
                "encryption_xml_present": (
                    "META-INF/encryption.xml" in names
                ),
                "rights_xml_present": "META-INF/rights.xml" in names,
                "rootfile_count": rootfile_count,
                "spine_linear_no_count": linear_no,
                "spine_duplicate_idref_count": dup_idref,
                "page_progression_direction": ppd,
            }
            # Auto-trigger surface — non-zero writes only
            # (zero diff on the clean golden)
            if media_overlay_items:
                package["media_overlay_item_count"] = len(
                    media_overlay_items
                )
                package["media_overlay_item_hrefs"] = (
                    media_overlay_items[:_MAX_SAMPLES]
                )
            if media_overlay_refs:
                package["media_overlay_spine_refs"] = (
                    media_overlay_refs[:_MAX_SAMPLES]
                )
            if content["autoplay_attribute_count"]:
                package["autoplay_attribute_count"] = content[
                    "autoplay_attribute_count"
                ]
            # XHTML HTML comment census — non-zero writes only
            # (clean-control zero-new-keys discipline, same as the
            # autoplay keys)
            if content["html_comment_count"]:
                package["html_comment_count"] = content[
                    "html_comment_count"
                ]
                package["html_comment_samples"] = content[
                    "html_comment_samples"
                ]
            # OPF <meta property="dcterms:modified"> raw value (the
            # EPUB3 modification clock) — written only when present
            if pkg_meta.get("dcterms_modified"):
                package["dcterms_modified"] = pkg_meta[
                    "dcterms_modified"
                ]
            return text, headings, package, extra

    # ========================================================
    # OPF readers
    # ========================================================

    @staticmethod
    def _opf_metadata(opf_root):
        """DC metadata facts (deep profile): lenient localname
        collection.

        Real-world OPF has namespace variants, consistent with the
        lenient discipline for container/rootfile/nav.  Single-
        value fields take the first non-empty value truncated at
        100 chars (raw values, no normalization); the subjects
        list is bounded ≤10; a zero-hit publisher stays absent,
        no invented key; the refines/meta property semantic chain
        is out of scope here.
        """
        meta = None
        for elem in opf_root.iter():
            if _localname(elem.tag) == "metadata":
                meta = elem
                break
        out = {}
        if meta is None:
            return out
        singles = (
            "title", "creator", "language", "date", "identifier",
            "rights", "source", "contributor",
        )
        subjects = []
        for child in meta:
            name = _localname(child.tag)
            text = (child.text or "").strip()
            if not text:
                continue
            # <meta property="dcterms:modified"> is the EPUB3
            # modification clock; the raw value is registered (first
            # non-empty, truncated like the single-value fields) —
            # the refines semantic chain stays uninterpreted, the
            # raw value is only registered.
            if (
                name == "meta"
                and (child.get("property") or "") == "dcterms:modified"
            ):
                if "dcterms_modified" not in out:
                    out["dcterms_modified"] = text[:_DC_VALUE_MAX]
                continue
            if name == "subject":
                if len(subjects) < _DC_SUBJECT_MAX:
                    subjects.append(text)
            elif name in singles and name not in out:
                out[name] = text[:_DC_VALUE_MAX]
        if subjects:
            out["subjects"] = subjects
        return out

    @staticmethod
    def _opf_manifest(opf_root, opf_dir):
        """Manifest item collection + remote split.

        An item whose href carries a URL scheme (http/https/file
        …) is an OCF external reference (EPUB3 remote-resources
        semantics): it still enters the manifest with existing
        behavior (pseudo-path href; a downstream consumer's read
        failure is silently skipped, zero drift), but its
        pseudo-path set and raw href samples go out via
        remote_facts — the aggregation step excludes remote from
        the manifest_missing count (remote is not a missing
        in-package asset) and registers the manifest_remote_*
        keys.
        """
        manifest = {}
        remote_total = 0
        remote_samples = []
        remote_pseudo = set()
        for item in opf_root.iter(f"{{{_OPF_NS}}}item"):
            item_id = item.get("id")
            href = item.get("href")
            if not item_id or not href:
                continue
            if _URL_SCHEME_RE.match(href):
                remote_total += 1
                if len(remote_samples) < _MAX_SAMPLES:
                    remote_samples.append(href)
                remote_pseudo.add(
                    posixpath.normpath(
                        posixpath.join(opf_dir, unquote(href))
                    )
                )
            # href may carry URL encoding (%20 etc.); zip member
            # names are the decoded paths
            href = unquote(href)
            manifest[item_id] = {
                # href is relative to the OPF's directory; use
                # posix absolute paths inside the zip
                "href": posixpath.normpath(
                    posixpath.join(opf_dir, href)
                ),
                "media_type": item.get("media-type") or "",
                "properties": item.get("properties") or "",
            }
        remote_facts = {
            "count": remote_total,
            "samples": remote_samples,
            "pseudo_hrefs": remote_pseudo,
        }
        return manifest, remote_facts

    # ========================================================
    # navigation / image evidence (facts only, never narrative)
    # ========================================================

    @staticmethod
    def _navigation_evidence(zf, names, manifest):
        """TOC navigation facts: EPUB3 nav document first, EPUB2
        NCX as fallback.

        TOC entries are counted and sampled only; the reading
        order is never rewritten (the spine is the fact source
        for body order).
        """
        nav_item = None
        ncx_item = None
        for item in manifest.values():
            if "nav" in (item.get("properties") or "").split():
                nav_item = item
            if item["media_type"] == "application/x-dtbncx+xml":
                ncx_item = item
        target = nav_item or ncx_item
        kind = "nav" if nav_item else ("ncx" if ncx_item else None)
        if target is None or target["href"] not in names:
            return {
                "extractor": "epub_navigation_v1",
                "kind": kind,
                "path": target["href"] if target else None,
                "toc_entry_count": 0,
                "sample_titles": [],
                "note": (
                    "no readable nav/NCX document declared in the "
                    "manifest"
                ),
            }
        try:
            root = EpubParser._parse_xml(zf.read(target["href"]))
        except Exception as e:
            return {
                "extractor": "epub_navigation_v1",
                "kind": kind,
                "path": target["href"],
                "toc_entry_count": 0,
                "sample_titles": [],
                "error": f"{type(e).__name__}: {e}",
            }
        titles = []
        doc_title = None
        targets = []
        if kind == "nav":
            # EPUB3: anchor texts inside <nav epub:type="toc">
            for elem in root.iter():
                if _localname(elem.tag) != "a":
                    continue
                text = (elem.text or "").strip()
                if text:
                    titles.append(text)
        else:
            # EPUB2 NCX: navLabel/text
            for label in root.iter():
                if _localname(label.tag) != "navLabel":
                    continue
                for node in label.iter():
                    if _localname(node.tag) == "text":
                        text = (node.text or "").strip()
                        if text:
                            titles.append(text)
                        break
            # docTitle/text book-title declaration verbatim (no
            # value invented when missing); navPoint/content src
            # target sampling (the spine stays the fact source
            # for reading order; this registers the navigation's
            # own declarations only).
            for elem in root.iter():
                local = _localname(elem.tag)
                if local == "docTitle":
                    for node in elem.iter():
                        if _localname(node.tag) == "text":
                            text = (node.text or "").strip()
                            if text:
                                doc_title = text
                            break
                elif local == "content":
                    src = (elem.get("src") or "").strip()
                    if src:
                        targets.append(src)
        result = {
            "extractor": "epub_navigation_v1",
            "kind": kind,
            "path": target["href"],
            "toc_entry_count": len(titles),
            "sample_titles": titles[:_MAX_SAMPLES],
            "note": (
                "toc entries are counted and sampled as facts; the "
                "reading order of the narrative stays the spine order"
            ),
        }
        # Non-empty writes only; a clean file gets no new keys.
        if doc_title:
            result["doc_title"] = doc_title
        if targets:
            result["sample_targets"] = targets[:_MAX_SAMPLES]
        return result

    @staticmethod
    def _image_evidence(manifest):
        """Manifest-declared image asset facts: counts + type
        distribution + samples."""
        images = [
            item for item in manifest.values()
            if item["media_type"].startswith("image/")
        ]
        type_counts = {}
        for item in images:
            type_counts[item["media_type"]] = (
                type_counts.get(item["media_type"], 0) + 1
            )
        return {
            "extractor": "epub_images_v1",
            "image_count": len(images),
            "media_type_counts": type_counts,
            "sample_hrefs": [
                item["href"] for item in images[:_MAX_SAMPLES]
            ],
            "note": (
                "manifest-declared image assets; the bytes stay in the "
                "package (facts only, no extraction)"
            ),
        }

    @staticmethod
    def _opf_spine(opf_root):
        ids = []
        for itemref in opf_root.iter(f"{{{_OPF_NS}}}itemref"):
            idref = itemref.get("idref")
            if idref:
                ids.append(idref)
        return ids

    @staticmethod
    def _opf_spine_facts(opf_root, spine_ids):
        """Spine attribute reads: linear="no" count / duplicate
        idref count / page-progression-direction raw value.
        Registration only — no verdict is ever derived from
        these.
        """
        linear_no = 0
        ppd = None
        spine_elem = None
        for elem in opf_root.iter():
            if _localname(elem.tag) == "spine":
                spine_elem = elem
                break
        if spine_elem is not None:
            ppd = spine_elem.get("page-progression-direction")
            for itemref in spine_elem:
                if _localname(itemref.tag) != "itemref":
                    continue
                if (
                    itemref.get("linear") or ""
                ).strip().lower() == "no":
                    linear_no += 1
        duplicate_idref = len(spine_ids) - len(set(spine_ids))
        return linear_no, duplicate_idref, ppd

    @staticmethod
    def _cover_evidence(opf_root, manifest, opf_dir, names):
        """Cover registration: three-tier priority manifest
        properties="cover-image" > meta name="cover"
        (content=manifest id → href mapping) > guide reference
        (a zero-hit tier, kept as a fallback implementation).
        No filename inference (no invented values); the cover
        never enters the body (a cover-image is an image manifest
        item, not spine XHTML).
        """
        asset_ref = None
        source = None
        # Tier 1: manifest item properties="cover-image"
        for item in manifest.values():
            if "cover-image" in (item["properties"] or "").split():
                asset_ref = item["href"]
                source = "manifest_properties"
                break
        # Tier 2: EPUB2 <meta name="cover" content="manifest-id"/>
        if asset_ref is None:
            for elem in opf_root.iter():
                if _localname(elem.tag) != "meta":
                    continue
                if (elem.get("name") or "") != "cover":
                    continue
                item = manifest.get(elem.get("content") or "")
                if item is not None:
                    asset_ref = item["href"]
                    source = "meta_name_cover"
                    break
        # Tier 3: guide <reference type="cover" href="..."/>
        # (href may carry a #fragment; strip it, then normalize
        # against the OPF directory)
        if asset_ref is None:
            for elem in opf_root.iter():
                if _localname(elem.tag) != "reference":
                    continue
                if (elem.get("type") or "") != "cover":
                    continue
                href = elem.get("href") or ""
                if href:
                    asset_ref = posixpath.normpath(
                        posixpath.join(
                            opf_dir, unquote(href.split("#")[0])
                        )
                    )
                    source = "guide_reference"
                    break
        if asset_ref is None:
            return {"asset_ref": None, "source": None, "resolved": False}
        return {
            "asset_ref": asset_ref,
            "source": source,
            "resolved": asset_ref in names,
        }

    @staticmethod
    def _content_census(zf, hrefs, names):
        """Spine XHTML content census: same single-pass read over
        raw bytes as the link census, extended to content-layer
        semantic facts — table structure / CSS references /
        accessibility attributes / language and direction / SVG /
        structural block aggregate counts + fragment existence
        validation (same-document href="#f" belongs to the
        current document; a missing target or unknown id counts
        as broken; an unreadable target document cannot prove
        missing-ness, so it is not counted).  Also counts the
        object/embed/iframe embedding carriers and inline event
        attributes (script precedent — content never taken,
        never executed).  All best-effort: CRC-failed members
        are skipped defensively when read; no part in tri-state
        derivation.
        """
        internal = 0
        external = 0
        samples = []
        # <script> is silently skipped by the render layer
        # (correct: script text is not narrative), but its
        # presence itself is a security fact — registered in this
        # census channel; silent omission would be a gap.
        scripts = 0
        # Non-script embedding carriers + inline event attributes
        # (same tier as script)
        objects = embeds = iframes = event_handlers = 0
        tables = rows = cells = header_cells = 0
        rowspans = colspans = 0
        stylesheet_links = style_attrs = 0
        # url() references inside inline style attributes —
        # existence counting
        inline_style_urls = 0
        imgs = alt_nonempty = 0
        aria_attrs = role_attrs = 0
        role_values = {}
        lang_values = {}
        xml_langs = 0
        rtl = 0
        svgs = svg_texts = 0
        blockquotes = figures = figcaptions = 0
        autoplays = 0
        # HTML comment census (full true count + bounded body
        # value samples)
        html_comments = 0
        html_comment_samples = []
        doc_ids = {}
        frag_links = []
        for href in hrefs:
            try:
                data = zf.read(href)
            except Exception:
                continue
            base_dir = posixpath.dirname(href)
            for match in _ANCHOR_HREF_RE.finditer(data):
                value = (
                    match.group(1) or match.group(2) or b""
                ).decode("utf-8", "replace")
                if not value:
                    continue
                if _URL_SCHEME_RE.match(value):
                    external += 1
                else:
                    internal += 1
                    # fragment collection: validation is deferred
                    # until all ids are gathered
                    path_part, _, frag = value.partition("#")
                    if frag:
                        if path_part:
                            target = posixpath.normpath(
                                posixpath.join(
                                    base_dir, unquote(path_part)
                                )
                            )
                        else:
                            target = href
                        frag_links.append((target, unquote(frag)))
                if len(samples) < _MAX_SAMPLES:
                    samples.append(value)
            tables += len(_TABLE_RE.findall(data))
            rows += len(_TABLE_ROW_RE.findall(data))
            cells += len(_TABLE_CELL_RE.findall(data))
            header_cells += len(_TABLE_HEADER_CELL_RE.findall(data))
            rowspans += len(_ROWSPAN_RE.findall(data))
            colspans += len(_COLSPAN_RE.findall(data))
            stylesheet_links += len(_STYLESHEET_LINK_RE.findall(data))
            style_attrs += len(_STYLE_ATTR_RE.findall(data))
            inline_style_urls += len(
                _INLINE_STYLE_URL_RE.findall(data)
            )
            imgs += len(_IMG_RE.findall(data))
            alt_nonempty += len(_IMG_ALT_RE.findall(data))
            aria_attrs += len(_ARIA_ATTR_RE.findall(data))
            for match in _ROLE_VALUE_RE.finditer(data):
                role_attrs += 1
                _bump_bounded(
                    role_values,
                    match.group(1).decode("utf-8", "replace"),
                )
            for match in _HTML_LANG_RE.finditer(data):
                _bump_bounded(
                    lang_values,
                    match.group(1).decode("utf-8", "replace"),
                )
            xml_langs += len(_XML_LANG_RE.findall(data))
            rtl += len(_DIR_RTL_RE.findall(data))
            svgs += len(_SVG_RE.findall(data))
            scripts += len(_SCRIPT_TAG_RE.findall(data))
            objects += len(_OBJECT_TAG_RE.findall(data))
            embeds += len(_EMBED_TAG_RE.findall(data))
            iframes += len(_IFRAME_TAG_RE.findall(data))
            event_handlers += len(_EVENT_ATTR_RE.findall(data))
            # audio/video autoplay attribute counting
            # (auto-trigger surface)
            for tag in _AUDIO_VIDEO_TAG_RE.findall(data):
                if b"autoplay" in tag.lower():
                    autoplays += 1
            svg_texts += len(_SVG_TEXT_RE.findall(data))
            blockquotes += len(_BLOCKQUOTE_RE.findall(data))
            figures += len(_FIGURE_RE.findall(data))
            figcaptions += len(_FIGCAPTION_RE.findall(data))
            # HTML comment body value samples (empty bodies count
            # but are not sampled)
            for match in _HTML_COMMENT_RE.finditer(data):
                html_comments += 1
                body = match.group(1).decode("utf-8", "replace").strip()
                if not body:
                    continue
                if len(html_comment_samples) < _MAX_SAMPLES:
                    html_comment_samples.append(body[:_COMMENT_SNIPPET])
            doc_ids[href] = {
                m.group(1).decode("utf-8", "replace")
                for m in _ID_VALUE_RE.finditer(data)
            }
        # Fragment existence validation: in-spine targets use the
        # collected id sets; out-of-spine manifest documents are
        # read once on demand (unreadable → not counted broken)
        broken = 0
        broken_samples = []
        for target, frag in frag_links:
            ids = doc_ids.get(target)
            if ids is None:
                if target not in names:
                    broken += 1
                    if len(broken_samples) < _MAX_SAMPLES:
                        broken_samples.append(f"{target}#{frag}")
                    continue
                try:
                    tdata = zf.read(target)
                except Exception:
                    continue
                ids = {
                    m.group(1).decode("utf-8", "replace")
                    for m in _ID_VALUE_RE.finditer(tdata)
                }
            if frag not in ids:
                broken += 1
                if len(broken_samples) < _MAX_SAMPLES:
                    broken_samples.append(f"{target}#{frag}")
        return {
            "internal_link_count": internal,
            "external_link_count": external,
            "link_href_samples": samples,
            "table_count": tables,
            "table_row_count": rows,
            "table_cell_count": cells,
            "table_header_cell_count": header_cells,
            "rowspan_count": rowspans,
            "colspan_count": colspans,
            "stylesheet_link_count": stylesheet_links,
            "style_attribute_count": style_attrs,
            "inline_style_url_count": inline_style_urls,
            "image_element_count": imgs,
            "image_alt_nonempty_count": alt_nonempty,
            "aria_attribute_count": aria_attrs,
            "role_attribute_count": role_attrs,
            "role_value_counts": role_values,
            "html_lang_counts": lang_values,
            "xml_lang_attribute_count": xml_langs,
            "rtl_direction_count": rtl,
            "fragment_link_count": len(frag_links),
            "broken_fragment_count": broken,
            "broken_fragment_samples": broken_samples,
            "svg_element_count": svgs,
            "script_element_count": scripts,
            "object_element_count": objects,
            "embed_element_count": embeds,
            "iframe_element_count": iframes,
            "event_handler_attribute_count": event_handlers,
            "autoplay_attribute_count": autoplays,
            "svg_inline_text_count": svg_texts,
            "blockquote_count": blockquotes,
            "figure_count": figures,
            "figcaption_count": figcaptions,
            "html_comment_count": html_comments,
            "html_comment_samples": html_comment_samples,
        }

    @staticmethod
    def _css_facts(zf, manifest, names):
        """Manifest-declared CSS asset facts: file count / byte
        totals + rule-layer semantic counts (display:none /
        visibility:hidden / generated content / @media /
        @font-face).  The CSS external-reference surface (@import
        / url() totals, remote scheme-bearing targets counted
        separately + bounded samples) is registered alongside —
        counting and registration only, never fetched.
        """
        file_count = 0
        total_bytes = 0
        display_none = 0
        visibility_hidden = 0
        generated = 0
        media_queries = 0
        font_faces = 0
        # External-reference surface (remote = scheme-bearing,
        # never fetched)
        imports = 0
        url_refs = 0
        remote_refs = 0
        remote_samples = []
        for item in manifest.values():
            if item["media_type"] != "text/css":
                continue
            if item["href"] not in names:
                continue
            try:
                data = zf.read(item["href"])
            except Exception:
                continue
            file_count += 1
            total_bytes += len(data)
            display_none += len(_CSS_DISPLAY_NONE_RE.findall(data))
            visibility_hidden += len(
                _CSS_VISIBILITY_HIDDEN_RE.findall(data)
            )
            generated += len(_CSS_GENERATED_RE.findall(data))
            media_queries += len(_CSS_MEDIA_QUERY_RE.findall(data))
            font_faces += len(_CSS_FONT_FACE_RE.findall(data))
            # @import / url() counting + separate remote targets
            imports += len(_CSS_IMPORT_RE.findall(data))
            url_refs += len(_CSS_URL_RE.findall(data))
            remote_hits = [
                m.group(1).decode("ascii", "replace")
                for m in _CSS_REMOTE_URL_RE.finditer(data)
            ] + [
                m.group(1).decode("ascii", "replace")
                for m in _CSS_REMOTE_IMPORT_RE.finditer(data)
            ]
            remote_refs += len(remote_hits)
            for value in remote_hits:
                if len(remote_samples) < _MAX_SAMPLES:
                    remote_samples.append(value)
        return {
            "css_file_count": file_count,
            "css_total_bytes": total_bytes,
            "css_display_none_count": display_none,
            "css_visibility_hidden_count": visibility_hidden,
            "css_generated_content_count": generated,
            "css_media_query_count": media_queries,
            "css_font_face_count": font_faces,
            "css_import_count": imports,
            "css_url_reference_count": url_refs,
            "css_remote_reference_count": remote_refs,
            "css_remote_reference_samples": remote_samples,
        }

    # ========================================================
    # XML / chapter
    # ========================================================

    @staticmethod
    def _parse_xml(data):
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError:
            # Retry once with the entity whitelist cleaned
            # (undeclared entities are common in XHTML)
            cleaned = _ENTITY_RE.sub(
                lambda m: _HTML_ENTITIES[m.group(1)], data.decode(
                    "utf-8", "replace"
                ),
            ).encode("utf-8")
            return ElementTree.fromstring(cleaned)

    @staticmethod
    def _parse_chapter(data):
        """Parse one XHTML chapter → ChapterRenderer."""
        root = EpubParser._parse_xml(data)
        body = None
        for elem in root.iter():
            if _localname(elem.tag) == "body":
                body = elem
                break
        if body is None:
            body = root
        return _ChapterRenderer(body)


class _ChapterRenderer:
    """A chapter's block-level rendering result (written out
    once after lines are collected)."""

    def __init__(self, body):
        """Initialize with the chapter body element."""
        self._body = body

    def render(self, lines, headings):
        """Append the chapter's block-level rendering to the
        (text, tag) line list."""
        _render_blocks(self._body, lines, headings)


def _render_blocks(elem, lines, headings):
    """Block-level walk → (text, tag) line list; an empty string
    line acts as a paragraph separator."""
    for child in elem:
        name = _localname(child.tag)
        if name in _SKIP_TAGS:
            continue
        if name in _HEADING_TAGS:
            title = _inline_text(child)
            if not title:
                continue
            level = int(name[1])
            rendered = "#" * level + " " + title
            headings.append(
                HeadingCandidate(
                    raw=title,
                    pattern="markdown",
                    depth_hint=level,
                    start=_offset_of(lines),
                    end=_offset_of(lines) + len(rendered),
                )
            )
            lines.append((rendered, name))
            lines.append(("", ""))
        elif name == "p":
            para = _inline_text(child)
            if para:
                lines.append((para, name))
            lines.append(("", ""))
        elif name in ("ul", "ol"):
            _render_list(child, lines, 0)
            lines.append(("", ""))
        elif name == "table":
            _render_table(child, lines)
            lines.append(("", ""))
        elif name == "blockquote":
            quote = _inline_text(child)
            if quote:
                lines.append(("> " + quote, name))
            lines.append(("", ""))
        elif name == "pre":
            block = _inline_text(child)
            if block:
                lines.append(("```", name))
                lines.append((block, name))
                lines.append(("```", name))
            lines.append(("", ""))
        elif name == "img":
            src = child.get("src") or ""
            alt = child.get("alt") or ""
            label = alt if alt else src
            lines.append((f"[image: {label}]", name))
            lines.append(("", ""))
        elif name in ("figure", "section", "article", "div", "header",
                      "footer", "main", "aside", "nav"):
            # Container elements: recurse down, no own text
            _render_blocks(child, lines, headings)
        else:
            # Other inline elements land in this layer's text flow
            text = _inline_text(child)
            if text:
                lines.append((text, name))
                lines.append(("", ""))


def _render_list(elem, lines, depth):
    ordered = _localname(elem.tag) == "ol"
    index = 0
    for child in elem:
        name = _localname(child.tag)
        if name != "li":
            continue
        index += 1
        item_text = _inline_text(child)
        marker = f"{index}." if ordered else "-"
        lines.append(("  " * depth + f"{marker} " + item_text, "li"))
        for sub in child:
            subname = _localname(sub.tag)
            if subname in ("ul", "ol"):
                _render_list(sub, lines, depth + 1)


def _render_table(table, lines):
    rows = []
    for tr in table.iter():
        if _localname(tr.tag) != "tr":
            continue
        cells = []
        for cell in tr:
            cell_name = _localname(cell.tag)
            if cell_name in ("td", "th"):
                value = _inline_text(cell).replace("|", "\\|")
                cells.append(value)
        if any(c.strip() for c in cells):
            rows.append(cells)
    # The rendering core (monospace padding + separator line) is
    # shared with pptx in narrative/shared.py
    for i, line in enumerate(_markdown_table_lines(rows)):
        lines.append((line, "hr" if i == 1 else "tr"))


def _inline_text(elem):
    """Text inside an element: block-level children collapse to
    spaces, img records a placeholder name."""
    parts = []

    def walk(node):
        """Recursively collect inline text; img records a
        placeholder name, br collapses to a space."""
        name = _localname(node.tag)
        if name in _SKIP_TAGS:
            return
        if name == "img":
            alt = node.get("alt") or node.get("src") or ""
            parts.append(f"[image: {alt}]")
            return
        if name == "br":
            parts.append(" ")
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(elem)
    # Conservative whitespace contract: only newlines collapse
    # to spaces; other unicode whitespace is left untouched
    # (nbsp etc. are document facts).
    return "".join(parts).replace("\n", " ").strip()


def _offset_of(lines):
    """Cumulative character offset of the current line list (for
    coarse positioning)."""
    return sum(len(t) + 1 for t, _ in lines)
