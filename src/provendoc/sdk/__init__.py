"""Provendoc SDK — four interfaces + standalone export functions.

Public API:
    Provendoc — SDK client class, providing parse/chunk/evaluate/auto_pipeline

    Export functions (optional, in sdk/export.py):
    export_markdown, export_json, export_raw_document

Usage:
    from provendoc.sdk import Provendoc

    oi = Provendoc()
    result = oi.parse("file.txt")                        # pure parsing
    result = oi.chunk("file.txt")                        # parsing + default chunking
    result = oi.evaluate("file.txt", chunk_size=200)     # custom params + benchmark
    result = oi.auto_pipeline("file.txt")                # full evaluation + optimization

    # Optional: export results to disk (never automatic)
    from provendoc.sdk.export import export_markdown
    export_markdown(result, "output.md")
"""

from provendoc.sdk.client import Provendoc

__all__ = ["Provendoc"]
