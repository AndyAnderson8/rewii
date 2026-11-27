"""Small, shared file helpers: hashing, atomic copies and safe path joins.

Everything that copies user data onto a drive or into the library goes through
:func:`copy_atomic` (temp file + rename, so a failed or cancelled copy never
leaves a half-written file under the real name), and every relative path read
from a manifest or archive goes through :func:`safe_join`.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import shutil
import sys
import threading
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .progress import Reporter
from .errors import ReWiiError

CHUNK = 4 * 1024 * 1024

# ---- the temp-name convention (everything here is recognised by is_temp_name) ----
#: A FILE that is still being written: ``<final name>.rewii-part`` next to its final name
#: (see :func:`part_path`, :func:`write_atomic`, :func:`copy_atomic`).
PART_SUFFIX = ".rewii-part"
#: A FOLDER that is still being filled (a snapshot, a NAND archive): ``<name>.partial``.
PARTIAL_DIR_SUFFIX = ".partial"
#: The two names used while a finished file/folder is swapped in for an existing one.
NEW_SUFFIX, OLD_SUFFIX = ".rewii-new", ".rewii-old"
#: Prefix of game staging files written next to their final name (see ``games``).
TEMP_PREFIX = ".rewii-tmp-"
#: Every suffix that marks something unfinished. ``.part`` and ``.rewii-tmp`` are no longer
#: written but may be left on a drive by older versions.
TEMP_SUFFIXES = (PART_SUFFIX, ".part", PARTIAL_DIR_SUFFIX, NEW_SUFFIX, OLD_SUFFIX, ".rewii-tmp")
#: A leftover temp file is only cleaned up when nothing has written to it for this long.
TEMP_IDLE_SECONDS = 3600


class UnsafePath(ReWiiError, ValueError):
    """A relative path that would escape its base folder."""


class DriveNotConnected(ReWiiError, OSError):
    """The drive's root folder is gone (unplugged, unmounted). Nothing is ever
    created in its place - that would silently write to the computer's own disk."""

    def __init__(self, root: Path | str) -> None:
        super().__init__(errno.ENODEV, "the drive isn't connected", str(root))
        self.root = Path(root)

    def __str__(self) -> str:
        return (f"The drive {self.root} isn't connected. Plug it in (and wait until the computer "
                "shows it), then try again.")


def require_drive(root: Path | str) -> Path:
    """``root`` if that folder is there, else raise :class:`DriveNotConnected`.

    Every function that writes to a drive calls this first: ``mkdir(parents=True)``
    on a vanished mount point would otherwise re-create it on the computer's disk."""
    root = Path(root)
    try:
        ok = root.is_dir()
    except OSError:
        ok = False
    if not ok:
        raise DriveNotConnected(root)
    return root


_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.I)


def is_reserved_name(part: str) -> bool:
    """True for Windows device names (``CON``, ``nul.txt``, ``COM1`` ...), which can't be
    file or folder names there - so they must never be put on a FAT32 drive."""
    return _RESERVED.match(part.split(".", 1)[0].rstrip(" ")) is not None


def same_content(a: Path, b: Path) -> bool:
    """True if two files hold exactly the same bytes."""
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(CHUNK), fb.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True


def is_temp_name(name: str) -> bool:
    """True for the names ReWii uses for files that are still being written
    (``.rewii-tmp-*``, ``*.rewii-part``, ``*.part``, ``*.partial``, ``*.rewii-new``...).
    Such a file is never a game, an app or something to back up."""
    low = name.lower()
    return low.startswith(TEMP_PREFIX) or low.endswith(TEMP_SUFFIXES)


def same_file(a: Path, b: Path) -> bool:
    """True if both paths exist and are the same file (e.g. names that differ only
    in case on FAT32/NTFS/APFS)."""
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def rename_path(src: Path, dst: Path) -> None:
    """Rename/move a file or folder to ``dst``, never replacing something else.

    * ``dst`` exists and is a different file -> :class:`FileExistsError`.
    * A change of upper/lower case only (``game.ISO`` -> ``game.iso``) goes through
      a temporary name: on case-insensitive filesystems a direct rename is refused
      or silently does nothing (Linux vfat).
    * Otherwise a plain move (also across filesystems).
    """
    src, dst = Path(src), Path(dst)
    if src == dst:
        return
    if dst.exists() and not same_file(src, dst):
        raise FileExistsError(errno.EEXIST, "already exists", str(dst))
    if src.name != dst.name and src.name.casefold() == dst.name.casefold() \
            and (src.parent == dst.parent or same_file(src.parent, dst.parent)):
        # not a temp name: if the computer dies between the two renames, the game/folder is
        # still listed (as misplaced) under this name instead of being hidden or cleaned up
        tmp = src.with_name(f"{src.stem}.rewii-mv{src.suffix}")
        if tmp.exists():
            tmp = src.with_name(f"{src.stem}.rewii-mv{os.getpid()}{src.suffix}")
        os.rename(src, tmp)
        try:
            os.rename(tmp, dst)
        except BaseException:
            with contextlib.suppress(OSError):
                os.rename(tmp, src)
            raise
        return
    if dst.exists():                 # the same file under another name (hard link): replace it
        os.replace(src, dst)
        return
    shutil.move(str(src), str(dst))


