"""Borderless table evidence from text-alignment geometry.

Ruled-table detection (``NativeTableExtractor``) requires vector rules, but
business documents (invoices, purchase orders) overwhelmingly use borderless
line-item tables separated only by whitespace.  This extractor records the
complementary evidence: it groups the native text layer into visual lines,
splits lines into cells on large horizontal gaps, and keeps only regions
whose cell starts realign on a shared set of column anchors across rows.

Multi-column pages (two-column academic articles) get one structural pass
before line clustering: persistent vertical whitespace gutters are detected
from word x-coverage, and a cluster that spans a gutter is split at it, so
parallel text columns never merge into one full-width "line" (measured on
PubTables-1M: cross-column merging was both the top false-positive source —
detections absorbing a whole text column — and the top miss source —
table rows polluted by the neighbouring column's prose).

Everything stays evidence-first:

* a region is recorded only when its geometry shows column realignment —
  the extractor never claims a table from content semantics;
* column anchors, row counts and totals-row labels are recorded as facts;
  cells are never re-flowed and totals are never recomputed;
* detection confidence is honest: alignment evidence is weaker than ruled
  evidence, so blocks carry a lower confidence and a distinct
  ``source_kind`` (``borderless_table``).

Documents without a native text layer (scanned) can be processed by
``extract_with_ocr``: alignment evidence from OCR word coordinates is
weaker than native evidence, so it is recorded with a lower confidence
and a distinct ``source_kind`` (``borderless_table_ocr``), and OCR noise
words (skew artifacts with neither a 3-letter run, any digit, nor a
2-ideograph CJK/kana run) are dropped before alignment — measured on
business-records (research) (eng) before this path was trusted, and on a synthetic
chi_sim scanned table for the multilingual generalization (see
scripts/evaluate_ocr_alignment_quality.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFPage,
)
from provendoc.core.parsers.narrative.pdf.evidence.ocr import OCRBackendUnavailable

# Totals / subtotals row labels (first cell), shared with ruled-table
# evidence.  Recorded as evidence only — sums are never recomputed.
_TOTAL_ROW_RE = re.compile(
    r"^(?:合计|小计|总计|累计|结转|total|subtotal|grand\s*total|sum)\b",
    re.IGNORECASE,
)
_NUMERIC_CELL_RE = re.compile(r"[\d]")
# A cell that is *entirely* a number/currency/quantity value ("12", "$10.00",
# "1,200.00", "-3") — as opposed to prose that merely contains digits
# (". N° 30002", "Bank Branch 2").
_NUMERIC_VALUE_RE = re.compile(r"^[$€£¥]?\s*\(?-?[\d][\d.,\s]*\)?$")
# Scientific-shape data values: ranges and spread statistics printed as
# "28(20–56)" (median (range)), "45.2±3.1", "20–91", "24(23.8%)"
# (count (share)) — the lexical shape of an academic data column.  The
# parenthesised form additionally tolerates "~"-ranges and percent signs
# ("13(5 ~ 23%)"), the shape of every clinical cohort table; prose that
# merely contains digits ("Age (years)", "N = 252", "Patient(n = 109)")
# never matches — a leading alphabetic character or a letter inside the
# parentheses breaks the pattern (measured on PubTables-1M, where the
# strict pattern left a genuine 2-column patient table at 5/21 numeric
# cells and the two-column guard rejected it; with the tolerance the same
# column reads 19/21).
_NUMERIC_RANGE_RE = re.compile(
    r"^[-+±]?\d[\d.,\s]*%?(?:[±×–—/~]\s*[-+±]?\d[\d.,\s]*%?)*"
    r"(?:\(\s*[-+±]?\d[\d.,\s%]*[±×–—/~]?[\d.,\s%]*\s*\))?$"
)

_MIN_CELLS_PER_ROW = 2          # a table-like line has at least 2 columns
_MIN_ROWS_PER_REGION = 2        # a region realigns across at least 2 rows
_MIN_COLUMN_ANCHORS = 3         # and shares at least 3 column anchors
_MAX_REGION_BREAK = 2          # tolerated non-candidate line inside a region
_ANCHOR_CLUSTER_TOLERANCE = 6.0  # pt — cell edges within this join an anchor
_ANCHOR_MIN_ROW_SHARE = 0.35    # anchor must appear in >= 35% of kept rows
_GAP_FACTOR = 1.2              # cell break = gap > factor * line height
_MIN_GAP_POINTS = 10.0         # absolute floor for a cell break (pt)
_MIN_ROW_ALIGNMENT = 2         # kept rows realign on >= 2 shared anchors
# Consecutive table-like lines further apart than this start a new region —
# a line-item table's rows are evenly spaced, so a large vertical break is
# a different table (e.g. the totals block at the page bottom), not the
# same region tolerating noise.
_MAX_REGION_GAP_FACTOR = 3.0   # gap > factor * median line height
_MIN_REGION_GAP_POINTS = 25.0  # absolute floor for a region break (pt)
# A column that is >= this share pure numeric values (quantities,
# amounts) is the lexical signature of a data column.  Regions with one
# keep the base confidence; regions without any are stratified down —
# alignment alone is necessary but not sufficient evidence of table
# semantics (invoice headers and letterheads align too).
_NUMERIC_COLUMN_SHARE = 0.5
_NO_NUMERIC_COLUMN_STRATUM = 0.2  # confidence subtracted without a data column
_MAX_TABLE_SAMPLES = 20
# OCR noise words (skew artifacts like "—-", "@", "oO", "曰") carry neither
# a 3-letter run, any digit, nor a 2-ideograph CJK/kana run; genuine table
# values always carry one of the three (Chinese/Japanese cell words are
# 2+ ideographs — a single ideograph is the chi_sim analogue of "oO"/"tn"
# single-glyph noise).  Applied to OCR word sources only — native words
# are real rendered text and never pass through this filter.  Hangul
# syllables are not covered: no local traineddata/eval corpus for kor,
# so that script system stays on the eng-calibrated rule until one exists.
_OCR_NOISE_SURVIVES_RE = re.compile(
    r"[A-Za-z]{3,}|\d|[\u3400-\u9fff\u3040-\u30ff]{2,}"
)
_OCR_CONFIDENCE = 0.45  # OCR alignment evidence is weaker than native
# --- page gutter detection (multi-column layouts) -------------------------
# A page gutter is a vertical whitespace strip that stays word-free over
# almost the whole page's text height AND is actually crossed by parallel
# text lines — measured on business-records (research) invoices and PubTables-1M articles:
# invoice column gaps cover 5-15% of a sparse page's text height while a
# true academic gutter stays essentially uncovered, so the covered budget
# is capped in points as well as in share; the width window (>= 15pt,
# <= 40pt) keeps out narrow table gaps and empty page halves; and a strip
# nothing spans is not splitting anything and is dropped.
# A final guard keeps table column gaps out: the gutters must cut the
# page's text extent into bands that are each wide enough to be a text
# column — at least a fifth of the text width AND at least 100pt (the
# absolute floor matters on cropped/narrow pages, where a share of a
# small width is small too).  A wide borderless table's own column gaps
# pass every strip-level test (26 rows of cells clear the gap and span
# it), but they slice the table into narrow cell columns (28-58pt bands
# on a 580pt-wide business-records (research) invoice page, 38pt on a 172pt-wide cropped
# SciTSR table) — a genuine text column is never that narrow (two-column
# ~250pt, three-column ~165pt).  One narrow band rejects the whole page's
# gutter set (back to the single-column pipeline), never a partial split
# that would shred a real table.
# Finally, a real page gutter is a typographic constant: text columns
# divide the text width into EQUAL shares (two-column 0.5, three-column
# 1/3+2/3), measured on PubTables-1M two/three-column pages at within
# 0.02 of the exact fractions.  A table's internal column gap sits
# wherever its column widths put it (0.39-0.70 normalized on the SciTSR
# whole-page-table crops), so a strip off the equal-split positions is a
# table gap, not a layout gutter.  Non-equal column splits (60/40
# magazine layouts) would be missed — not observed in the corpora and
# recorded as an accepted blind spot; the fallback is the single-column
# pipeline, i.e. the calibrated baseline behaviour.
_GUTTER_SCAN_BIN = 2.0                # pt, x-scan resolution
_PAGE_GUTTER_MIN_WIDTH = 15.0        # pt
_PAGE_GUTTER_MAX_WIDTH = 40.0         # pt
_PAGE_GUTTER_CLEAR_SHARE = 0.85       # of the page's covered text height
_PAGE_GUTTER_MAX_COVERED = 24.0       # pt, absolute covered budget per bin
_PAGE_GUTTER_MIN_SPANS = 4            # clusters gapped across the strip
_PAGE_GUTTER_MIN_BAND_SHARE = 0.2     # of the page's text width, per band
_PAGE_GUTTER_MIN_BAND_POINTS = 100.0  # pt, absolute floor per band
_PAGE_GUTTER_SPLIT_TOL = 0.03         # of the text width, per split position
# The standard 15pt floor has one documented exception: real two-column
# articles whose typographic gutter is 12-14pt (measured 292-304 on
# PubTables-1M A4 pages, i.e. 12pt after bin rounding).  A strip that
# narrow is also what a cropped table's internal column gap looks like
# (14pt on a SciTSR crop whose split shredded its rows), so the narrow
# window opens only on pages with parallel-text evidence — a word budget
# that two-column article pages clear easily (299-1013 words on the 19
# narrow-gutter pages measured) while a table crop never does (42
# words).  Below the budget the 15pt floor stays: the calibrated
# baseline behaviour, unchanged.
_PAGE_GUTTER_NARROW_MIN_WIDTH = 12.0  # pt, only on word-rich pages
_PAGE_GUTTER_NARROW_MIN_WORDS = 150   # words, parallel-text evidence budget

# --- masked gutters: a second chance when v1 is masked by full-width rows ---
# Whole-page gutter detection gives up on mixed pages where a full-width
# table's rows cover the gutter position for more than the covered budget
# (measured 40-64pt on PubTables-1M).  Those are exactly the two-column
# articles whose body text still needs banding.  The second-chance pass
# probes only the equal-split centres v1 already trusts (the typographic
# constants) and excludes, per centre, the words bridging it: a bridging
# word is a full-width element (title line, full-width table row) and
# must not veto the gutter running beside it.  Measured on the 100-page
# PubTables validation: 17 masked pages yield candidates whose bridging
# words cover 0.05-0.29 of the page text height; the cap below keeps the
# exclusion honest — full-width elements must stay the exception, not
# the page (a page half-covered by one spanning table is a table page,
# not a columnar page).  Every other v1 guard applies unchanged.
_PAGE_GUTTER_MAX_BRIDGED_SHARE = 0.35  # of the page's text height, bridged

# --- rotated tables: a gated second pass in the perpendicular frame ---
# A table printed sideways (90 degrees) on an upright page carries words
# whose bboxes are tall-narrow (height ~ word length, width ~ font size);
# the alignment pipeline's horizontal-line assumption cannot see it.  When
# a page has at least ``_ROTATED_MIN_TALL_WORDS`` clearly tall words, the
# whole word set is rotated 90 degrees and the same pipeline runs again in
# the perpendicular frame: upright text self-destructs there (a horizontal
# line becomes a vertical stack of single-word clusters, which never forms
# rows), so the only possible survivors are genuinely rotated regions — and
# a tall-share filter (>= ``_ROTATED_TALL_SHARE`` of the region's
# original-frame words tall) keeps the second pass honest on mixed pages.
# Measured: PubTables-1M has 1 rotated page in the 100-page validation
# sample (PMC4714070 p3, its GT table otherwise missed); SciTSR.c carries 4
# rotated-header tables whose sideways header rows the second pass recovers
# on top of the already region-detected upright bodies; business-records (research) invoices
# have 41 pages with >= 12 tall words (sideways labels) and the second pass
# adds zero false positives there.  Geometry maps back exactly — the
# transform is invertible and direction-independent (a table printed the
# other way round appears 180-degree flipped, rows still horizontal).  The
# one accepted limitation: cell word order assumes this rotation direction;
# the corpus samples all read correctly.
_ROTATED_TALL_AR = 1.5             # height > factor * width
_ROTATED_TALL_MIN_HEIGHT = 8.0     # pt, absolute floor
_ROTATED_MIN_TALL_WORDS = 12       # words, gate for the second pass
_ROTATED_TALL_SHARE = 0.5          # of the region's words, tall-share filter


def _round1(value: float) -> float:
    return round(float(value), 1)


class _Word:
    __slots__ = ("x0", "y0", "x1", "y1", "text")

    def __init__(self, x0: float, y0: float, x1: float, y1: float, text: str):
        """Record a word's coordinates and text."""
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.text = text

    @property
    def cy(self) -> float:
        """The word's vertical center coordinate."""
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        """The word's height (floor 0.1 to guard against division by zero)."""
        return max(self.y1 - self.y0, 0.1)


