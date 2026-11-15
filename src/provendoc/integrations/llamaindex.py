"""LlamaIndex adapter — ``ProvendocReader`` (``BaseReader``).

Drop-in reader for LlamaIndex ingestion pipelines:

    from provendoc.integrations.llamaindex import ProvendocReader

    reader = ProvendocReader()
    documents = reader.load_data(input_file="document.md")

    # or with quality evaluation attached (metadata gains quality_overall)
    reader = ProvendocReader(mode="evaluate")
    documents = reader.load_data(
        input_file="document.md", extra_info={"tenant": "acme"}
    )

Design:

- ``load_data(input_file, extra_info)`` follows the BaseReader
  convention; ``extra_info`` entries are merged into every
  document's metadata (caller-owned keys win on collision).
- ``mode="chunk"`` (default) maps to ``Provendoc.chunk``;
  ``mode="evaluate"`` maps to ``Provendoc.evaluate`` with
  ``params`` forwarded (semantic-first or legacy).
- A pre-built ``Provendoc`` client can be injected; otherwise
  one is constructed lazily on first ``load_data`` call.

The heavy ecosystem dependency (llama-index-core) is imported
inside this module only — the rest of Provendoc never
requires it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

try:  # pragma: no cover - exercised via tests with the real package
    from llama_index.core.readers.base import BaseReader
    from llama_index.core.schema import Document
except ImportError as exc:  # noqa: TRY003 - actionable message
    raise ImportError(
        "llama-index-core is required for the LlamaIndex adapter: "
        "pip install llama-index-core"
    ) from exc

from provendoc.integrations._common import result_to_items

__all__ = ["ProvendocReader"]


class ProvendocReader(BaseReader):
    """Read one file's Provendoc semantic units as LlamaIndex Documents.

    Args:
        client: optional pre-built ``Provendoc`` instance
            (shared evaluators across readers when supplied).
        mode: ``"chunk"`` (default) or ``"evaluate"``.
        params: extra kwargs forwarded to ``evaluate()`` in
            evaluate mode (e.g. ``size_soft_limit``); ignored in
            chunk mode.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        mode: str = "chunk",
        params: Optional[dict] = None,
    ) -> None:
        """Store constructor args; the SDK client is built lazily."""
        if mode not in ("chunk", "evaluate"):
            raise ValueError(
                f"mode must be 'chunk' or 'evaluate', got {mode!r}"
            )
        self._client = client
        self._mode = mode
        self._params = dict(params or {})

    def _run(self, file_path: Path):
        """Run the underlying SDK interface once."""
        if self._client is None:
            from provendoc.sdk.client import Provendoc

            self._client = Provendoc()
        if self._mode == "evaluate":
            return self._client.evaluate(
                str(file_path), **self._params
            )
        return self._client.chunk(str(file_path))

    def load_data(
        self,
        input_file: Optional[Any] = None,
        extra_info: Optional[dict] = None,
    ) -> List[Document]:
        """Return one LlamaIndex Document per semantic unit.

        Args:
            input_file: path to the file to ingest (required here —
                unlike file-less readers, this adapter has no
                remote/default source).
            extra_info: optional caller-owned metadata merged into
                every document (caller keys win on collision).

        Raises:
            ValueError: if ``input_file`` is not supplied.
        """
        if input_file is None:
            raise ValueError(
                "ProvendocReader.load_data requires input_file "
                "(the file path to ingest)"
            )
        file_path = Path(input_file)
        result = self._run(file_path)
        documents: List[Document] = []
        for text, metadata in result_to_items(
            result, source=str(file_path)
        ):
            if extra_info:
                merged = dict(metadata)
                merged.update(extra_info)
                metadata = merged
            documents.append(
                Document(text=text, metadata=metadata)
            )
        return documents
