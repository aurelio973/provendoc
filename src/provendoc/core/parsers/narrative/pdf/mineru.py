"""Optional, provenance-preserving MinerU layout adapter.

MinerU is intentionally modelled as a layout *overlay*: immutable native, OCR,
image, and table evidence remains unchanged, while MinerU's ordered semantic
regions are translated into bounded derived blocks that link back to that
source evidence.  Temporary MinerU output is parsed in-process and removed
before this adapter returns.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from provendoc.core.parsers.narrative.pdf.contracts import PDFEnrichmentBackend

# Default on-disk cache directory for MinerU results. The cache is keyed on
# the source PDF digest, the full command line, and the installed MinerU
# version, so a backend upgrade or an option change never serves stale
# results. Pass ``cache_dir=None`` to disable caching entirely.
DEFAULT_CACHE_DIR = Path(".cache") / "mineru"


class MinerUBackendUnavailable(RuntimeError):
    """Raised when the local MinerU command-line entry point is unavailable."""


class MinerUBackendError(RuntimeError):
    """Raised when MinerU cannot produce a usable structured layout output."""


class MinerUBreakerOpen(RuntimeError):
    """Raised when the in-process MinerU circuit breaker is open.

    This is a fast, truthful "we are not calling the backend right now"
    signal, not a model failure: callers keep their native/OCR evidence and
    record the breaker state for observability.
    """


class _CircuitBreaker:
    """In-process consecutive-failure circuit breaker.

    After ``failure_threshold`` consecutive failures the breaker opens for
    ``open_seconds``; calls during that window fail fast without invoking the
    backend. The first call after the window (half-open) probes recovery: a
    success closes the breaker, a failure reopens it. State is per-instance
    and per-process — parallel workers each keep their own breaker, which the
    module documentation states honestly.
    """

    def __init__(self, *, failure_threshold: int = 3, open_seconds: float = 600.0):
        """Configure the breaker threshold and cooldown duration."""
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self.last_failure_reason: str | None = None

    def check(self) -> dict[str, object]:
        """Raise ``MinerUBreakerOpen`` when the breaker is still open."""
        if self.opened_at is None:
            return self.state("closed")
        elapsed = time.monotonic() - self.opened_at
        if elapsed >= self.open_seconds:
            # Half-open: allow one probing request through.
            return self.state("half_open")
        raise MinerUBreakerOpen(
            f"MinerU circuit breaker is open after "
            f"{self.consecutive_failures} consecutive failures; "
            f"retrying after {self.open_seconds - elapsed:.0f}s "
            f"(last failure: {self.last_failure_reason or 'unknown'})"
        )

    def record_success(self) -> None:
        """Reset the breaker state after a successful call."""
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_failure_reason = None

    def record_failure(self, reason: str) -> None:
        """Record one failure; open the breaker once the threshold is hit."""
        self.consecutive_failures += 1
        self.last_failure_reason = reason
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = time.monotonic()

    def state(self, phase: str | None = None) -> dict[str, object]:
        """Return a snapshot of the breaker's current state."""
        return {
            "phase": phase or ("open" if self.opened_at is not None else "closed"),
            "consecutive_failures": self.consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "open_seconds": self.open_seconds,
            "opened_at_epoch": (
                time.time() - (time.monotonic() - self.opened_at)
                if self.opened_at is not None
                else None
            ),
            "last_failure_reason": self.last_failure_reason,
        }


