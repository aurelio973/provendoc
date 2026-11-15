# ============================================================
# Code Domain
# Shared parser base for language-specific entry points
# ============================================================
"""Shared BaseParser integration for language parsers.

Each language is exposed as its own class (PythonCodeParser, GoCodeParser, ...)
to keep clean SDK provenance and room to extend; the real tree traversal is
shared in TreeSitterCodeBackend so recovery contracts are not re-implemented
per language.
"""

from __future__ import annotations

import logging
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.code.code_decode_errors import (
    collect_code_decode_errors,
)
from provendoc.core.parsers.code.code_file_facts import (
    build_code_output_views,
    build_code_parse_config,
)
from provendoc.core.parsers.code.code_parse_status import (
    derive_code_parse_status,
)
from provendoc.core.parsers.code.code_unresolved import (
    derive_code_unresolved,
)
from provendoc.core.parsers.code.generic_backend import TreeSitterCodeBackend

_log = logging.getLogger(__name__)


class BaseCodeParser(BaseParser):
    """Shared base class for all per-language code parsers."""

    language = ""
    file_type = "code"
    backend_class = TreeSitterCodeBackend

    def __init__(self, encoding: str = "utf-8", tolerant: bool = True):
        """Initialize with encoding and tolerant switches, and instantiate the language backend."""
        self.encoding = encoding
        self.tolerant = tolerant
        self._backend = self.backend_class()

    def parse(self, file_path: str) -> Document:
        """Parse a code file into a Document.

        Syntax errors are recorded in metadata.code.diagnostics with recovered
        status, never as parse_failed; duration goes into metadata.parse_duration_ms.

        Args:
            file_path: path to the code file.

        Returns:
            Document: source code as text, structured parse in metadata.code.
        """
        start = time.perf_counter()
        ctx = self._read_and_diagnose(
            file_path,
            self.file_type,
            self.encoding,
            self.tolerant,
        )
        backend_result = self._backend.parse(ctx.text, self.language)
        structure = backend_result.structure
        metadata = ctx.metadata
        metadata["code"] = structure.to_metadata()
        metadata["code"]["language"] = self.language

        # Object-level unresolved registry (code_unresolved_prereg_v1, frozen):
        # derived 1:1 from the metadata["code"] diagnostics export window;
        # non-empty writes only, silent degradation (same wiring discipline
        # as the config domain). This method is the single wiring point for
        # 30+ language parsers (vue/html/sql only override backend_class).
        try:
            stamp_unresolved_registry(
                metadata,
                "code_unresolved",
                derive_code_unresolved(metadata["code"]),
            )
        except Exception as exc:
            _log.debug("code unresolved registry skipped: %s", exc)

        # The four engineering facts (code_engineering_facts_prereg_v1, frozen
        # 2026-10-25): document-level three-state parse_status, byte-exact
        # decode_errors + utf8_corrupt_suspected probe, output_views two-view
        # declaration, and the parse_config reproducibility snapshot. All
        # additive / non-empty writes only / silent degradation — same single
        # wiring point as above.
        try:
            metadata["parse_status"] = derive_code_parse_status(metadata)
        except Exception as exc:
            _log.debug("code parse status derivation skipped: %s", exc)

        try:
            decode_errors = collect_code_decode_errors(
                file_path, metadata.get("encoding_used")
            )
            if decode_errors is not None:
                metadata["decode_errors"] = decode_errors
        except Exception as exc:
            _log.debug("code decode error registry skipped: %s", exc)

        try:
            metadata["output_views"] = build_code_output_views()
        except Exception as exc:
            _log.debug("code output views declaration skipped: %s", exc)

        try:
            metadata["parse_config"] = build_code_parse_config(
                metadata,
                type(self).__name__,
                self.language,
                self.encoding,
                self.tolerant,
            )
        except Exception as exc:
            _log.debug("code parse config declaration skipped: %s", exc)

        metadata["parse_duration_ms"] = round((time.perf_counter() - start) * 1000, 3)

        # Deliberately no parse_failed for syntax diagnostics. In the code
        # domain "recovered" is a structured success result; callers decide
        # compilability from metadata.code.status.
        if structure.diagnostics:
            metadata["warnings"].append(
                "code parser recovered "
                f"{len(structure.diagnostics)} syntax diagnostic(s); "
                "see metadata.code.diagnostics"
            )

        return Document(
            text=ctx.text,
            metadata=metadata,
            source_text=ctx.source_text,
        )
