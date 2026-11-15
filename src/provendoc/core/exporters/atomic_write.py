"""Atomic file writes for every export path (temp + os.replace).

All disk writes in ``sdk/export.py`` and ``core/exporters/*`` share
this single implementation (single source of truth — no second
atomic-write variant). Writing always takes two steps:

1. write a sibling temp file ``{name}.tmp-{pid}`` and ``fsync``;
2. ``os.replace`` atomically replaces the target (on Windows this
   maps to ``MoveFileEx(MOVEFILE_REPLACE_EXISTING)`` semantics).

If the process crashes at any moment, the disk holds either the
complete old file or the complete new file — never a partial file.
A failed temp write is cleaned up and the target is untouched.

``fsync`` only covers the file body; directory fsync (rename
durability on POSIX) has no equivalent on Windows and a
cross-platform implementation is not worth it — losing the rename
itself within the crash window just falls back to the old file,
which is still safe; only the new file becomes visible a bit
later, atomicity is unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_TEXT = "utf-8"


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Atomically replace ``path`` with ``text`` (utf-8)."""

    target = Path(path)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding=_TEXT, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Any failure (including KeyboardInterrupt): clean up the
        # half-written temp file; the target stays as-is.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    indent: int = 2,
) -> None:
    """Atomically replace ``path`` with ``data`` rendered as JSON.

    Same serialization parameters as the previous non-atomic writers
    (``ensure_ascii=False``, ``indent``, ``default=str``) so output
    bytes are byte-identical to before — only the write protocol
    changed.
    """

    atomic_write_text(
        path,
        json.dumps(
            data,
            ensure_ascii=False,
            indent=indent,
            default=str,
        ),
    )


__all__ = ["atomic_write_text", "atomic_write_json"]
