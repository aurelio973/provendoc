"""Parse cache layer.

ParseCache holds pickled parser snapshots (key = path + size + mtime +
parser code fingerprint + output-affecting config) so repeated benchmark
runs skip re-parsing. compute_parser_fingerprint is the parser code fingerprint —
any derived data that enters the cache must have its generating code covered by the fingerprint (hard rule).

Split out of provendoc_run.py (structural convergence).
"""

import hashlib
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

from provendoc.runtime.config import RunConfig, anchor_cwd_relative

# Module-level fingerprint cache (computed once per process)
_parser_fingerprint: str = ""


def compute_parser_fingerprint() -> str:
    """Hash of parser + cleaning + representation source files.

    Changes when any of them changes → cache key changes →
    stale entries are naturally ignored (not loaded).

    The cached IngestionResult carries representation (the narrative/config/code
    representations built inside universal_parser), so the fingerprint must cover
    every code layer that builds representation: cleaning (sanitized text input),
    structure (heading/special-module detection), strategies (strategy parameters),
    representations (the representation logic itself). Missing any layer → code
    changes without a fingerprint change → stale representations keep being served
    from the old cache (a pitfall hit by the bare-code misdetection case).

    Cache fingerprint contract (migration constraint 3, hard rule):
    for any derived data entering the pickle, the directory holding its generating
    code must be included in the corresponding fingerprint. Today this fingerprint
    covers only base + core (six directories); intelligence/ is not included —
    which relies on the current state that quality scores never enter the pickle
    and are recomputed every run. Once quality-evaluation results start being
    cached, a separate evaluator fingerprint (covering intelligence/) is mandatory; this one must not be reused.

    Note: the fingerprint's package_root is derived from this module's file location
    (runtime/'s parent is the provendoc package root) — the same directory the
    pre-split implementation in provendoc_run.py pointed to, so fingerprint values are unchanged and old caches stay valid.
    """
    global _parser_fingerprint
    if _parser_fingerprint:
        return _parser_fingerprint
    package_root = Path(__file__).resolve().parent.parent
    files_to_hash = [
        "base/parser.py",
        "core/pipeline/universal_parser.py",
        "base/models/document.py",
    ]
    for extra_dir in (
        "core/text", "core/diagnostics", "core/cleaning",
        "core/structure", "core/strategies", "core/representations",
    ):
        extra_path = package_root / extra_dir
        if extra_path.exists():
            files_to_hash.extend(
                str(f) for f in sorted(extra_path.rglob("*.py"))
            )
    parsers_dir = package_root / "core" / "parsers"
    if parsers_dir.exists():
        files_to_hash.extend(str(f) for f in sorted(parsers_dir.rglob("*.py")))
    hasher = hashlib.md5()
    for f in sorted(files_to_hash):
        p = Path(f)
        if not p.is_absolute():
            p = package_root / f
        if p.exists():
            hasher.update(p.read_bytes())
    _parser_fingerprint = hasher.hexdigest()[:12]
    return _parser_fingerprint


