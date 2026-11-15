"""Provendoc — evidence-first, domain-aware ingestion pipeline with
auditable Representation Stack Evaluation (RSE).

Public API:
    Provendoc — SDK client: parse / chunk / evaluate / auto_pipeline
    UniversalParser — multi-format parser facade
    export_markdown / export_json / export_raw_document — optional persistence

Usage:
    from provendoc import Provendoc

    ad = Provendoc()
    result = ad.parse("file.txt")

The CLI entry point lives in ``provendoc.app`` (console script
``provendoc``); the repo-root ``app.py`` is a thin development shim.
"""

import logging as _logging

from provendoc.core.pipeline.universal_parser import UniversalParser
from provendoc.sdk.client import Provendoc
from provendoc.sdk.export import (
    export_json,
    export_markdown,
    export_raw_document,
)

# Logging convention (library-wide):
#   - modules use a module-level `_log = logging.getLogger(__name__)`
#     with lazy %s interpolation;
#   - library code never calls basicConfig / adds StreamHandler -- output
#     setup is the entry point's job. The package root attaches a
#     NullHandler so embedding the SDK stays silent on the host app's
#     stderr;
#   - entry points configure output: CLI defaults to WARNING, the API
#     server to INFO; PROVENDOC_LOG_LEVEL overrides both.
_logging.getLogger(__name__).addHandler(_logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "Provendoc",
    "UniversalParser",
    "export_markdown",
    "export_json",
    "export_raw_document",
    "__version__",
]