def _clean_name(path: object) -> str:
    """A file name for a message, without ReWii's temp-file decorations."""
    name = Path(str(path)).name
    changed = True
    while changed:
        changed = False
        low = name.lower()
        if low.startswith(TEMP_PREFIX):
            name, changed = name[len(TEMP_PREFIX):], True
        for sfx in TEMP_SUFFIXES:
            if low.endswith(sfx) and len(name) > len(sfx):
                name, changed = name[:-len(sfx)], True
                break
    return name


def _on_drive(exc: OSError, drive_root: Path | None) -> bool | None:
    """Is the file the error is about on the drive? None if that can't be told."""
    if drive_root is None:
        return None
    names = [n for n in (exc.filename2, exc.filename) if isinstance(n, (str, bytes, os.PathLike))]
    if not names:
        return None
    root = os.path.abspath(os.fspath(drive_root))
    for n in names:
        p = os.path.abspath(os.fsdecode(n))
        if p == root or p.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def explain_os_error(exc: BaseException, drive_root: Path | str | None = None) -> str:
    """A plain sentence for an error that reaches the user.

    Operating-system errors are translated ("The drive is full", "The drive is
    write-protected", "The drive was disconnected"...) and never mention ReWii's
    temporary file names. Other errors are returned as their own message."""
    if not isinstance(exc, OSError) or isinstance(exc, DriveNotConnected):
        return str(exc) or type(exc).__name__
    root = Path(drive_root) if drive_root is not None else None
    code = exc.errno
    name = _clean_name(exc.filename2 or exc.filename) if (exc.filename2 or exc.filename) else ""
    about = f" ({name})" if name else ""
    on_drive = _on_drive(exc, root)
    gone = False
    if root is not None:
        try:
            gone = not root.is_dir()
        except OSError:
            gone = True
    if gone and on_drive is not False and code in (errno.ENOENT, errno.EIO, errno.ENODEV, errno.ENXIO,
                                                   errno.ENOTDIR, errno.ESTALE, errno.ENOTCONN, None):
        return ("The drive was disconnected before this finished. Plug it back in and try again.")
    where = "This computer's disk" if on_drive is False else "The drive"
    if code in (errno.ENOSPC, errno.EDQUOT):
        return f"{where} is full{about}. Free up some space and try again."
    if code == errno.EROFS:
        return (f"{where} is write-protected (read-only), so nothing can be changed on it. Check "
                "the lock switch on SD cards; if the computer mounted it read-only because of "
                "errors, repair or reformat it.")
    if code == errno.EFBIG:
        return f"This file is larger than 4 GB, which FAT32 can't hold{about}."
    if code in (errno.EIO, errno.ENODEV, errno.ENXIO):
        return (f"{where} stopped responding{about}. It may have been disconnected, or it is "
                "failing.")
    if code in (errno.EACCES, errno.EPERM):
        return (f"Permission denied{about}: the file may be in use or protected "
                "(reading a drive directly needs admin rights).")
    if code == errno.ENOENT:
        return f"{name}: no such file or folder" if name else "A file or folder that was needed is missing."
    if exc.strerror:
        return f"{exc.strerror}{about}"
    return str(exc) or type(exc).__name__


def part_path(dest: Path, unique: bool = False) -> Path:
    """The temp name a file is written under before it is renamed to ``dest``.
    ``unique`` adds the process and thread id (for caches several threads may fill)."""
    tag = f".{os.getpid()}.{threading.get_ident()}" if unique else ""
    return dest.with_name(f"{dest.name}{tag}{PART_SUFFIX}")


def remove_quietly(paths: Iterable[Path]) -> None:
    """Delete files, ignoring any that are missing or can't be removed (cleanup paths)."""
    for p in paths:
        with contextlib.suppress(OSError):
            p.unlink()


