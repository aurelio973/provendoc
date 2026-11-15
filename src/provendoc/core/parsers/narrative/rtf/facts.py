# ============================================================
# Narrative Domain — RTF fact-registration layer (facts)
# ============================================================
# 2026-11-10 mechanical split from rtf_parser.py (zero per-line changes). This module holds
# only pure data fact tables: evidence vocabularies, destination three-way classification, sampling caps, codepage aliases,
# and \\info key tables. Contract sources and prereg numbers: see the rtf_parser.py header comments.
# Dependency direction: scanner / adapters -> facts (one-way; this module has zero back-dependencies).

from typing import Tuple

# RTF header (after BOM stripping the source must start with this, otherwise honest failure).
_RTF_HEAD = "{\\rtf"

# D12 A: the evidence_type vocabulary for this family. native_rtf = pure-stdlib state machine
# reading the source directly; rendered_conversion is a reserved slot — if users later supply
# LibreOffice/Word conversion products, they are only registered as visual/recovery corroboration (kept
# separate from the native evidence, never overwriting). This chain introduces no external conversion dependency.
_EVIDENCE_TYPE_VOCAB = frozenset({"native_rtf", "rendered_conversion"})

# ------------------------------------------------------------
# D10 destination three-way classification (rtf_family_prereg_v1 §2b #2)
# ------------------------------------------------------------
# Structure destinations: collected and parsed in-group for registration (no longer pure skip-counting).
_STRUCTURE_DESTINATIONS = frozenset({
    "fonttbl", "colortbl", "info",
})

# Resource destinations: hex collected in-group and decoded into byte assets (object counted only;
# OLE extraction belongs to P1 — registered, not executed).
_RESOURCE_DESTINATIONS = frozenset({
    "pict",
})

# P1 view destinations (D16/D17/D20): text collected in-group into the matching view/registration
# channel (never into the main body). kinds: _HEADER_FOOTER_KINDS.
_VIEW_DESTINATIONS = frozenset({
    "header", "headerl", "headerr", "headerf",
    "footer", "footerl", "footerr", "footerf",
    "footnote",
})

# P1 structure-registration destinations (D14/D15/D20): parsed and registered in-group.
_STRUCTURE_DESTINATIONS_V2 = frozenset({
    "stylesheet", "listtable", "listoverridetable", "object",
})

# The rest keep v1 semantics: whole-group skip, counted by name (existence registration is the evidence boundary).
# "generator" keeps the v1 ordinary-destination counting semantics (\*\generator goes through the * branch's
# dedicated collection, no conflict).
_SKIP_DESTINATIONS = frozenset({
    "generator",
    "listlevel", "listtext",
    "levelnumbers", "filetbl",
    "xmlnstbl", "rsidtbl", "latentstyles", "datastore", "themedata",
    "colorschememapping", "shpinst", "shpict", "mmathPr",
    "wgrffmtfilter", "panose", "nonshppict", "template", "fchars",
    "lchars", "ftnsep", "ftnsepc", "ftncn", "aftnsep", "aftnsepc",
    "aftncn", "pgdsctbl", "revtbl",
})

# Known ignorable \*\word destinations (not counted as unknown; \*\ud and \*\generator
# have dedicated v2 collection semantics, \*\fldinst keeps the v1 skip + observation).
# P1 changes: \*\annotation upgraded to collection (annotation_view), \*\objclass upgraded to
# collection (objclass names); both moved out of this table (v1 had counted objclass as
# ignored and annotation as unknown — v2 changes it honestly as the semantics arrive; the test
# anchors are synced).
_KNOWN_IGNORED_DESTS = frozenset({
    "ud", "generator", "fldinst", "shppict", "nonshppict",
    "datafield", "background", "themedata", "colorschememapping",
    # r25 (4): rsidtbl upgraded to collection (moved out of this table per the objclass/annotation precedent)
    "latentstyles", "xmlnstbl", "panose", "blipuid",
    "keycode", "company", "manager", "dxfrun", "tenant",
    "objdata", "objname", "objalias", "objsect",
    # r25 (4): revtbl upgraded to collection (moved out of this table)
    "objtime", "wzgpos", "wzgnam",
})

# Control symbols (non-letter symbols) → output mapping; unlisted ones are silently swallowed.
_SYMBOL_OUTPUT = {
    "~": "\u00a0",   # non-breaking space
}

# Fixed-semantics control words → visible characters (defined by the RTF spec, not guesses):
# silently swallowing them would lose visible body punctuation.
_WORD_OUTPUT = {
    "emdash": "\u2014",    # —
    "endash": "\u2013",     # –
    "lquote": "\u2018",     # '
    "rquote": "\u2019",     # '
    "ldblquote": "\u201c",  # "
    "rdblquote": "\u201d",  # "
    "bullet": "\u2022",     # •
}

# Tail stripped after output (trailing \par noise never enters the body).
_TRAILING_NL = "\n"