class ParseCache:
    """Parse-result cache (parser + config injected; instances hold no state)."""

    def __init__(self, config: RunConfig, parser):
        """Inject the run configuration and parsers."""
        self.config = config
        self.parser = parser

    def cache_key(self, file_path: Path) -> str:
        """Compute cache key from file path + size + mtime + parser fingerprint.

        Config that affects PDF parse output (OCR language, tessdata path, guard caps)
        also joins the key: switching pdf_ocr_language and friends invalidates old
        cache entries automatically, so stale OCR results in the wrong language are
        never served. ocr_parallel_workers doesn't affect output content (serial/parallel
        equivalence verified), so it stays out of the key. The image-asset switch also
        joins the key: old cache from switch-off runs lacks image_asset_payloads, and a hit would silently drop images — the two must be isolated.
        """
        fp = compute_parser_fingerprint()
        pdf_settings = (
            self.config.pdf_max_pages,
            self.config.pdf_max_file_bytes,
            self.config.pdf_max_page_pixels,
            # G11: content-stream caps and the image-asset byte budget both change parse
            # output (guard-page skips / payload truncation) and must join the key —
            # otherwise adjusting the caps serves stale, cap-mismatched results from the old cache.
            getattr(
                self.config, "pdf_max_page_content_stream_bytes", None
            ),
            getattr(
                self.config, "pdf_max_image_asset_bytes", None
            ),
            self.config.pdf_ocr_time_budget_seconds,
            self.config.pdf_mineru_timeout_seconds,
            self.config.pdf_ocr_language,
            self.config.pdf_ocr_tessdata,
            getattr(self.config, "enable_image_asset_export", True),
        )
        try:
            stat = file_path.stat()
            key_str = (
                f"{file_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
                f"|{fp}|{pdf_settings}"
            )
        except OSError:
            key_str = f"{file_path.resolve()}|{fp}|{pdf_settings}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def cache_path(self, key: str) -> Path:
        """Get cache file path for a given key."""
        cache_dir = anchor_cwd_relative(self.config.parse_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{key}.pkl"

    def parse(self, file_path: Path):
        """Parse a file, using cache if available."""
        if not self.config.enable_parse_cache:
            return self.parser.parse(str(file_path))

        key = self.cache_key(file_path)
        cache_path = self.cache_path(key)

        # cache hit
        if cache_path.exists():
            try:
                with open(cache_path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass  # corrupted cache, fall through to re-parse

        # cache miss — parse and cache
        result = self.parser.parse(str(file_path))
        try:
            with open(cache_path, "wb") as f:
                pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass  # caching failure is non-fatal
        return result

    def cleanup(self):
        """Clean stale cache entries by TTL and size limit."""
        cache_dir = anchor_cwd_relative(self.config.parse_cache_dir)
        if not cache_dir.exists():
            return {"removed_ttl": 0, "removed_size": 0, "entries": 0, "size_mb": 0}

        now = datetime.now(timezone.utc).timestamp()
        ttl_seconds = self.config.cache_ttl_days * 86400
        max_size_bytes = self.config.cache_max_size_mb * 1024 * 1024

        entries = []
        for f in cache_dir.glob("*.pkl"):
            try:
                stat = f.stat()
                entries.append((f, stat.st_size, stat.st_mtime))
            except OSError:
                continue

        # TTL: remove files older than cache_ttl_days
        removed_ttl = 0
        for f, size, mtime in entries:
            if now - mtime > ttl_seconds:
                f.unlink(missing_ok=True)
                removed_ttl += 1

        # Recompute remaining after TTL cleanup
        remaining = [(f, size, mtime) for f, size, mtime in entries
                     if f.exists()]
        total_size = sum(size for _, size, _ in remaining)

        # Size limit: if over limit, remove oldest first
        removed_size = 0
        if total_size > max_size_bytes:
            remaining.sort(key=lambda x: x[2])  # oldest first
            for f, size, _ in remaining:
                if total_size <= max_size_bytes:
                    break
                f.unlink(missing_ok=True)
                total_size -= size
                removed_size += 1

        final_entries = len(list(cache_dir.glob("*.pkl")))
        final_size_mb = sum(f.stat().st_size for f in cache_dir.glob("*.pkl")) / (1024 * 1024)
        return {
            "removed_ttl": removed_ttl,
            "removed_size": removed_size,
            "entries": final_entries,
            "size_mb": round(final_size_mb, 1),
        }

    @staticmethod
    def stats() -> dict:
        """Get current cache directory stats (the default-config cache directory)."""
        cache_dir = anchor_cwd_relative(RunConfig().parse_cache_dir)
        if not cache_dir.exists():
            return {"entries": 0, "size_mb": 0.0}
        files = list(cache_dir.glob("*.pkl"))
        total = sum(f.stat().st_size for f in files)
        return {"entries": len(files), "size_mb": round(total / (1024 * 1024), 1)}

    @staticmethod
    def clear(cache_dir: str = ".cache/parse"):
        """Clear all parse cache entries."""
        p = anchor_cwd_relative(cache_dir)
        if p.exists():
            count = len(list(p.glob("*.pkl")))
            shutil.rmtree(p)
            return count
        return 0