class MinerULayoutBackend:
    """Run local MinerU pipeline mode and expose its structured content list.

    This class has no import-time dependency on ``mineru``.  Installing MinerU
    remains optional, and unavailable executables are reported by
    :meth:`availability` so the PDF pipeline can retain native/OCR evidence
    without making an unsupported success claim.
    """

    _CONTENT_LIST_SUFFIX = "_content_list.json"
    _SUPPORTED_TYPES = ("text", "title", "table", "image", "chart", "equation", "formula")

    def __init__(
        self,
        *,
        executable: str | Path | None = None,
        language: str = "en",
        method: str = "auto",
        timeout_seconds: float = 300.0,
        table: bool = True,
        formula: bool = True,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        cache_dir: str | Path | None = None,
        enable_cache: bool = True,
        breaker: _CircuitBreaker | None = None,
    ) -> None:
        """Configure the MinerU CLI invocation arguments."""
        if method not in {"auto", "txt", "ocr"}:
            raise ValueError("MinerU method must be one of: auto, txt, ocr")
        if timeout_seconds <= 0:
            raise ValueError("MinerU timeout_seconds must be positive")
        self.executable = Path(executable) if executable else None
        self.language = language
        self.method = method
        self.timeout_seconds = timeout_seconds
        self.table = table
        self.formula = formula
        self.command_runner = command_runner or subprocess.run
        self.breaker = breaker or _CircuitBreaker()
        self.enable_cache = enable_cache
        self.cache_dir = (
            Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        )

    def availability(self) -> dict[str, object]:
        """Return command availability, not an unverified model-readiness claim."""
        executable = self._resolve_executable()
        version = self._installed_version()
        if executable is None:
            return {
                "available": False,
                "backend": "mineru",
                "version": version or "unknown",
                "reason": "MinerU CLI executable was not found",
                "model_readiness": "not_checked",
            }
        return {
            "available": True,
            "backend": "mineru",
            "version": version or "unknown",
            "executable": str(executable),
            "command_backend": "pipeline",
            "method": self.method,
            "language": self.language,
            "table": self.table,
            "formula": self.formula,
            "model_readiness": "verified_at_execution",
        }

    def metadata(self) -> dict[str, object]:
        """Return the stable enrichment contract advertised by this adapter."""
        return PDFEnrichmentBackend(
            name="mineru",
            version=self._installed_version() or "unknown",
            kind="layout",
            supported_block_types=("text", "title", "table", "figure", "formula"),
        ).as_dict()

    def extract(self, file_path: str | Path) -> dict[str, Any]:
        """Execute MinerU and return parsed content-list items plus audit metadata.

        The returned output deliberately excludes temporary absolute paths.
        It records only artifact names, checksums, command options, and bounded
        process diagnostics so callers retain an audit trail after cleanup.
        Results are cached under ``cache_dir`` keyed on the PDF digest, the
        command line, and the installed MinerU version; the in-process circuit
        breaker fails fast while the backend is considered down.
        """
        path = Path(file_path)
        executable = self._resolve_executable()
        if executable is None:
            raise MinerUBackendUnavailable("MinerU CLI executable was not found")
        if not path.is_file():
            raise MinerUBackendError(f"source PDF does not exist: {path}")

        # Breaker first: while open, never start a new model run.
        breaker_phase = self.breaker.check()

        cache_key = self._cache_key(path, executable)
        if self.enable_cache and self.cache_dir is not None:
            cached = self._load_cache(cache_key)
            if cached is not None:
                cached["cache"] = {"hit": True, "key": cache_key}
                self.breaker.record_success()
                return cached

        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="provendoc-mineru-") as directory:
            output_dir = Path(directory) / "output"
            command = self._command(executable, path, output_dir)
            try:
                completed = self.command_runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                self.breaker.record_failure(
                    f"timeout after {self.timeout_seconds:.0f}s"
                )
                raise MinerUBackendError(
                    f"MinerU timed out after {self.timeout_seconds:.0f} seconds"
                ) from error
            except OSError as error:
                self.breaker.record_failure(f"could not be started: {error}")
                raise MinerUBackendError(
                    f"MinerU could not be started: {error}"
                ) from error

            if completed.returncode != 0:
                detail = self._diagnostic_tail(completed.stderr) or self._diagnostic_tail(
                    completed.stdout
                )
                suffix = f": {detail}" if detail else ""
                self.breaker.record_failure(
                    f"exited with code {completed.returncode}{suffix}"
                )
                raise MinerUBackendError(
                    f"MinerU exited with code {completed.returncode}{suffix}"
                )

            content_path = self._find_content_list(output_dir, path.stem)
            try:
                payload = json.loads(content_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                self.breaker.record_failure(f"unreadable content-list output: {error}")
                raise MinerUBackendError(
                    f"MinerU content-list output is unreadable: {error}"
                ) from error
            if not isinstance(payload, list):
                self.breaker.record_failure("content-list output is not a JSON list")
                raise MinerUBackendError("MinerU content-list output must be a JSON list")

            items = [item for item in payload if isinstance(item, dict)]
            result = {
                "items": items,
                "backend": self.availability(),
                "command": {
                    "backend": "pipeline",
                    "method": self.method,
                    "language": self.language,
                    "table": self.table,
                    "formula": self.formula,
                    "executable": executable.name,
                },
                "artifacts": {
                    "content_list": content_path.name,
                    "content_list_sha256": self._sha256(content_path),
                    "content_item_count": len(items),
                    "temporary_output_cleaned": True,
                },
                "execution": {
                    "returncode": int(completed.returncode),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "stdout_tail": self._diagnostic_tail(completed.stdout),
                    "stderr_tail": self._diagnostic_tail(completed.stderr),
                },
                "breaker": self.breaker.state(
                    "half_open" if breaker_phase.get("phase") == "half_open" else None
                ),
                "cache": {"hit": False, "key": cache_key},
            }
            self.breaker.record_success()
            if self.enable_cache and self.cache_dir is not None:
                self._store_cache(cache_key, result)
            return result

    def _resolve_executable(self) -> Path | None:
        if self.executable is not None:
            return self.executable if self.executable.is_file() else None

        exe = sys.executable or ""
        candidates = [
            Path(exe).with_name("mineru.exe"),
            Path(exe).with_name("mineru"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate

        discovered = shutil.which("mineru")
        return Path(discovered) if discovered else None

    def _command(self, executable: Path, source: Path, output_dir: Path) -> list[str]:
        return [
            str(executable),
            "--path",
            str(source),
            "--output",
            str(output_dir),
            "--backend",
            "pipeline",
            "--method",
            self.method,
            "--lang",
            self.language,
            "--table",
            str(self.table).lower(),
            "--formula",
            str(self.formula).lower(),
        ]

    def _cache_key(self, path: Path, executable: Path) -> str:
        """Fingerprint the PDF digest, command line, and backend version."""
        digest = hashlib.sha256()
        digest.update(self._sha256(path).encode("ascii"))
        digest.update("\x00".join(self._command(executable, path, Path("."))).encode("utf-8"))
        digest.update((self._installed_version() or "unknown").encode("utf-8"))
        return digest.hexdigest()

    def _load_cache(self, cache_key: str) -> dict[str, Any] | None:
        cache_path = self.cache_dir / f"{cache_key}.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return None
        return payload

    def _store_cache(self, cache_key: str, result: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = self.cache_dir / f"{cache_key}.json"
            cache_path.write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            # Cache write failures must never fail the layout enrichment.
            return

    @classmethod
    def _find_content_list(cls, output_dir: Path, stem: str) -> Path:
        expected = output_dir / stem / "auto" / f"{stem}{cls._CONTENT_LIST_SUFFIX}"
        if expected.is_file():
            return expected
        matches = sorted(output_dir.rglob(f"{stem}{cls._CONTENT_LIST_SUFFIX}"))
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MinerUBackendError("MinerU completed without a content-list JSON artifact")
        raise MinerUBackendError("MinerU produced multiple ambiguous content-list JSON artifacts")

    @staticmethod
    def _installed_version() -> str | None:
        try:
            return importlib.metadata.version("mineru")
        except importlib.metadata.PackageNotFoundError:
            return None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _diagnostic_tail(value: str | None, limit: int = 1000) -> str:
        normalized = " ".join((value or "").split())
        return normalized[-limit:]


def normalized_mineru_type(item: dict[str, Any]) -> str:
    """Map MinerU content-list types to the PDF runtime's structural types."""
    value = str(item.get("type", "text")).strip().casefold()
    if value == "title":
        # Preserve the semantic title role through the overlay.  The runtime
        # later materializes it as a text block with a layout-title payload,
        # then the heading reconstructor can apply its explicit level signal.
        return "title"
    if value == "table":
        return "table"
    if value in {"image", "chart"}:
        return "image"
    if value in {"equation", "interline_equation", "formula"}:
        return "formula"
    return "text"


def mineru_item_text(item: dict[str, Any]) -> str:
    """Extract only structured textual fields that MinerU explicitly produced."""
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()

    if normalized_mineru_type(item) == "table":
        table_body = item.get("table_body")
        if isinstance(table_body, str) and table_body.strip():
            return table_body.strip()
        captions = item.get("table_caption")
        if isinstance(captions, Sequence) and not isinstance(captions, (str, bytes)):
            text_parts = [str(value).strip() for value in captions if str(value).strip()]
            if text_parts:
                return "\n".join(text_parts)
    return ""