class _Line:
    """A visual line: words clustered by vertical center."""

    __slots__ = ("words", "cells")

    def __init__(self, words: list[_Word]):
        """Sort words by horizontal position and split into cells."""
        self.words = sorted(words, key=lambda w: w.x0)
        self.cells = self._split_cells()

    def _split_cells(self) -> list[list[_Word]]:
        heights = [w.height for w in self.words]
        median_height = sorted(heights)[len(heights) // 2]
        gap_threshold = max(_MIN_GAP_POINTS, _GAP_FACTOR * median_height)
        cells: list[list[_Word]] = []
        current: list[_Word] = [self.words[0]]
        for word in self.words[1:]:
            gap = word.x0 - current[-1].x1
            if gap > gap_threshold:
                cells.append(current)
                current = [word]
            else:
                current.append(word)
        cells.append(current)
        return cells

    @property
    def top(self) -> float:
        """Minimum top boundary of words in the row."""
        return min(w.y0 for w in self.words)

    @property
    def bottom(self) -> float:
        """Maximum bottom boundary of words in the row."""
        return max(w.y1 for w in self.words)

    @property
    def cy(self) -> float:
        """The row's vertical center coordinate."""
        return (self.top + self.bottom) / 2

    @property
    def cell_starts(self) -> list[float]:
        """Left-boundary x coordinate of each cell."""
        return [cell[0].x0 for cell in self.cells if cell]

    @property
    def cell_edges(self) -> list[float]:
        """Left-aligned columns realign on starts; right-aligned numeric
        columns realign on ends — both are alignment facts."""
        edges: list[float] = []
        for cell in self.cells:
            if cell:
                edges.append(cell[0].x0)
                edges.append(cell[-1].x1)
        return edges

    @property
    def text(self) -> str:
        """Text joined from the words in the row."""
        return " ".join(w.text for w in self.words)


def _union_length(intervals: list[tuple[float, float]]) -> float:
    """Total length of the union of 1-D intervals (overlaps counted once)."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_y0, cur_y1 = ordered[0]
    for y0, y1 in ordered[1:]:
        if y0 > cur_y1:
            total += cur_y1 - cur_y0
            cur_y0, cur_y1 = y0, y1
        else:
            cur_y1 = max(cur_y1, y1)
    return total + cur_y1 - cur_y0


def _word_clusters(words: list[_Word]) -> list[list[_Word]]:
    """Cluster words by vertical-centre proximity (shared line model)."""
    if not words:
        return []
    heights = sorted(w.height for w in words)
    median_height = heights[len(heights) // 2]
    tolerance = median_height * 0.5
    ordered = sorted(words, key=lambda w: w.cy)
    clusters: list[list[_Word]] = [[ordered[0]]]
    for word in ordered[1:]:
        cluster = clusters[-1]
        cluster_cy = sum(w.cy for w in cluster) / len(cluster)
        if abs(word.cy - cluster_cy) <= tolerance:
            cluster.append(word)
        else:
            clusters.append([word])
    return clusters


def _page_gutters(words: list[_Word], page_width: float) -> list[tuple[float, float]]:
    """Detect persistent vertical whitespace gutters from word x-coverage.

    A gutter is a strip that stays word-free over at least
    ``_PAGE_GUTTER_CLEAR_SHARE`` of the page's covered text height (with
    an absolute covered budget in points, so a sparse page's incidental
    gaps do not qualify), sits in the width window, and is spanned by at
    least ``_PAGE_GUTTER_MIN_SPANS`` gapped clusters — the signature of
    parallel text columns, unlike a table's internal column gap (cleared
    only over the table's own rows) or an empty page half.

    Finally, the surviving strips must cut the page's text extent into
    bands each at least ``_PAGE_GUTTER_MIN_BAND_SHARE`` of the text width
    and ``_PAGE_GUTTER_MIN_BAND_POINTS`` points wide — text columns are
    wide; a wide borderless table's internal gaps slice it into narrow
    cell columns instead.  One narrow band rejects the whole set: a
    partial split would shred a genuine table, while no split merely
    falls back to the single-column pipeline.

    And each strip's centre must sit near an equal-split position of
    the text width (0.5, 1/3+2/3, 0.25+0.75): real text columns divide
    the page into equal shares, while a table's internal gaps sit at
    its column-width positions.

    The width window's lower edge drops from 15pt to 12pt when the page
    carries at least ``_PAGE_GUTTER_NARROW_MIN_WORDS`` words — narrow
    page gutters exist (12pt two-column articles) but a strip that
    narrow needs the parallel-text evidence a full article page has and
    a cropped table does not (see the constants above for the measured
    margins).
    """
    if len(words) < 12 or page_width <= 0:
        return []
    min_width = (
        _PAGE_GUTTER_NARROW_MIN_WIDTH
        if len(words) >= _PAGE_GUTTER_NARROW_MIN_WORDS
        else _PAGE_GUTTER_MIN_WIDTH
    )
    bins = int(page_width / _GUTTER_SCAN_BIN) + 1
    covered: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for word in words:
        first = max(0, min(bins - 1, int(word.x0 / _GUTTER_SCAN_BIN)))
        last = max(0, min(bins - 1, int(word.x1 / _GUTTER_SCAN_BIN)))
        for b in range(first, last + 1):
            covered[b].append((word.y0, word.y1))
    page_text_height = _union_length([(w.y0, w.y1) for w in words])
    if page_text_height <= 0:
        return []
    limit = min(
        (1.0 - _PAGE_GUTTER_CLEAR_SHARE) * page_text_height,
        _PAGE_GUTTER_MAX_COVERED,
    )
    gutters: list[tuple[float, float]] = []
    run_start: int | None = None
    for b in range(bins + 1):  # +1 sentinel closes a trailing clear run
        clear = b < bins and _union_length(covered[b]) <= limit
        if clear and run_start is None:
            run_start = b
        elif not clear and run_start is not None:
            x0 = run_start * _GUTTER_SCAN_BIN
            x1 = b * _GUTTER_SCAN_BIN
            run_start = None
            if not (min_width <= x1 - x0 <= _PAGE_GUTTER_MAX_WIDTH):
                continue
            if not (any(w.x1 < x0 for w in words) and any(w.x0 > x1 for w in words)):
                continue
            centre = (x0 + x1) / 2
            spans = 0
            for cluster in _word_clusters(words):
                if any(w.x0 <= centre <= w.x1 for w in cluster):
                    continue  # bridged: a full-width element, not a split
                if any((w.x0 + w.x1) / 2 < centre for w in cluster) and any(
                    (w.x0 + w.x1) / 2 >= centre for w in cluster
                ):
                    spans += 1
            if spans >= _PAGE_GUTTER_MIN_SPANS:
                gutters.append((round(x0, 1), round(x1, 1)))
    if not gutters:
        return []
    text_x0 = min(w.x0 for w in words)
    text_x1 = max(w.x1 for w in words)
    min_band = max(
        _PAGE_GUTTER_MIN_BAND_SHARE * (text_x1 - text_x0),
        _PAGE_GUTTER_MIN_BAND_POINTS,
    )
    prev = text_x0
    for gx0, gx1 in gutters:
        if gx0 - prev < min_band:
            return []  # a cell-column slice, not a text-column layout
        prev = gx1
    if text_x1 - prev < min_band:
        return []
    text_width = text_x1 - text_x0
    equal_splits = [i / k for k in (2, 3, 4) for i in range(1, k)]
    for gx0, gx1 in gutters:
        position = ((gx0 + gx1) / 2 - text_x0) / text_width
        if not any(abs(position - split) <= _PAGE_GUTTER_SPLIT_TOL for split in equal_splits):
            return []  # wherever the column widths are, not a layout
    return gutters


def _masked_page_gutters(
    words: list[_Word], page_width: float
) -> list[tuple[float, float]]:
    """Second-chance gutters for pages whose v1 detection was masked.

    Called only when ``_page_gutters`` returned nothing.  For each of the
    five equal-split centres of the text width, the words bridging the
    centre are excluded from the coverage map and from the reference
    height (a full-width element does not veto the whitespace beside
    it), their own y-coverage capped by ``_PAGE_GUTTER_MAX_BRIDGED_SHARE``
    so the exclusion stays exceptional.  All other guards are v1's:
    width window (with the same word-budget narrow exception), both
    sides, ``_PAGE_GUTTER_MIN_SPANS`` gapped clusters, the strip centre
    within the split tolerance of the probed centre, and the
    all-or-nothing band-width floor.
    """
    if len(words) < 12 or page_width <= 0:
        return []
    min_width = (
        _PAGE_GUTTER_NARROW_MIN_WIDTH
        if len(words) >= _PAGE_GUTTER_NARROW_MIN_WORDS
        else _PAGE_GUTTER_MIN_WIDTH
    )
    text_x0 = min(w.x0 for w in words)
    text_x1 = max(w.x1 for w in words)
    text_width = text_x1 - text_x0
    page_text_height = _union_length([(w.y0, w.y1) for w in words])
    if text_width <= 0 or page_text_height <= 0:
        return []
    bins = int(page_width / _GUTTER_SCAN_BIN) + 1
    strips: list[tuple[float, float]] = []
    for part in (0.25, 1 / 3, 0.5, 2 / 3, 0.75):
        centre = text_x0 + part * text_width
        bridging = [w for w in words if w.x0 <= centre <= w.x1]
        bridged = _union_length([(w.y0, w.y1) for w in bridging])
        if bridged > _PAGE_GUTTER_MAX_BRIDGED_SHARE * page_text_height:
            continue
        nb = [w for w in words if not (w.x0 <= centre <= w.x1)]
        if not nb:
            continue
        height = _union_length([(w.y0, w.y1) for w in nb])
        if height <= 0:
            continue
        limit = min((1.0 - _PAGE_GUTTER_CLEAR_SHARE) * height, _PAGE_GUTTER_MAX_COVERED)
        covered: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
        for word in nb:
            first = max(0, min(bins - 1, int(word.x0 / _GUTTER_SCAN_BIN)))
            last = max(0, min(bins - 1, int(word.x1 / _GUTTER_SCAN_BIN)))
            for b in range(first, last + 1):
                covered[b].append((word.y0, word.y1))
        run_start: int | None = None
        for b in range(bins + 1):  # +1 sentinel closes a trailing clear run
            clear = b < bins and _union_length(covered[b]) <= limit
            if clear and run_start is None:
                run_start = b
            elif not clear and run_start is not None:
                x0 = run_start * _GUTTER_SCAN_BIN
                x1 = b * _GUTTER_SCAN_BIN
                run_start = None
                if not (min_width <= x1 - x0 <= _PAGE_GUTTER_MAX_WIDTH):
                    continue
                if abs((x0 + x1) / 2 - centre) > _PAGE_GUTTER_SPLIT_TOL * text_width:
                    continue
                if not (any(w.x1 < x0 for w in words) and any(w.x0 > x1 for w in words)):
                    continue
                spans = 0
                for cluster in _word_clusters(words):
                    if any(w.x0 <= (x0 + x1) / 2 <= w.x1 for w in cluster):
                        continue
                    if any((w.x0 + w.x1) / 2 < (x0 + x1) / 2 for w in cluster) and any(
                        (w.x0 + w.x1) / 2 >= (x0 + x1) / 2 for w in cluster
                    ):
                        spans += 1
                if spans >= _PAGE_GUTTER_MIN_SPANS:
                    key = (round(x0, 1), round(x1, 1))
                    if key not in {(round(s[0], 1), round(s[1], 1)) for s in strips}:
                        strips.append(key)
    if not strips:
        return []
    min_band = max(
        _PAGE_GUTTER_MIN_BAND_SHARE * text_width,
        _PAGE_GUTTER_MIN_BAND_POINTS,
    )
    prev = text_x0
    for gx0, gx1 in strips:
        if gx0 - prev < min_band:
            return []
        prev = gx1
    if text_x1 - prev < min_band:
        return []
    return sorted(strips)


def _split_words_at_gutters(
    words: list[_Word], gutters: list[tuple[float, float]]
) -> list[tuple[int, list[_Word]]]:
    """Split a cy-cluster into per-column parts at the gutter centres.

    A cluster whose words bridge a gutter centre (a full-width element:
    title line, full-width table row) is kept intact and stays with its
    left band; only genuinely gapped clusters are split, each part tagged
    with its band index (0 = leftmost).  A cluster that sits entirely
    right of a gutter centre belongs to the next band, not the one it
    happened to be listed in — a right-column paragraph or footer table
    whose baselines have no left-column counterpart must not be dragged
    into the left band (that scattered a footer table's rows across two
    bands on PubTables-1M mixed pages).
    """
    parts: list[tuple[int, list[_Word]]] = [(0, list(words))]
    for gutter in gutters:
        centre = (gutter[0] + gutter[1]) / 2
        next_parts: list[tuple[int, list[_Word]]] = []
        for band, part in parts:
            if any(w.x0 <= centre <= w.x1 for w in part):
                next_parts.append((band, part))
                continue
            left = [w for w in part if (w.x0 + w.x1) / 2 < centre]
            right = [w for w in part if (w.x0 + w.x1) / 2 >= centre]
            if left and right:
                next_parts.append((band, left))
                next_parts.append((band + 1, right))
            elif right:
                next_parts.append((band + 1, right))
            else:
                next_parts.append((band, part))
        parts = next_parts
    return parts


def _is_tall_word(word: _Word) -> bool:
    """A sideways-rendered word: bbox height tracks word length and width
    tracks font size — the opposite of a horizontal word."""
    return (word.y1 - word.y0) > _ROTATED_TALL_AR * (word.x1 - word.x0) and (
        word.y1 - word.y0
    ) > _ROTATED_TALL_MIN_HEIGHT


def _rotate_words_90(words: list[_Word], height: float) -> list[_Word]:
    """Rotate the page 90 degrees: (x, y) -> (height - y, x).

    Tall words become wide, so the alignment pipeline's horizontal-line
    assumption holds again in the perpendicular frame.
    """
    return [_Word(height - w.y1, w.x0, height - w.y0, w.x1, w.text) for w in words]


def _rotate_bbox_back(
    bbox: tuple[float, float, float, float], height: float
) -> tuple[float, float, float, float]:
    """Map a bbox from the rotated frame back to the original page frame."""
    x0, y0, x1, y1 = bbox
    return (y0, height - x1, y1, height - x0)


def _word_overlaps_region(word: _Word, bbox) -> bool:
    """Does the word's bbox belong to the region (overlap over the smaller
    area >= 0.25, or the word centre inside) — the evaluation's matching
    rule, applied here to collect a detected region's original-frame words."""
    wb = (word.x0, word.y0, word.x1, word.y1)
    inter = max(0.0, min(wb[2], bbox[2]) - max(wb[0], bbox[0])) * max(
        0.0, min(wb[3], bbox[3]) - max(wb[1], bbox[1])
    )
    if inter <= 0:
        return False
    wa = max(0.0, wb[2] - wb[0]) * max(0.0, wb[3] - wb[1])
    ra = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
    if min(wa, ra) > 0 and inter / min(wa, ra) >= 0.25:
        return True
    cx, cy = (wb[0] + wb[2]) / 2, (wb[1] + wb[3]) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


