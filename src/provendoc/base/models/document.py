from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Document:
    """Unified parser output document.

    Three-text-field provenance contract:
        source_text: the decoded file text as-is — the result of
            encoding detection, before BOM stripping, newline
            normalization, invisible-character sanitization, or any
            semantic cleaning. The "open the file in an editor" view;
            no downstream stage may modify it. Only text formats
            (txt/md/json/yaml/toml/ini/csv/html/log/jsonl) carry a
            value; binary formats (pdf/docx/excel) are None.
        original_text: the parse-output snapshot — after encoding
            detection, BOM stripping, newline normalization, and
            invisible-char sanitization. No later cleaning, repair,
            or representation step touches it. This is the field DQR
            (document quality report) evaluates.
        text: the working text. Equals original_text at parse time;
            becomes the processed version after semantic
            cleaning/repair. Chunking (representation) runs on this
            field.

    source_text / original_text / text layer on top of each other so
    every change from "raw file" → "parse snapshot" → "working text"
    is traceable; the original is never lost.
    """
    text: str
    metadata: dict = field(default_factory=dict)
    original_text: Optional[str] = None
    source_text: Optional[str] = None

    def __post_init__(self):
        # Parse-time snapshot: original_text always records parse output.
        if self.original_text is None:
            self.original_text = self.text
