"""Small, shared file helpers: hashing, atomic copies and safe path joins.

Everything that copies user data onto a drive or into the library goes through
:func:`copy_atomic` (temp file + rename, so a failed or cancelled copy never
leaves a half-written file under the real name), and every relative path read
from a manifest or archive goes through :func:`safe_join`.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath

from .progress import Reporter

CHUNK = 4 * 1024 * 1024
PART_SUFFIX = ".rewii-part"


class UnsafePath(ValueError):
    """A relative path that would escape its base folder."""


def sha1_file(path: Path, rep: Reporter | None = None) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            if rep is not None:
                rep.advance(len(b))
    return h.hexdigest()


def copy_atomic(src: Path, dest: Path, rep: Reporter | None = None, *, want_hash: bool = False,
                fsync: bool = False, keep_times: bool = True) -> str:
    """Copy ``src`` to ``dest`` via a temp file. Returns the SHA-1 of the bytes
    read when ``want_hash`` (else ""). ``fsync`` flushes to the device before the
    rename (use it for anything irreplaceable written to removable media)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + PART_SUFFIX)
    h = hashlib.sha1() if want_hash else None
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(CHUNK)
                if not b:
                    break
                fo.write(b)
                if h is not None:
                    h.update(b)
                if rep is not None:
                    rep.advance(len(b))
            if fsync:
                fo.flush()
                os.fsync(fo.fileno())
        if keep_times:
            with contextlib.suppress(OSError):      # FAT/exFAT mounts may refuse chmod/utime
                shutil.copystat(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return h.hexdigest() if h is not None else ""


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