def _banded_lines(
    words: list[_Word], gutters: list[tuple[float, float]]
) -> list[list[_Line]]:
    """Cluster words into visual lines, one line stream per page band.

    Words are clustered by vertical-centre proximity exactly as on a
    single-column page; a cluster spanning a gutter is then split at it,
    and the resulting per-band line streams are returned sorted by top.
    Region detection afterwards runs inside each band, where the
    business-records-calibrated single-column thresholds keep their meaning.
    """
    if not words:
        return []
    bands: list[list[_Line]] = [[] for _ in range(len(gutters) + 1)]
    for cluster in _word_clusters(words):
        for band, part in _split_words_at_gutters(cluster, gutters):
            if part:
                bands[band].append(_Line(part))
    return [sorted(band, key=lambda line: line.top) for band in bands if band]


def _visual_lines(
    words: list[_Word], gutters: list[tuple[float, float]] | None = None
) -> list[_Line]:
    """Cluster words into visual lines by vertical center proximity."""
    if gutters:
        return [line for band in _banded_lines(words, gutters) for line in band]
    return [line for band in _banded_lines(words, []) for line in band]


def _column_anchors(rows: list[_Line]) -> list[float]:
    """Cluster cell edge positions (starts and ends) into column anchors.

    Invoices right-align numeric columns, so their cell *starts* drift with
    the printed width while their *ends* realign exactly; description
    columns realign on starts.  Clustering both edge kinds captures the
    realignment fact either way.  An anchor survives only when it is shared
    by at least ``_ANCHOR_MIN_ROW_SHARE`` of the region rows.
    """
    edges: list[float] = []
    for line in rows:
        edges.extend(line.cell_edges)
    if not edges:
        return []
    edges.sort()
    clusters: list[list[float]] = [[edges[0]]]
    for edge in edges[1:]:
        if edge - clusters[-1][-1] <= _ANCHOR_CLUSTER_TOLERANCE:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])
    min_count = max(2, _ANCHOR_MIN_ROW_SHARE * len(rows))
    anchors: list[float] = []
    for cluster in clusters:
        if len(cluster) >= min_count:
            anchors.append(round(sum(cluster) / len(cluster), 1))
    return sorted(anchors)


