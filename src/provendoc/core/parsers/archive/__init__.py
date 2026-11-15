"""Archive domain (collection-stage): archive expansion at collection time."""

from provendoc.core.parsers.archive.expander import (
    ARCHIVE_SUFFIXES,
    ArchiveExpander,
    ExpansionLimits,
    is_archive,
)

__all__ = [
    "ARCHIVE_SUFFIXES",
    "ArchiveExpander",
    "ExpansionLimits",
    "is_archive",
]
