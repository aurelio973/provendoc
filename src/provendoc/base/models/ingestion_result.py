from dataclasses import dataclass, field
from typing import List

from provendoc.base.models.document import Document
from provendoc.base.models.representation_unit import RepresentationUnit


def _unit_list(content) -> List[RepresentationUnit]:
    """Normalize the three legal shapes of representation ``content``
    into ``List[RepresentationUnit]``.

    ``RepresentationResult.content`` is canonically a list; narrative
    representations use the three-granularity container
    ``MultiGranularityOutput``; document representations are dicts.
    Callers always get a list; container choice is expressed
    explicitly via ``granularity=``, not hasattr probing.
    """
    if content is None or isinstance(content, dict):
        return []
    # Deferred import: granularity lives in the core layer; the base
    # layer must not depend on it in the other direction.
    from provendoc.core.representations.narrative.granularity import (
        MultiGranularityOutput,
    )

    if isinstance(content, MultiGranularityOutput):
        return list(content.base)
    if isinstance(content, list):
        return content
    return []


@dataclass
class IngestionResult:
    """
    ingestion pipeline unified output
    """
    # parser output
    document: Document

    # representation registry
    representations: dict = field(default_factory=dict)

    # pipeline metadata
    metadata: dict = field(default_factory=dict)

    # future extensions
    extra: dict = field(default_factory=dict)

    @property
    def text(self):
        """Body text (passes through ``document.text``)."""
        return self.document.text

    @property
    def warnings(self):
        """Diagnostic warnings (metadata["warnings"], else empty list)."""
        return self.document.metadata.get("warnings", [])

    @property
    def headings(self):
        """Heading list (metadata["headings"], else empty list)."""
        return self.document.metadata.get("headings", [])

    @property
    def file_type(self):
        """File type tag (metadata["file_type"], e.g. "markdown"/"pdf")."""
        return self.document.metadata.get("file_type")

    @property
    def encoding(self):
        """Encoding actually used for decoding (metadata["encoding_used"])."""
        return self.document.metadata.get("encoding_used")

    @property
    def chunks(self) -> List[RepresentationUnit]:
        """Unified semantic representation — canonical type is RepresentationUnit.

        For backward compatibility, this property name is kept as 'chunks',
        but the returned objects are full RepresentationUnit instances.

        Always returns a base-layer list; narrative ``content`` used to be
        returned raw here, making ``len(result.chunks)`` a TypeError on
        markdown/docx/tex while PDF/spreadsheet returned a list. Use
        ``chunks_at(granularity)`` for coarse/fine layers.
        """
        return self.chunks_at("base")

    def chunks_at(self, granularity: str = "base") -> List[RepresentationUnit]:
        """Return semantic units at a granularity layer; always a list.

        Args:
            granularity: "base" (default) / "coarse" / "fine".
                Representations without a granularity container
                (PDF/spreadsheet etc.) have a single layer — any value
                returns it; a missing layer returns an empty list,
                never raises.

        Returns:
            List[RepresentationUnit], document order.
        """
        semantic = self.representations.get("semantic")
        if not semantic:
            return []
        content = semantic.content

        from provendoc.core.representations.narrative.granularity import (
            MultiGranularityOutput,
        )

        if isinstance(content, MultiGranularityOutput):
            return list(getattr(content, granularity, []) or [])
        return _unit_list(content)

    @property
    def texts(self):
        """Plain-text list of semantic units (``[u.text for u in self.chunks]``).

        For callers that just want a list of strings to feed a model —
        separated from the analytic ``chunks`` so they need not know
        what a RepresentationUnit is.
        """
        return [unit.text for unit in self.chunks]

    @property
    def document_representation(self):
        """Document-representation content (else empty dict)."""
        document_repr = self.representations.get("document")
        if document_repr:
            return document_repr.content
        return {}

    @property
    def semantic_signals(self):
        """Semantic signals (metadata["semantic_signals"], else empty dict)."""
        return self.document.metadata.get(
            "semantic_signals",
            {}
        )

    @property
    def hidden_unicode(self):
        """Hidden-Unicode diagnostic details (diagnostics.hidden_unicode,
        else empty list)."""
        diagnostics = self.document.metadata.get(
            "diagnostics",
            {}
        )
        return diagnostics.get(
            "hidden_unicode",
            []
        )

    def get_representation(self, name):
        """Return the named representation result (else None)."""
        return self.representations.get(name)