def _anchor_alignment(row: _Line, anchors: list[float]) -> int:
    """How many of a row's cell edges realign on the shared anchors."""
    matched = 0
    for edge in row.cell_edges:
        if any(abs(edge - anchor) <= _ANCHOR_CLUSTER_TOLERANCE for anchor in anchors):
            matched += 1
    return matched


def _is_numeric_value(cell: list[_Word]) -> bool:
    text = " ".join(w.text for w in cell).strip()
    return bool(
        _NUMERIC_VALUE_RE.match(text) or _NUMERIC_RANGE_RE.match(text)
    )


def _header_like(line: _Line) -> bool:
    """A header row labels its columns without carrying numeric data."""
    if not line.cells:
        return False
    texts = [" ".join(w.text for w in cell) for cell in line.cells if cell]
    return bool(texts) and all(
        not _NUMERIC_CELL_RE.search(text) for text in texts
    )


# A two-column region where BOTH columns average at least this many words
# per cell is running prose, not a table — measured on PubTables-1M mixed
# layouts (pages whose whole-page gutter detection is masked by a full-width
# table, so parallel body-text columns merge into 2-cell "rows"): the prose
# false positives run 7.4-9.2 words/cell in both columns, while every genuine
# header-path region keeps at least one sparse column (business-records (research) invoices
# 4.7/4.3, 5.2/1.8, 2.0/4.5; academic tables <= 2).  Completes the two-column
# guard's original intent ("neither column is data: both are prose") with
# the prose half of the test.
_PROSE_COLUMN_DENSITY = 6.0
# ...and the rejection only fires on PURE prose regions: ones without a
# contiguous block of at least this many multi-column rows.  A region that
# absorbed adjacent prose around a genuine table keeps its multi-column
# block (measured on PMC3032948 p3: a full-width 5-row table directly above
# the prose columns with no region break; after anchor filtering 3 of its
# rows survive as a contiguous run alongside 28 prose rows), while the
# pure-prose false positives carry only scattered multi-cell fragments —
# a numbered reference list column cuts its "1." numbers into separate
# cells (PMC3667107 p5: eight 3-cell rows, none adjacent).  The absorbed
# prose around a detected table is the mixed-layout absorption pathology,
# recorded separately, not a reason to discard the table.
_MIXED_TABLE_BLOCK_MIN_ROWS = 3


