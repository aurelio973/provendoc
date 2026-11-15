"""Evidence extractors for the PDF ingestion runtime."""

from provendoc.core.parsers.narrative.pdf.evidence.annotations import (
    AnnotationExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.figures import (
    FigureEvidenceExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.file_integrity import (
    FileIntegrityExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.image_assets import (
    PDFImageAssetExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.images import ImageRegionExtractor
from provendoc.core.parsers.narrative.pdf.evidence.native import NativePDFExtractor
from provendoc.core.parsers.narrative.pdf.evidence.navigation import (
    NavigationEvidence,
    NavigationExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.ocr import (
    OCRBackendUnavailable,
    OCRPDFExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.page_geometry import (
    PageGeometryExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.tables import (
    NativeTableExtractor,
    SharedTableFinder,
    SharedTableFinderConsumer,
)
from provendoc.core.parsers.narrative.pdf.evidence.text_characters import (
    TextCharacterExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.watermark import (
    WatermarkExtractor,
)

__all__ = [
    "NativePDFExtractor",
    "AnnotationExtractor",
    "ImageRegionExtractor",
    "PDFImageAssetExtractor",
    "FigureEvidenceExtractor",
    "FileIntegrityExtractor",
    "NativeTableExtractor",
    "SharedTableFinder",
    "SharedTableFinderConsumer",
    "TextCharacterExtractor",
    "WatermarkExtractor",
    "PageGeometryExtractor",
    "NavigationExtractor",
    "NavigationEvidence",
    "OCRPDFExtractor",
    "OCRBackendUnavailable",
]
