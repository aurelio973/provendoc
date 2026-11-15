# ============================================================
# Archive Domain (collection-stage)
# Collection-stage archive expander
# ============================================================
"""Archive expansion at collection time.

Design positioning:
    archive is not a parse domain (there is no archive semantic representation), it is a collection-stage
    action — at the file-collection stage archives are expanded into member file lists, each member flowing
    as usual to the matching domain's parser, finally merged into the output together with ordinary files.

Security (defensive caps, all configurable):
    - path traversal: normalized member names reject absolute paths / ".." / drive letters;
      the on-disk path is a random uuid, always inside work_dir
    - zip bomb: member-count cap / per-member extraction cap / total extraction cap
    - symlinks / device files: tar members that are not regular files are always skipped
    - nesting depth: archives inside archives expand recursively, stopping beyond max_depth

Honesty:
    Expansion facts (member counts/skip reasons per archive) go into the run-level
    artefacts by the caller, never silently dropped.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import tarfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# zip family (readable by zipfile)
# .epub is not on this list: it is a narrative container (EPUB book), extracted spine-order
# by the narrative domain's EpubParser, not expanded as an ordinary archive.
ZIP_SUFFIXES = {".zip", ".jar", ".war", ".ear", ".apk", ".whl", ".egg"}
# tar family (compound suffixes included, matched by endswith)
TAR_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz")
TAR_SUFFIXES = {".tar"}
# Single-file compression streams (after extraction, flow by suffix-stripped name)
STREAM_SUFFIXES = {".gz", ".bz2", ".xz"}

ARCHIVE_SUFFIXES = ZIP_SUFFIXES | TAR_SUFFIXES | STREAM_SUFFIXES


def is_archive(path: Path) -> bool:
    """Whether the file is an expandable archive (compound suffixes take precedence)."""
    name = path.name.lower()
    if name.endswith(TAR_COMPOUND_SUFFIXES):
        return True
    return path.suffix.lower() in (
        ZIP_SUFFIXES | TAR_SUFFIXES | STREAM_SUFFIXES
    )


@dataclass
class ArchiveInfo:
    """Expansion facts of a single archive (goes into run-level artefacts)."""

    archive_path: str                 # virtual path relative to the dataset
    format: str                       # zip / tar / gzip / bz2 / xz
    member_count: int = 0             # total members in the archive
    extracted: int = 0                # members actually written to disk
    skipped_unsupported: int = 0      # members with unsupported suffixes
    skipped_oversize: int = 0         # over the per-member size cap
    skipped_unsafe: int = 0           # path traversal / non-regular files
    skipped_duplicate: int = 0        # virtual path conflicts
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        """Serialize into a dict."""
        return {
            "archive": self.archive_path,
            "format": self.format,
            "members": self.member_count,
            "extracted": self.extracted,
            "skipped": {
                "unsupported": self.skipped_unsupported,
                "oversize": self.skipped_oversize,
                "unsafe": self.skipped_unsafe,
                "duplicate": self.skipped_duplicate,
            },
            "notes": self.notes,
        }


@dataclass
class ExpansionLimits:
    """Expansion quotas (nesting depth, member count, per-member and total bytes)."""

    max_depth: int = 3
    max_members_per_archive: int = 1000
    max_member_bytes: int = 200 * 1024 * 1024        # 200MB
    max_total_bytes: int = 4 * 1024 * 1024 * 1024    # 4GB


class ArchiveExpander:
    """Expand archives into a member file list of (real path, virtual relative path).

    Usage::

        expander = ArchiveExpander(work_dir, supported_suffixes)
        entries, infos = expander.expand(archives)
        # entries = [(member real path, member virtual relative path Path)]
        try:
            for real, virtual in entries:
                process(real, relative_override=virtual)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    """

    def __init__(
        self,
        work_dir: Path,
        supported_suffixes: Sequence[str],
        limits: Optional[ExpansionLimits] = None,
    ):
        """Initialize with the work dir, supported suffixes and expansion quotas."""
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.supported = {s.lower() for s in supported_suffixes}
        self.limits = limits or ExpansionLimits()
        self._total_bytes = 0
        # Nesting-depth over-limit counter (expansion fact)
        self.depth_stops = 0
        # Virtual path uniqueness (same-name member protection)
        self._seen_virtual: set = set()

    # ========================================================
    # public
    # ========================================================

    def expand(
        self, archives: Iterable[Tuple[Path, str]],
    ) -> Tuple[List[Tuple[Path, Path]], List[Dict]]:
        """Expand a list of archives.

        Args:
            archives: (real path, virtual relative path) pairs; the virtual path is the archive's
                own posix path relative to the dataset

        Returns:
            (entries, infos): entries = [(member real path, member virtual relative
            path)], infos = a list of ArchiveInfo.as_dict()
        """
        entries: List[Tuple[Path, Path]] = []
        infos: List[Dict] = []
        for real, virtual in archives:
            info = ArchiveInfo(archive_path=virtual, format=self._format_of(real))
            self._expand_one(real, virtual, entries, info, depth=1)
            if info.extracted == 0 and info.member_count > 0:
                info.notes.append("no_supported_members")
            infos.append(info.as_dict())
        return entries, infos

    # ========================================================
    # dispatch
    # ========================================================

    @staticmethod
    def _format_of(path: Path) -> str:
        name = path.name.lower()
        if name.endswith(TAR_COMPOUND_SUFFIXES) or path.suffix.lower() == ".tar":
            return "tar"
        if path.suffix.lower() == ".gz":
            return "gzip"
        if path.suffix.lower() == ".bz2":
            return "bz2"
        if path.suffix.lower() == ".xz":
            return "xz"
        return "zip"

    def _expand_one(
        self,
        archive_path: Path,
        virtual_prefix: str,
        entries: List[Tuple[Path, Path]],
        info: ArchiveInfo,
        depth: int,
    ) -> None:
        if depth > self.limits.max_depth:
            self.depth_stops += 1
            return
        fmt = self._format_of(archive_path)
        if fmt == "tar":
            self._expand_tar(archive_path, virtual_prefix, entries, info, depth)
        elif fmt == "zip":
            self._expand_zip(archive_path, virtual_prefix, entries, info, depth)
        else:
            self._expand_stream(archive_path, virtual_prefix, entries, info, depth)

    # ========================================================
    # Member-name safety
    # ========================================================

    @staticmethod
    def _sanitize_member_name(name: str) -> Optional[List[str]]:
        """Member name → a safe path-segment list; returns None when unsafe.

        Rules: posix segmentation; strip "." and empty segments; reject "..", absolute paths,
        drive letters (C:), backslash injection, overlong segments.
        """
        if not name or name.startswith("/") or "\\" in name:
            return None
        if len(name) > 2 and name[1] == ":":
            return None
        parts = [p for p in PurePosixPath(name).parts if p not in (".", "")]
        if any(p == ".." for p in parts):
            return None
        if not parts or any(len(p) > 255 for p in parts):
            return None
        return parts

    def _reserve_virtual(self, virtual: str) -> bool:
        """Register a virtual path; returns False when it already exists (duplicate protection)."""
        if virtual in self._seen_virtual:
            return False
        self._seen_virtual.add(virtual)
        return True

    def _stage_path(self, member_name: str) -> Path:
        """Member on-disk path: random uuid prefix + the original suffix kept.

        Keeping the suffix is a hard contract: downstream parsers route by suffix, and the nested-archive
        check (is_archive) also depends on the suffix — a pure-uuid on-disk name would leave every member
        reporting Unsupported file type.
        """
        return self.work_dir / (uuid.uuid4().hex + Path(member_name).suffix)

    # ========================================================
    # Member on-disk write (streaming + caps)
    # ========================================================

    def _write_stream(self, source, target: Path) -> int | str | None:
        """Stream-copy source → target; returns the bytes written, None when the per-member cap is exceeded, "total" when the total cap is exceeded."""
        written = 0
        with open(target, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > self.limits.max_member_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    return None
                if self._total_bytes + written > self.limits.max_total_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    return "total"
                out.write(chunk)
        self._total_bytes += written
        return written

    def _emit_member(
        self,
        member_path: Path,
        member_name: str,
        virtual: str,
        entries: List[Tuple[Path, Path]],
        info: ArchiveInfo,
        depth: int,
    ) -> None:
        """Post-write member dispatch: nested archives recurse / supported suffixes register / the rest are deleted."""
        if is_archive(member_path):
            self._expand_one(member_path, virtual, entries, info, depth + 1)
            member_path.unlink(missing_ok=True)
            return
        if self._suffix_supported(member_name.lower()):
            info.extracted += 1
            entries.append((member_path, Path(virtual)))
        else:
            info.skipped_unsupported += 1
            member_path.unlink(missing_ok=True)

    def _suffix_supported(self, lower_name: str) -> bool:
        """Whether the member name has a supported parseable suffix (compound suffixes included)."""
        if lower_name.endswith(TAR_COMPOUND_SUFFIXES):
            return True
        if lower_name.endswith(".tar"):
            return True
        dot = lower_name.rfind(".")
        return dot != -1 and lower_name[dot:] in self.supported

    # ========================================================
    # tar
    # ========================================================

    def _expand_tar(
        self, archive_path: Path, virtual_prefix: str,
        entries: List[Tuple[Path, Path]], info: ArchiveInfo, depth: int,
    ) -> None:
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                members = tf.getmembers()
                info.member_count += len(members)
                if len(members) > self.limits.max_members_per_archive:
                    info.notes.append("member_count_truncated")
                for member in members[: self.limits.max_members_per_archive]:
                    if not member.isfile():
                        info.skipped_unsafe += 1
                        continue
                    if member.size > self.limits.max_member_bytes:
                        info.skipped_oversize += 1
                        continue
                    parts = self._sanitize_member_name(member.name)
                    if parts is None:
                        info.skipped_unsafe += 1
                        continue
                    src = tf.extractfile(member)
                    if src is None:
                        info.skipped_unsafe += 1
                        continue
                    member_name = "/".join(parts)
                    virtual = (
                        f"{virtual_prefix}/{member_name}"
                        if virtual_prefix else member_name
                    )
                    if not self._reserve_virtual(virtual):
                        info.skipped_duplicate += 1
                        continue
                    target = self._stage_path(member_name)
                    result = self._write_stream(src, target)
                    if result is None:
                        info.skipped_oversize += 1
                    elif result == "total":
                        info.notes.append("total_size_limit_reached")
                        return
                    else:
                        self._emit_member(
                            target, member_name, virtual, entries, info, depth,
                        )
        except (tarfile.TarError, OSError, EOFError) as e:
            info.notes.append(f"tar_open_failed: {e}")

    # ========================================================
    # zip
    # ========================================================

    def _expand_zip(
        self, archive_path: Path, virtual_prefix: str,
        entries: List[Tuple[Path, Path]], info: ArchiveInfo, depth: int,
    ) -> None:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                names = zf.namelist()
                info.member_count += len(names)
                if len(names) > self.limits.max_members_per_archive:
                    info.notes.append("member_count_truncated")
                for name in names[: self.limits.max_members_per_archive]:
                    if name.endswith("/"):
                        continue  # directory entry
                    parts = self._sanitize_member_name(name)
                    if parts is None:
                        info.skipped_unsafe += 1
                        continue
                    try:
                        if zf.getinfo(name).file_size > self.limits.max_member_bytes:
                            info.skipped_oversize += 1
                            continue
                        src = zf.open(name)
                    except (KeyError, zipfile.BadZipFile):
                        info.skipped_unsafe += 1
                        continue
                    member_name = "/".join(parts)
                    virtual = (
                        f"{virtual_prefix}/{member_name}"
                        if virtual_prefix else member_name
                    )
                    if not self._reserve_virtual(virtual):
                        info.skipped_duplicate += 1
                        continue
                    target = self._stage_path(member_name)
                    result = self._write_stream(src, target)
                    if result is None:
                        info.skipped_oversize += 1
                    elif result == "total":
                        info.notes.append("total_size_limit_reached")
                        return
                    else:
                        self._emit_member(
                            target, member_name, virtual, entries, info, depth,
                        )
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            info.notes.append(f"zip_open_failed: {e}")

    # ========================================================
    # Single-file compression streams (.gz / .bz2 / .xz)
    # ========================================================

    def _expand_stream(
        self, archive_path: Path, virtual_prefix: str,
        entries: List[Tuple[Path, Path]], info: ArchiveInfo, depth: int,
    ) -> None:
        fmt = self._format_of(archive_path)
        openers: Dict[str, Callable[..., Any]] = {
            "gzip": gzip.open,
            "bz2": bz2.open,
            "xz": lzma.open,
        }
        opener = openers.get(fmt)
        if opener is None:
            info.notes.append(f"unknown_stream_format: {archive_path.suffix}")
            return
        info.member_count += 1

        suffix = archive_path.suffix.lower()
        member_name = archive_path.name[: -len(suffix)]
        if not member_name:
            info.skipped_unsafe += 1
            return
        virtual = (
            f"{virtual_prefix}/{member_name}"
            if virtual_prefix else member_name
        )
        if not self._reserve_virtual(virtual):
            info.skipped_duplicate += 1
            return

        target = self._stage_path(member_name)
        try:
            with opener(archive_path, "rb") as src:
                result = self._write_stream(src, target)
        except (OSError, EOFError, lzma.LZMAError) as e:
            info.notes.append(f"stream_decompress_failed: {e}")
            target.unlink(missing_ok=True)
            return
        if result is None:
            info.skipped_oversize += 1
        elif result == "total":
            info.notes.append("total_size_limit_reached")
            return
        else:
            self._emit_member(target, member_name, virtual, entries, info, depth)
