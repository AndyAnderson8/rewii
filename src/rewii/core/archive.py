"""One safe zip extractor shared by everything that unpacks untrusted archives.

Rejected outright (:class:`ArchiveError`): absolute paths, ``..``, a ``:`` in
*any* path component (Windows drive letters / alternate streams), two entries
that only differ by upper/lower case, archives whose contents exceed a size
cap, and anything that would resolve outside the destination folder.

Skipped but harmless (listed in ``Extracted.skipped``): entries whose names
can't exist on FAT32/Windows - control or ``*?"<>|`` characters, reserved
device names (``CON``, ``NUL``, ``COM1`` ...), names ending in a dot or space.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections.abc import Callable, Sequence

from .fileops import is_reserved_name, part_path
from .progress import CancelToken, ProgressFn, Reporter
from .errors import ReWiiError

MAX_UNCOMPRESSED = 4 * 1024 ** 3      # zip-bomb guard
CHUNK = 256 * 1024

_BAD_CHARS = set(':*?"<>|')


class ArchiveError(ReWiiError):
    """The archive is unsafe or unreadable."""


class UnusableName(ArchiveError):
    """An entry whose name can't exist on a FAT32/Windows drive (odd characters, a
    reserved device name, a trailing dot...). Harmless, so extraction skips it
    instead of refusing the whole archive."""


def safe_parts(name: str) -> Optional[list[str]]:
    """Validated path components of an archive entry; None for directory entries.

    Raises :class:`ArchiveError` for anything unsafe (see the module docstring).
    """
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    is_dir = n.endswith("/")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or ":" in p:           # traversal / drive letters / alternate streams
            raise ArchiveError(f"unsafe path in archive: {name!r}")
    for p in parts:
        if any(c in _BAD_CHARS for c in p) or any(ord(c) < 32 for c in p):
            raise UnusableName(f"name not allowed on the drive: {name!r}")
        if p.rstrip(" .") != p:
            raise UnusableName(f"invalid FAT32 name in archive: {name!r}")
        if is_reserved_name(p):
            raise UnusableName(f"reserved file name in archive: {name!r}")
    if is_dir or not parts:
        return None
    return parts


@dataclass
class Extracted:
    """What :func:`extract_zip` did."""
    written: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)    # entries ``allow`` turned down


Mapping = Callable[[Sequence[str]], dict[str, str]]


def _identity(names: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for n in names:
        parts = safe_parts(n)
        if parts is not None:
            out[n] = "/".join(parts)
    return out


def plan_zip(z: zipfile.ZipFile, mapping: Optional[Mapping] = None,
             max_total: int = MAX_UNCOMPRESSED,
             skipped: Optional[list[str]] = None) -> list[tuple[zipfile.ZipInfo, str]]:
    """Validate every entry of ``z`` and return ``(info, destination)`` pairs
    (POSIX paths relative to the extraction root) for its files.

    ``mapping`` turns the entry names into destinations (default: unchanged);
    entries it leaves out are dropped. Destinations are validated again.
    """
    usable: dict[str, zipfile.ZipInfo] = {}          # an exact repeat of a name: the last one wins
    for i in z.infolist():
        try:
            safe_parts(i.filename)                   # directories are checked too
        except UnusableName:
            if skipped is not None and not i.is_dir():
                skipped.append(i.filename)
            continue
        usable[i.filename] = i
    files = [i for i in usable.values() if not i.is_dir()]
    if sum(i.file_size for i in files) > max_total:
        raise ArchiveError("archive is unreasonably large")
    plan = (mapping or _identity)([i.filename for i in files])
    out: list[tuple[zipfile.ZipInfo, str]] = []
    seen: dict[str, str] = {}
    for info in files:
        rel = plan.get(info.filename)
        if rel is None:
            continue
        parts = safe_parts(rel)
        if parts is None:
            raise ArchiveError(f"unsafe path in archive: {info.filename!r}")
        rel = "/".join(parts)
        key = rel.casefold()
        if key in seen:
            raise ArchiveError(f"archive has the same file twice: {seen[key]!r} and "
                               f"{info.filename!r}")
        seen[key] = info.filename
        out.append((info, rel))
    # a file may not also be used as a folder ("a" and "a/b")
    for key in seen:
        parent = key.rpartition("/")[0]
        while parent:
            if parent in seen:
                raise ArchiveError(f"archive uses {seen[parent]!r} as both a file and a folder")
            parent = parent.rpartition("/")[0]
    return out


def extract_zip(zip_path: Path | str, dest_root: Path | str, *,
                mapping: Optional[Mapping] = None,
                allow: Optional[Callable[[str], bool]] = None,
                max_total: int = MAX_UNCOMPRESSED,
                progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None) -> Extracted:
    """Safely extract ``zip_path`` under ``dest_root``.

    ``allow(destination)`` may turn individual files down (they are listed in
    :attr:`Extracted.skipped`). Files are written through a ``.part`` temporary
    name; nothing is written if validation fails.
    """
    res = Extracted()
    root = Path(dest_root).resolve()
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ArchiveError(f"{Path(zip_path).name} is not a valid zip") from e
    with z:
        todo: list[tuple[zipfile.ZipInfo, Path]] = []
        for info, rel in plan_zip(z, mapping, max_total, res.skipped):
            if allow is not None and not allow(rel):
                res.skipped.append(rel)
                continue
            target = root.joinpath(*rel.split("/"))
            if not target.resolve().is_relative_to(root) or target.resolve() == root:
                raise ArchiveError(f"unsafe path in archive: {info.filename!r}")
            todo.append((info, target))
        total = sum(i.file_size for i, _ in todo)
        rep = Reporter(progress, cancel, total=total, message="Extracting")
        rep.set_total(total)
        budget = max_total
        for info, target in todo:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = part_path(target)
            try:
                with z.open(info) as src, open(tmp, "wb") as dst:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        budget -= len(chunk)
                        if budget < 0:
                            raise ArchiveError("archive is unreasonably large")
                        dst.write(chunk)
                        rep.advance(len(chunk), f"Extracting {target.name}")
                tmp.replace(target)
            except zipfile.BadZipFile as e:
                tmp.unlink(missing_ok=True)
                raise ArchiveError(f"{Path(zip_path).name} is damaged: {e}") from e
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            res.written.append(target)
    return res
