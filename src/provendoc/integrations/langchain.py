"""LangChain adapter — ``ProvendocLoader`` (``BaseLoader``).

Drop-in loader for LangChain ingestion pipelines:

    from provendoc.integrations.langchain import ProvendocLoader

    loader = ProvendocLoader("document.md")
    docs = loader.load()          # list[langchain_core.documents.Document]

    # or with quality evaluation attached (metadata gains quality_overall)
    loader = ProvendocLoader("document.md", mode="evaluate")
    docs = loader.load()

Design:

- One loader instance per file (BaseLoader semantics: ``lazy()``
  yields the file's chunks; ``load()`` collects them).
- ``mode="chunk"`` (default) maps to ``Provendoc.chunk`` —
  clean semantic units, no evaluation.
- ``mode="evaluate"`` maps to ``Provendoc.evaluate`` — the
  same chunks plus evaluation/DQR context in metadata. Extra kwargs are
  forwarded as evaluate params (semantic-first or legacy).
- A pre-built ``Provendoc`` client can be injected; otherwise
  one is constructed lazily on first ``lazy()`` call.

The heavy ecosystem dependency (langchain-core) is imported inside
this module only — the rest of Provendoc never requires it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional

try:  # pragma: no cover - exercised via tests with the real package
    from langchain_core.document_loaders import BaseLoader
    from langchain_core.documents import Document
except ImportError as exc:  # noqa: TRY003 - actionable message
    raise ImportError(
        "langchain-core is required for the LangChain adapter: "
        "pip install langchain-core"
    ) from exc

from provendoc.integrations._common import result_to_items

__all__ = ["ProvendocLoader"]


class ProvendocLoader(BaseLoader):
    """Load one file's Provendoc semantic units as LangChain Documents.

    Args:
        file_path: path to the file to ingest.
        client: optional pre-built ``Provendoc`` instance
            (shared evaluators across loaders when supplied).
        mode: ``"chunk"`` (default) or ``"evaluate"``.
        params: extra kwargs forwarded to ``evaluate()`` in
            evaluate mode (e.g. ``size_soft_limit``); ignored in
            chunk mode.
    """

    def __init__(
        self,
        file_path,
        client: Optional[Any] = None,
        mode: str = "chunk",
        params: Optional[dict] = None,
    ) -> None:
        """Store constructor args; the SDK client is built lazily."""
        if mode not in ("chunk", "evaluate"):
            raise ValueError(
                f"mode must be 'chunk' or 'evaluate', got {mode!r}"
            )
        self._file_path = Path(file_path)
        self._client = client
        self._mode = mode
        self._params = dict(params or {})

    def _run(self):
        """Run the underlying SDK interface once."""
        if self._client is None:
            from provendoc.sdk.client import Provendoc

            self._client = Provendoc()
        if self._mode == "evaluate":
            return self._client.evaluate(
                str(self._file_path), **self._params
            )
        return self._client.chunk(str(self._file_path))

    def lazy_load(self) -> Iterator[Document]:
        """Yield one LangChain Document per semantic unit."""
        result = self._run()
        for text, metadata in result_to_items(
            result, source=str(self._file_path)
        ):
            yield Document(page_content=text, metadata=metadata)

    # older langchain-core versions (<0.2) hook ``lazy()`` instead
    # of ``lazy_load()``; aliasing covers both without cost.
    lazy = lazy_load
