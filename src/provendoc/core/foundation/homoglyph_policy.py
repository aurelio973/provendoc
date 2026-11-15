# -*- coding: utf-8 -*-
"""Homoglyph disguise detection — single source of truth for the
observation layer.

Why in the foundation layer: detection (H-set derivation + run
scan) must share the data source with the transform layer to
prevent two drifting rule sets. Detection semantics = counting
in-field instances of cross-script letter substitution disguise
combinations (observation-only — this module performs no text
transformations).

Rules:
    - Data: vendor pair confusables.json + categories.json
      (UTS #39 15.1.0 era snapshot, bundled in PyPI
      confusable-homoglyphs 3.3.1).
    - Fold discipline: the bundled JSON is a mixed-direction closed
      mapping — non-ASCII single-char keys are forward skeleton
      entries ('а'→'a'); ASCII keys are reverse closure entries
      (→ full variant set). So folding takes only forward entries
      with single-step application (UTS #39 skeleton is a
      single-step map; iteration would explode on
      'm'→'rn'→variant set).
    - H set = {c: forward single-char key, ord>=128, single-step
      target is a single-char pure ASCII alnum, script(c)≠LATIN,
      category(c) is a letter (L*)}. Excluded: latin-script letters,
      symbols (Sm), single→multi expansion, multi-char keys, and
      whole-script / single-char runs.
    - Script/category are derived entirely from categories.json
      code_points_ranges [start,end,script_id,category_id] by
      binary search — runtime unicodedata must NOT be used
      (it drifts across Python versions and misclassifies
      characters like U+212A KELVIN SIGN as foreign-script).
    - Hit = a maximal run (ASCII alnum ∪ H chars) containing both
      member types (mixed) with fold(run)≠run. Count unit = number
      of hit runs (token-level — unlike the character-level counts
      of the other families).

Data integrity: at load time both files' SHA256 fingerprints are
verified against the registered values; a mismatch is a
configuration error that raises HomoglyphDataError — no silent
degradation.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List

# ---- Data source anchoring (see public_data_sources.md Security) ----
# Data ships with the package: the packaged copy takes precedence;
# the repo tests/datasets copy stays as a fallback (dev-tree
# compatibility). Drift between the two copies is guarded by the
# load-time SHA256 fingerprint check — any side being altered
# raises HomoglyphDataError, no silent dual rule sets.
_PACKAGED_CONFUSABLES_DIR = Path(__file__).resolve().parent / "data" / "confusables"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_REPO_CONFUSABLES_DIR = _REPO_ROOT / "tests" / "datasets" / "security" / "confusables"

CONFUSABLES_DIR = (
    _PACKAGED_CONFUSABLES_DIR
    if (_PACKAGED_CONFUSABLES_DIR / "confusables.json").is_file()
    else _REPO_CONFUSABLES_DIR
)

#: Registered fingerprints (verified at load time)
CONFUSABLES_SHA256 = (
    "2D8B4774CD9DC6F233A18681BC00423AE270037437F276FC6A8B80630941FE7D"
)
CATEGORIES_SHA256 = (
    "804570F15EDD97F7CD15F4458CEA45BD7DF2C1A0D179A7975ED7EDB32B1CC9C9"
)


class HomoglyphDataError(RuntimeError):
    """Vendor data missing or fingerprint mismatch — a configuration
    error, never silently degraded."""


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _load_verified_pair(
    confusables_path: Path | str,
    categories_path: Path | str,
) -> tuple[Dict[str, list], Dict[str, list]]:
    """Load and fingerprint-verify the vendor pair (for unit tests
    injecting tampered data).

    Verification failure → HomoglyphDataError (fingerprint +
    measured value, for troubleshooting).
    """
    conf_path = Path(confusables_path)
    cat_path = Path(categories_path)
    for path, expected in (
        (conf_path, CONFUSABLES_SHA256),
        (cat_path, CATEGORIES_SHA256),
    ):
        if not path.is_file():
            raise HomoglyphDataError(
                "homoglyph vendor data missing: %s" % path
            )
        actual = _sha256_of(path)
        if actual != expected:
            raise HomoglyphDataError(
                "homoglyph vendor data fingerprint mismatch: %s "
                "(expected %s, got %s)" % (path, expected, actual)
            )
    conf_data = json.loads(conf_path.read_text(encoding="utf-8"))
    cat_data = json.loads(cat_path.read_text(encoding="utf-8"))
    return conf_data, cat_data


def _derive_h(
    conf_data: Dict[str, list],
    cat_data: Dict[str, list],
) -> Dict[str, str]:
    """Derive the H set: forward single-char keys → single-char
    ASCII alnum targets, script≠LATIN and category a letter (L*).
    Pure function; script lookup goes through the vendor range
    table."""
    aliases = cat_data["iso_15924_aliases"]
    categories = cat_data["categories"]
    ranges = cat_data["code_points_ranges"]
    latin_id = aliases.index("LATIN")

    def lookup(cp: int):
        """Binary search code_points_ranges → (script_id,
        category_id)."""
        lo, hi = 0, len(ranges) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            start, end, script_id, category_id = ranges[mid]
            if cp < start:
                hi = mid - 1
            elif cp > end:
                lo = mid + 1
            else:
                return script_id, category_id
        return None, None

    h: Dict[str, str] = {}
    for key, entries in conf_data.items():
        if len(key) != 1 or ord(key) < 128:
            continue
        target = "".join(item["c"] for item in entries)
        if not (len(target) == 1 and target.isascii() and target.isalnum()):
            continue
        script_id, category_id = lookup(ord(key))
        if script_id is None or category_id is None:
            continue
        if script_id != latin_id and categories[category_id].startswith("L"):
            h[key] = target
    return h


# ---- Load-time verification + derivation (failure at import =
# explicit error, no silent fallback) ----

_CONF_DATA, _CAT_DATA = _load_verified_pair(
    CONFUSABLES_DIR / "confusables.json",
    CONFUSABLES_DIR / "categories.json",
)
#: H set (the derived substitution map)
HOMOGLYPH_SUBSTITUTIONS: Dict[str, str] = _derive_h(_CONF_DATA, _CAT_DATA)

# ---- Run scanning and folding ----

_H_SET = frozenset(HOMOGLYPH_SUBSTITUTIONS)

# Hot-path pre-check (C-level scan): H chars are extremely sparse
# in real corpora — skip the Python-level per-char scan when absent
# (same pattern as emoji_legality).
_HOMOGLYPH_HINT = re.compile(
    "[" + re.escape("".join(sorted(HOMOGLYPH_SUBSTITUTIONS))) + "]"
)


def _is_run_char(ch: str) -> bool:
    return (ch.isascii() and ch.isalnum()) or ch in _H_SET


def count_homoglyph_runs(text: str) -> int:
    """Count in-field instances of homoglyph disguise combinations
    (number of hit runs, token-level).

    Deterministic pure function; short-circuits via the C-level
    pre-check when no H char is present, otherwise a single
    Python-level pass. run = maximal consecutive segment (ASCII
    alnum ∪ H chars); hit = run containing both ASCII alnum and
    H chars (mixed) with fold(run)≠run. Whole-script runs (no
    ASCII member) and single-char runs never count.
    """
    if not _HOMOGLYPH_HINT.search(text):
        return 0
    count = 0
    i, n = 0, len(text)
    while i < n:
        if not _is_run_char(text[i]):
            i += 1
            continue
        j = i
        while j < n and _is_run_char(text[j]):
            j += 1
        run = text[i:j]
        has_ascii = False
        has_h = False
        for ch in run:
            if ch.isascii():
                has_ascii = True
            elif ch in _H_SET:
                has_h = True
            if has_ascii and has_h:
                break
        if has_ascii and has_h:
            folded = "".join(HOMOGLYPH_SUBSTITUTIONS.get(c, c) for c in run)
            if folded != run:
                count += 1
        i = j
    return count


def fold_homoglyph_run(run: str) -> str:
    """Single-step fold (H entries only, rest unchanged) — reused
    by tests and fact rendering."""
    return "".join(HOMOGLYPH_SUBSTITUTIONS.get(c, c) for c in run)


def homoglyph_runs(text: str) -> List[str]:
    """Detail list of hit runs (for tests and troubleshooting;
    census uses count_homoglyph_runs)."""
    if not _HOMOGLYPH_HINT.search(text):
        return []
    runs: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if not _is_run_char(text[i]):
            i += 1
            continue
        j = i
        while j < n and _is_run_char(text[j]):
            j += 1
        run = text[i:j]
        has_ascii = any(c.isascii() for c in run)
        has_h = any(c in _H_SET for c in run)
        if has_ascii and has_h:
            if fold_homoglyph_run(run) != run:
                runs.append(run)
        i = j
    return runs