def _passes_two_column_guard(kept: list[_Line]) -> bool:
    """Reject two-column regions that look like key-value prose blocks.

    Address, bank-detail and label-value blocks align on exactly two
    columns as well, but neither column is data: both are prose.  A
    genuine two-column line-item table carries either a header-like
    first row or at least one predominantly numeric column (quantities,
    amounts, dates).  The header escape hatch additionally requires a
    sparse column: a first row without digits is also the shape of a
    prose sentence, and two full prose columns (both averaging
    ``_PROSE_COLUMN_DENSITY`` words per cell) are body text, not a
    table — the mixed-layout page's dominant false positive.  The
    prose rejection only fires on pure-prose regions
    (``_MIXED_TABLE_BLOCK_MIN_ROWS``): a region holding a genuine
    multi-column table block keeps its evidence.  This is a lexical
    shape fact on the recorded cells — cells are still never
    re-flowed.
    """
    two_cell_rows = [row for row in kept if len(row.cells) == 2]
    if len(two_cell_rows) < len(kept) * 0.6:
        return True  # predominantly multi-column: alignment evidence suffices
    for column_index in (0, 1):
        cells = [row.cells[column_index] for row in two_cell_rows if len(row.cells) == 2]
        numeric = sum(1 for cell in cells if _is_numeric_value(cell))
        if cells and numeric / len(cells) >= 0.5:
            return True
    if not _header_like(kept[0]):
        return False
    run = best_run = 0
    for row in kept:
        run = run + 1 if len(row.cells) >= 3 else 0
        best_run = max(best_run, run)
    if best_run >= _MIXED_TABLE_BLOCK_MIN_ROWS:
        return True  # a contiguous multi-column table block: keep its evidence
    for column_index in (0, 1):
        cells = [row.cells[column_index] for row in two_cell_rows if len(row.cells) == 2]
        avg_words = sum(len(cell) for cell in cells) / len(cells)
        if avg_words < _PROSE_COLUMN_DENSITY:
            return True  # a label/data-shaped column: header evidence holds
    return False  # both columns are dense running prose