def hash_file(path: Path, algo: str = "sha1", rep: Reporter | None = None) -> str:
    """Hex digest of a file (``algo``: sha1, sha256, md5...), advancing ``rep`` by bytes read."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            if rep is not None:
                rep.advance(len(b))
    return h.hexdigest()


def sha1_file(path: Path, rep: Reporter | None = None) -> str:
    return hash_file(path, "sha1", rep)


def write_atomic(path: Path, data: bytes | str, *, fsync: bool = False, encoding: str = "utf-8",
                 errors: str = "strict", unique: bool = False) -> Path:
    """Write ``data`` to ``path`` through a temp file and a rename: a crash or a full disk
    never leaves a half-written file under the real name. Creates the parent folder.
    ``fsync`` flushes to the device first (use it on removable media for anything that matters)."""
    path = Path(path)
    raw = data.encode(encoding, errors) if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = part_path(path, unique)
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        remove_quietly([tmp])
        raise
    return path


def copy_into(src: Path, out: Path, rep: Reporter | None = None, *, fsync: bool = False,
              hasher: Any = None, message: str | None = None) -> None:
    """Chunked copy of ``src`` to exactly ``out`` (overwriting it). ``out`` is normally a temp
    name the caller renames afterwards; it is removed again if the copy fails or is cancelled."""
    try:
        with open(src, "rb") as fi, open(out, "wb") as fo:
            while True:
                b = fi.read(CHUNK)
                if not b:
                    break
                fo.write(b)
                if hasher is not None:
                    hasher.update(b)
                if rep is not None:
                    rep.advance(len(b), message)
            if fsync:
                fo.flush()
                os.fsync(fo.fileno())
    except BaseException:
        remove_quietly([out])
        raise


def copy_plain(src: Path | str, dest: Path | str) -> None:
    """Plain (not atomic) copy of one file, then times/modes where the file system allows
    (FAT32 may not). For files inside a folder that is itself still temporary, and as the
    ``copy_function`` of :func:`shutil.copytree`."""
    shutil.copyfile(src, dest)
    with contextlib.suppress(OSError):
        shutil.copystat(src, dest)


def copy_atomic(src: Path, dest: Path, rep: Reporter | None = None, *, want_hash: bool = False,
                fsync: bool = False, keep_times: bool = True, message: str | None = None) -> str:
    """Copy ``src`` to ``dest`` via a temp file. Returns the SHA-1 of the bytes
    read when ``want_hash`` (else ""). ``fsync`` flushes to the device before the
    rename (use it for anything irreplaceable written to removable media)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = part_path(dest)
    h = hashlib.sha1() if want_hash else None
    try:
        copy_into(src, tmp, rep, fsync=fsync, hasher=h, message=message)
        if keep_times:
            with contextlib.suppress(OSError):      # FAT/exFAT mounts may refuse chmod/utime
                shutil.copystat(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        remove_quietly([tmp])
        raise
    return h.hexdigest() if h is not None else ""


def make_sparse(f: object) -> None:
    """Mark an open file as sparse so unwritten ranges take no disk space.

    Linux and macOS files are sparse by default. On Windows (NTFS) a file must be
    flagged first, otherwise extending it really allocates every byte."""
    if sys.platform != "win32":
        return
    try:      # pragma: no cover - Windows only
        import ctypes
        import msvcrt
        from ctypes import wintypes
        handle = msvcrt.get_osfhandle(f.fileno())                     # type: ignore[attr-defined]
        returned = wintypes.DWORD()
        ctypes.windll.kernel32.DeviceIoControl(                        # type: ignore[attr-defined]
            wintypes.HANDLE(handle), 0x000900C4, None, 0, None, 0, ctypes.byref(returned), None)
    except (OSError, AttributeError, ValueError):
        pass      # not NTFS, or no handle: the file is simply not sparse


def safe_join(base: Path, rel: str) -> Path:
    """Join a forward-slash relative path from a manifest/archive onto ``base``.

    Rejects absolute paths, ``..``, empty parts, backslashes and drive letters
    (which would escape on Windows), and double-checks the resolved result.
    """
    parts = PurePosixPath(rel).parts
    if (not parts or rel.startswith("/")
            or any(p in ("..", ".", "") or "\\" in p or ":" in p for p in parts)):
        raise UnsafePath(f"unsafe path: {rel!r}")
    out = base.joinpath(*parts)
    try:
        if not out.resolve().is_relative_to(base.resolve()):
            raise UnsafePath(f"unsafe path: {rel!r}")
    except OSError as e:
        raise UnsafePath(f"unsafe path: {rel!r}") from e
    return out


def is_inside(path: Path, base: Path) -> bool:
    """True if ``path`` is ``base`` or somewhere under it (after resolving links)."""
    try:
        return path.resolve().is_relative_to(base.resolve())
    except OSError:
        return False