# ------------------------------------------------------------
# v2 collection caps (anti-blowup; overflows registered as-is; total counts uncapped)
# ------------------------------------------------------------
_MAX_TABLE_ROWS = 400          # row-structure registration cap (table_rows_total is a full count)
_MAX_RUNS_HEAD = 50            # run sample count (runs_total is a full count)
_MAX_FONT_ENTRIES = 64
_MAX_COLOR_ENTRIES = 256
_MAX_PARAGRAPH_OFFSETS = 1000
_MAX_PICT_RECORDS = 100
_MAX_PICT_HEX_CHARS = 4_000_000  # per-image hex character cap (~2 MB of bytes)

# ------------------------------------------------------------
# P1/P2 collection caps (rtf_family_prereg_v2)
# ------------------------------------------------------------
_MAX_STYLE_ENTRIES = 128       # D14 style-entry registration cap
_MAX_LIST_ENTRIES = 64         # D15 listtable entries
_MAX_LIST_OVERRIDE_ENTRIES = 64
_MAX_LIST_LEVELS = 32           # levels per list
_MAX_BOOKMARK_NAMES = 128      # D17 bookmark-name registration cap
_MAX_BOOKMARK_NAME_CHARS = 64
_MAX_FIELDS = 20                # D17 field sample count
_MAX_FIELD_INSTRUCTION_CHARS = 80
_MAX_FOOTNOTES = 100            # D16 footnote registration cap
_MAX_VIEW_TEXT_CHARS = 20_000   # per-view text cap (overflow truncated and registered)
_MAX_EMBEDDED_OBJECTS = 50      # D20 embedded-object registration cap
_MAX_OBJDATA_HEX_CHARS = 8_000_000  # per-object hex cap (~4 MB)
_MAX_HEADING_CANDIDATES = 50    # D14 heading-candidate cap
_MAX_PARAGRAPH_SAMPLES = 200    # D14 paragraph samples
_MAX_PARAGRAPH_TEXT_CHARS = 200
_MAX_NESTED_RTF_TEXT_CHARS = 200  # D20 nested-RTF body-head registration

# \pict image-format control words → canonical format labels (fixed RTF-spec semantics).
_PICT_FORMAT_WORDS = {
    "pngblip": "png",
    "jpegblip": "jpeg",
    "emfblip": "emf",
    "wmetafile": "wmf",
    "dibitmap": "dib",
    "bmpblip": "bmp",
}

# Byte magic numbers (image self-description; verified after decoding, never guessed).
_PICT_MAGICS: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF8", "gif"),
    (b"\xd7\xcd\xc6\x9a", "wmf"),
    (b"\x01\x00\x00\x00", "emf"),
    (b"BM", "bmp"),
)

# \info text keys (RTF-spec defined; the time group goes through raw records separately).
_INFO_TEXT_KEYS = frozenset({
    "title", "author", "subject", "keywords", "operator",
    "company", "manager", "comment", "doccomm",
})
_INFO_TIME_KEYS = frozenset({
    "creatim", "revtim", "printim", "buptim",
})

# r25 (9): \info integer scalar keys (editing minutes/pages/words/characters;
# the source is decimal integers, verbatim, no casting, no inference).
# r26 (5): +nofcharsws (characters excluding spaces; briefly lost during the split migration,
# restored 2026-11-11).
_INFO_SCALAR_KEYS = frozenset({
    "edmins", "nofpages", "nofwords", "nofchars", "nofcharsws",
})

# r25 sample caps (counts keep true values, samples capped — same shape as the docx rsids census).
_MAX_REVISION_AUTHOR_SAMPLES = 50
_MAX_RSID_SAMPLES = 50
_MAX_FONT_ALIAS_SAMPLES = 50

# fonttbl font-family control words (RTF spec).
_FONT_FAMILY_WORDS = frozenset({
    "froman", "fswiss", "fmodern", "fscript", "fdecor", "ftech",
    "fbidi", "fnil",
})

# D6 character-format control words → run state keys.
_RUN_FORMAT_WORDS = {
    "b": "bold", "i": "italic", "ul": "underline",
    "strike": "strike", "v": "hidden",
    "super": "super", "sub": "sub",
}


# Mac codepage alias table (d6 double fix, tex_math_unicode_prereg_v1 P2-1):
# the RTF \ansicpg1000N series declares Macintosh codepages; Python has no corresponding
# "cp100NN" encodings — direct concatenation would LookupError and fall back to cp1252 (the main culprit
# of the RTF-04 Mac-Japanese mojibake). 10001 (Mac Japanese) tested to decode correctly with strict cp932
# ("シンセサーパー構造…", S5 probe evidence; Python has no mac_japan);
# Chinese/Korean families map likewise to the matching Windows codepages; Latin families map to mac_* codecs
# (skipped item by item when absent; the final fallback is still cp1252 + the existing disclosure path).
_MAC_CODEPAGE_ALIASES = {
    10000: ("mac_roman",),          # Mac Latin
    10001: ("cp932",),              # Mac Japanese → cp932 (S5 tested)
    10002: ("cp950",),              # Mac Traditional Chinese
    10003: ("cp949",),              # Mac Korean
    10004: ("mac_arabic",),         # Mac Arabic
    10005: ("mac_hebrew",),         # Mac Hebrew
    10006: ("mac_greek",),          # Mac Greek
    10007: ("mac_cyrillic",),       # Mac Cyrillic
    10008: ("cp936",),              # Mac Simplified Chinese
}