def _numeric_dominant_column_ratio(kept: list[_Line]) -> float:
    """Share of pure-numeric cells in the most numeric column.

    Columns are the row-cell ordinals — after anchor refinement every
    kept row's cells realign on the shared anchors, so the ordinal maps
    to a stable column.  A data column (quantity, amount, date) is the
    lexical signature that separates a genuine table from an aligned
    metadata block; the ratio is recorded as a payload fact and drives
    confidence stratification, never rejection.
    """
    columns: dict[int, list[list[_Word]]] = {}
    for row in kept:
        for column_index, cell in enumerate(row.cells):
            if cell:
                columns.setdefault(column_index, []).append(cell)
    best = 0.0
    for cells in columns.values():
        numeric = sum(1 for cell in cells if _is_numeric_value(cell))
        best = max(best, numeric / len(cells))
    return best


def _is_table_like(line: _Line) -> bool:
    return len(line.cells) >= _MIN_CELLS_PER_ROW


def _detect_regions(lines: list[_Line]) -> list[list[_Line]]:
    """Group consecutive table-like lines into candidate regions.

    A region ends either after ``_MAX_REGION_BREAK`` non-candidate lines or
    when the next candidate line sits a large vertical gap below the
    previous one (evenly spaced table rows vs. unrelated page areas).
    """
    heights = sorted(line.bottom - line.top for line in lines) or [0.0]
    median_height = heights[len(heights) // 2]
    gap_limit = max(_MIN_REGION_GAP_POINTS, _MAX_REGION_GAP_FACTOR * median_height)
    regions: list[list[_Line]] = []
    current: list[_Line] = []
    break_run = 0
    for line in lines:
        if _is_table_like(line):
            if current and line.top - current[-1].bottom > gap_limit:
                if len(current) >= _MIN_ROWS_PER_REGION:
                    regions.append(current)
                current = []
                break_run = 0
            current.append(line)
            break_run = 0
        elif current:
            break_run += 1
            if break_run > _MAX_REGION_BREAK:
                if len(current) >= _MIN_ROWS_PER_REGION:
                    regions.append(current)
                current = []
    if len(current) >= _MIN_ROWS_PER_REGION:
        regions.append(current)
    return regions


def _refine_region(region: list[_Line]) -> tuple[list[_Line], list[float]] | None:
    """Keep only the rows that realign on shared anchors, then re-anchor.

    A candidate region may absorb noise lines (page headers, addresses)
    that sit between table-like lines.  Instead of rejecting the whole
    region for one misaligned row, misaligned rows are dropped and anchors
    are recomputed over the survivors — still a purely geometric filter.
    """
    anchors = _column_anchors(region)
    if len(anchors) < _MIN_COLUMN_ANCHORS:
        return None
    kept = [row for row in region if _anchor_alignment(row, anchors) >= _MIN_ROW_ALIGNMENT]
    if len(kept) < _MIN_ROWS_PER_REGION:
        return None
    anchors = _column_anchors(kept)
    if len(anchors) < _MIN_COLUMN_ANCHORS:
        return None
    kept = [row for row in kept if _anchor_alignment(row, anchors) >= _MIN_ROW_ALIGNMENT]
    if len(kept) < _MIN_ROWS_PER_REGION:
        return None
    if not _passes_two_column_guard(kept):
        return None
    return kept, anchors


def _refined_regions(
    words: list[_Word], gutters: list[tuple[float, float]]
) -> list[tuple[list[_Line], list[float]]]:
    """Every refined (kept, anchors) region under the given gutter set."""
    refined: list[tuple[list[_Line], list[float]]] = []
    for band_lines in _banded_lines(words, gutters):
        for region in _detect_regions(band_lines):
            result = _refine_region(region)
            if result is not None:
                refined.append(result)
    return refined


def _line_word_ids(line: _Line) -> frozenset[int]:
    """Identity of a line: the set of its word objects."""
    return frozenset(id(word) for word in line.words)


def _protected_row(row: _Line) -> bool:
    """A row whose word-for-word survival the rollback guard demands.

    Multi-column rows are always protected.  A two-cell row is protected
    unless BOTH cells are running prose (``_PROSE_COLUMN_DENSITY`` words
    each — the same calibrated density the two-column guard uses, applied
    per row) or carries a numeric cell: a label/data row is table
    evidence even when it has only two columns, while a dense prose pair
    is body text whose absorption the banding trims.  One-cell rows are
    absorbed noise in any stream and stay trimmable.
    """
    if len(row.cells) >= 3:
        return True
    if len(row.cells) == 2:
        left, right = row.cells
        if _is_numeric_value(left) or _is_numeric_value(right):
            return True
        return not (
            len(left) >= _PROSE_COLUMN_DENSITY
            and len(right) >= _PROSE_COLUMN_DENSITY
        )
    return False


def _masked_banding_preserves_evidence(
    words: list[_Word], gutters: list[tuple[float, float]]
) -> bool:
    """Multi-column evidence must survive the second-chance banding.

    A masked gutter candidate can sit on a table's own column gap: the
    banding then splits its rows into half-width fragments — the
    shredding pathology the report's follow-up work called out
    (measured on the 100-page PubTables validation: 13 of 17 candidate
    pages shred, e.g. PMC5814477 p7 keeps 2 of 35 multi-column rows).
    The guard compares the two pipelines on the same word objects: for
    every region the plain (single-column) pipeline refined, its
    protected rows — every row that is not a dense two-cell prose pair —
    must reappear word-for-word inside ONE banded region.  A bridging
    table row stays intact in its band, so a genuine masked-gutter page
    passes (PMC5846350 p2 / PMC3022178 p3: the plain pipeline refines
    nothing, the banded one recovers the missed table); a shredded one
    falls back to the plain pipeline — the calibrated baseline
    behaviour.  Purely geometric and lexical shape: no cell is ever
    re-flowed or reordered.
    """
    plain = _refined_regions(words, [])
    banded = _refined_regions(words, gutters)
    band_line_sets = [
        {_line_word_ids(line) for line in kept} for kept, _ in banded
    ]
    for kept, _ in plain:
        protected = [row for row in kept if _protected_row(row)]
        if not protected:
            continue
        if not any(
            all(_line_word_ids(row) in line_ids for row in protected)
            for line_ids in band_line_sets
        ):
            return False
    return True


class BorderlessTableExtractor:
    """Record borderless line-item tables as alignment evidence."""

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Alignment evidence from the native text layer."""
        document = fitz.open(file_path)
        try:
            pages = [
                self._extract_page(document[page_index], page_index + 1)
                for page_index in range(int(document.page_count))
            ]
            return PDFExtractionResult(
                pages=pages,
                extraction_method="borderless_tables",
                metadata={
                    "page_count": len(pages),
                    "source_kind": "borderless_table",
                    "table_block_count": sum(len(page.blocks) for page in pages),
                    "pdf_tables": _summarize(pages),
                },
            )
        finally:
            document.close()

    def extract_with_ocr(
        self,
        file_path: str | Path,
        *,
        language: str = "eng",
        dpi: int = 300,
        tessdata: str | Path | None = None,
    ) -> PDFExtractionResult:
        """Alignment evidence from OCR word coordinates (scanned pages).

        The same alignment pipeline as ``extract``, but the word source is
        a full-page PyMuPDF/Tesseract render.  OCR words carry more jitter
        and skew artifacts than native words, so: noise words are dropped
        before alignment, blocks carry a lower confidence, and the
        ``source_kind`` is ``borderless_table_ocr`` to keep the evidence
        provenance honest.  A page whose OCR render fails yields no
        evidence and is recorded in ``ocr_failed_pages`` — one bad page
        never discards its neighbours.

        Raises ``OCRBackendUnavailable`` (from the OCR evidence module)
        when the local PyMuPDF build cannot locate tessdata; the caller
        keeps native evidence and the honest fact that no OCR ran.
        """
        tessdata_path = self._resolve_tessdata(tessdata)
        document = fitz.open(file_path)
        try:
            pages: list[PDFPage] = []
            failed_pages: list[int] = []
            for page_index in range(int(document.page_count)):
                page = document[page_index]
                try:
                    text_page = page.get_textpage_ocr(
                        flags=0,
                        language=language,
                        dpi=dpi,
                        full=True,
                        tessdata=tessdata_path,
                    )
                    raw_words = page.get_text("words", textpage=text_page, sort=False)
                except Exception:
                    failed_pages.append(page_index + 1)
                    continue
                words = [
                    _Word(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]))
                    for w in raw_words
                    if str(w[4]).strip()
                ]
                words = [
                    word for word in words if _OCR_NOISE_SURVIVES_RE.search(word.text)
                ]
                pages.append(
                    self._page_from_words(
                        words,
                        page_number=page_index + 1,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        rotation=int(page.rotation),
                        word_source="ocr",
                    )
                )
            return PDFExtractionResult(
                pages=pages,
                extraction_method="borderless_tables_ocr",
                metadata={
                    "page_count": len(pages),
                    "source_kind": "borderless_table_ocr",
                    "ocr_backend": "pymupdf_tesseract",
                    "ocr_language": language,
                    "ocr_dpi": dpi,
                    "ocr_noise_word_filter": (
                        "drop words with neither a 3+ letter run, any digit, "
                        "nor a 2+ ideograph CJK/kana run"
                    ),
                    "ocr_failed_pages": failed_pages,
                    "table_block_count": sum(len(page.blocks) for page in pages),
                    "pdf_tables": _summarize(pages),
                },
            )
        finally:
            document.close()

    @staticmethod
    def _resolve_tessdata(tessdata: str | Path | None) -> str:
        if not hasattr(fitz.Page, "get_textpage_ocr"):
            raise OCRBackendUnavailable(
                "PyMuPDF does not expose Page.get_textpage_ocr in this environment"
            )
        path = tessdata
        if path is None:
            try:
                path = fitz.get_tessdata()
            except Exception as error:
                raise OCRBackendUnavailable(
                    "Tesseract tessdata is unavailable; configure TESSDATA_PREFIX or pass tessdata"
                ) from error
        if not path:
            raise OCRBackendUnavailable(
                "Tesseract tessdata is unavailable; configure TESSDATA_PREFIX or pass tessdata"
            )
        return str(path)

    def _extract_page(self, page: fitz.Page, page_number: int) -> PDFPage:
        try:
            raw_words = page.get_text("words")
        except Exception:
            raw_words = []
        words = [
            _Word(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]))
            for w in raw_words
            if str(w[4]).strip()
        ]
        return self._page_from_words(
            words,
            page_number=page_number,
            width=float(page.rect.width),
            height=float(page.rect.height),
            rotation=int(page.rotation),
            word_source="native",
        )

    def _page_from_words(
        self,
        words: list[_Word],
        *,
        page_number: int,
        width: float,
        height: float,
        rotation: int,
        word_source: str,
        _allow_rotated_pass: bool = True,
    ) -> PDFPage:
        """Run the alignment pipeline on pre-extracted words.

        A gated second pass in the perpendicular frame picks up tables
        printed sideways (see the ``_ROTATED_*`` constants for the measured
        evidence); its detections are mapped back exactly and marked
        ``rotated`` in the payload.  ``_allow_rotated_pass`` is disabled for
        the recursive rotated-frame call: upright words become tall when
        rotated, so without the flag the gate would re-fire and recurse
        forever.
        """
        blocks: list[PDFBlock] = []
        gutters = _page_gutters(words, width)
        if not gutters:
            # v1 was masked by full-width elements: a second-chance pass
            # probes the equal-split centres with bridging words excluded,
            # and banding only happens when it preserves the plain
            # pipeline's multi-column evidence (else: the single-column
            # fallback, i.e. the calibrated baseline behaviour).
            masked = _masked_page_gutters(words, width)
            if masked and _masked_banding_preserves_evidence(words, masked):
                gutters = masked
        for band_lines in _banded_lines(words, gutters):
            for region in _detect_regions(band_lines):
                refined = _refine_region(region)
                if refined is None:
                    continue
                kept, anchors = refined
                blocks.append(
                    self._emit_block(
                        kept, anchors, page_number, len(blocks), word_source
                    )
                )
        if _allow_rotated_pass:
            blocks.extend(
                self._rotated_pass_blocks(
                    words,
                    page_number=page_number,
                    width=width,
                    height=height,
                    rotation=rotation,
                    word_source=word_source,
                )
            )
        return PDFPage(
            page_number=page_number,
            width=width,
            height=height,
            rotation=rotation,
            blocks=blocks,
        )

    def _rotated_pass_blocks(
        self,
        words: list[_Word],
        *,
        page_number: int,
        width: float,
        height: float,
        rotation: int,
        word_source: str,
    ) -> list[PDFBlock]:
        """Second pass for sideways tables in the perpendicular frame.

        Gated on the tall-word count.  The recursive upright-pipeline call
        disables the rotated pass: upright words become tall in the rotated
        frame, so the gate would otherwise re-fire and recurse forever.
        Only regions whose original-frame words are tall-dominated survive
        the share filter — upright content seen sideways never does.
        """
        tall = [w for w in words if _is_tall_word(w)]
        if len(tall) < _ROTATED_MIN_TALL_WORDS:
            return []
        rotated_page = self._page_from_words(
            _rotate_words_90(words, height),
            page_number=page_number,
            width=height,
            height=width,
            rotation=rotation,
            word_source=word_source,
            _allow_rotated_pass=False,
        )
        blocks: list[PDFBlock] = []
        for block in rotated_page.blocks:
            if block.block_type != "table":
                continue
            orig_bbox = _rotate_bbox_back(block.bbox, height)
            inside = [w for w in words if _word_overlaps_region(w, orig_bbox)]
            if not inside:
                continue
            share = sum(1 for w in inside if _is_tall_word(w)) / len(inside)
            if share < _ROTATED_TALL_SHARE:
                continue  # upright content seen sideways: not a rotated table
            payload = dict(block.raw_payload or {})
            payload["row_bboxes"] = [
                list(_rotate_bbox_back(rb, height))
                for rb in payload.get("row_bboxes") or []
            ]
            payload["column_anchors"] = [
                _round1(height - a) for a in payload.get("column_anchors") or []
            ]
            payload["rotated"] = True
            payload["note"] = (
                "table printed sideways: detected in a 90-degree rotated frame; "
                "column_anchors are y-positions in the page frame; cell word "
                "order assumes this rotation direction"
            )
            blocks.append(
                PDFBlock(
                    block_type=block.block_type,
                    text=block.text,
                    page_number=page_number,
                    bbox=(
                        _round1(orig_bbox[0]),
                        _round1(orig_bbox[1]),
                        _round1(orig_bbox[2]),
                        _round1(orig_bbox[3]),
                    ),
                    source_kind=block.source_kind,
                    confidence=block.confidence,
                    raw_payload_ref=f"{block.raw_payload_ref}:rotated",
                    raw_payload=payload,
                    evidence_id=f"{block.evidence_id}:rotated",
                )
            )
        return blocks

    def _emit_block(
        self,
        region: list[_Line],
        anchors: list[float],
        page_number: int,
        region_index: int,
        word_source: str = "native",
    ) -> PDFBlock:
        rows = [
            [" ".join(w.text for w in cell).strip() for cell in line.cells]
            for line in region
        ]
        header_like = _header_like(region[0])
        total_rows = [
            {"row_index": index, "label": row[0]}
            for index, row in enumerate(rows)
            if row and _TOTAL_ROW_RE.match(row[0])
        ]
        bbox = (
            min(w.x0 for line in region for w in line.words),
            min(line.top for line in region),
            max(w.x1 for line in region for w in line.words),
            max(line.bottom for line in region),
        )
        row_bboxes = [
            [
                _round1(min(w.x0 for w in line.words)),
                _round1(line.top),
                _round1(max(w.x1 for w in line.words)),
                _round1(line.bottom),
            ]
            for line in region
        ]
        numeric_column_ratio = _numeric_dominant_column_ratio(region)
        has_numeric_column = numeric_column_ratio >= _NUMERIC_COLUMN_SHARE
        base_confidence = 0.55 if word_source == "native" else _OCR_CONFIDENCE
        confidence = (
            base_confidence
            if has_numeric_column
            else base_confidence - _NO_NUMERIC_COLUMN_STRATUM
        )
        return PDFBlock(
            block_type="table",
            text=self._render(rows),
            page_number=page_number,
            bbox=(
                _round1(bbox[0]),
                _round1(bbox[1]),
                _round1(bbox[2]),
                _round1(bbox[3]),
            ),
            source_kind=(
                "borderless_table" if word_source == "native" else "borderless_table_ocr"
            ),
            confidence=round(confidence, 2),
            raw_payload_ref=f"page:{page_number}:borderless_table:{region_index}",
            raw_payload={
                "table_index": region_index,
                "detection_method": "column_alignment",
                "word_source": word_source,
                "row_count": len(region),
                "column_anchor_count": len(anchors),
                "column_anchors": anchors,
                "header_like_first_row": header_like,
                "total_rows": total_rows,
                "rows": rows,
                "row_bboxes": row_bboxes,
                "numeric_dominant_column": has_numeric_column,
                "numeric_dominant_column_ratio": round(numeric_column_ratio, 3),
                "note": (
                    "borderless table recorded from alignment evidence only: "
                    "cells are never re-flowed and totals are never recomputed"
                ),
            },
            evidence_id=f"borderless_table:p{page_number}:t{region_index}",
        )

    @staticmethod
    def _render(rows: list[list[str]]) -> str:
        columns = max(*(len(row) for row in rows), 1)
        lines = ["[Table]"]
        for row in rows:
            normalized = row + [""] * (columns - len(row))
            lines.append("| " + " | ".join(normalized) + " |")
        return "\n".join(lines)


def _summarize(pages: list[PDFPage]) -> dict[str, Any]:
    table_facts: list[dict[str, Any]] = []
    for page in pages:
        for block in page.blocks:
            payload = block.raw_payload or {}
            table_facts.append(
                {
                    "page": page.page_number,
                    "evidence_id": block.evidence_id,
                    "detection_method": payload.get("detection_method"),
                    "word_source": payload.get("word_source"),
                    "row_count": payload.get("row_count"),
                    "column_anchor_count": payload.get("column_anchor_count"),
                    "column_anchors": payload.get("column_anchors"),
                    "header_like_first_row": payload.get("header_like_first_row"),
                    "numeric_dominant_column": payload.get("numeric_dominant_column"),
                    "total_row_count": len(payload.get("total_rows") or []),
                }
            )
    return {
        "table_count": len(table_facts),
        "tables_with_total_rows": sum(1 for fact in table_facts if fact["total_row_count"]),
        "tables_with_header_like_row": sum(
            1 for fact in table_facts if fact["header_like_first_row"]
        ),
        "samples": table_facts[:_MAX_TABLE_SAMPLES],
        "note": (
            "borderless tables recorded from column-alignment evidence; "
            "cells are never re-flowed and totals are never recomputed"
        ),
    }
