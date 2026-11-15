"""Narrative representation public exports.

The PDF compatibility adapter is intentionally lazy.  Its implementation imports
``granularity`` from this package tree, so importing it eagerly here would create
a cycle when the PDF runtime imports the splitter directly.
"""

from typing import TYPE_CHECKING

from provendoc.core.representations.narrative.markdown_narrative_representation import (
    MarkdownNarrativeRepresentation,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    TxtNarrativeRepresentation,
)

if TYPE_CHECKING:
    from provendoc.core.representations.narrative.pdf_narrative_representation import (
        PdfNarrativeRepresentation,
    )


def __getattr__(name: str):
    if name == "PdfNarrativeRepresentation":
        from provendoc.core.representations.narrative.pdf_narrative_representation import (
            PdfNarrativeRepresentation,
        )

        return PdfNarrativeRepresentation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MarkdownNarrativeRepresentation",
    "TxtNarrativeRepresentation",
    "PdfNarrativeRepresentation",
]
