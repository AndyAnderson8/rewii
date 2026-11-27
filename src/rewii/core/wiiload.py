"""Send a .dol/.elf/.zip to the Homebrew Channel over the network (wiiload).

Protocol v0.5, as implemented by devkitPro's ``wiiload`` (source/main.c)::

    "HAXX"
    u8  version major (0), u8 version minor (5), u16 BE args length
    u32 BE payload length (compressed size if compressed)
    u32 BE uncompressed length (0 when the payload is not compressed)
    payload (zlib stream when compressed)
    args: NUL-separated strings, first one the file name, then a final NUL

TCP port 4299. Zip files are sent uncompressed; the Homebrew Channel installs
them to its storage device, so they must be laid out as ``<app>/boot.dol``
(``apps/`` prefixes are stripped automatically, see :func:`hbc_zip`).
"""
from __future__ import annotations

import io
import socket
import struct
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Optional
from collections.abc import Sequence

from .progress import CancelToken, ProgressFn, Reporter
from .errors import ReWiiError

PORT = 4299
VERSION = (0, 5)
MAX_ARGS_LEN = 1024
CHUNK = 4 * 1024
ZIP_MAGIC = b"PK\x03\x04"


class WiiloadError(ReWiiError):
    """Could not send to the console."""


def build_args(filename: str, args: Sequence[str] = ()) -> bytes:
    """``filename\\0arg1\\0...\\0`` + terminating NUL (max 1024 bytes)."""
    blob = b"".join(s.encode("utf-8") + b"\0" for s in (filename, *args)) + b"\0"
    if len(blob) > MAX_ARGS_LEN:
        raise WiiloadError("argument string too long")
    return blob


def build_header(payload_len: int, uncompressed_len: int, args_len: int) -> bytes:
    """The 16-byte wiiload header."""
    return b"HAXX" + struct.pack(">BBHII", VERSION[0], VERSION[1], args_len,
                                 payload_len, uncompressed_len)


def hbc_zip(data: bytes) -> bytes:
    """Rewrite an OSC-style zip (``apps/<app>/...``) to the ``<app>/...`` layout the
    Homebrew Channel expects; other zips are returned unchanged."""
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise WiiloadError("not a valid zip file") from e
    with zin:
        names = [i.filename.replace("\\", "/") for i in zin.infolist()]
        boots = [n for n in names if PurePosixPath(n).name.lower() in ("boot.dol", "boot.elf")]
        if not boots:
            raise WiiloadError("zip has no boot.dol or boot.elf")
        app_dir = str(PurePosixPath(boots[0]).parent)
        if app_dir.count("/") == 0:
            return data          # already "<app>/boot.dol" (or bare boot.dol)
        prefix = str(PurePosixPath(app_dir).parent) + "/"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info, name in zip(zin.infolist(), names):
                if not name.startswith(prefix + PurePosixPath(app_dir).name + "/"):
                    continue
                new = name[len(prefix):]
                if info.is_dir():
                    continue
                zout.writestr(new, zin.read(info))
        return out.getvalue()


def prepare(path: Path | str, compress: bool = True, fix_zip: bool = True
            ) -> tuple[bytes, int]:
    """Read a file and return ``(payload, uncompressed_len)`` ready to send."""
    data = Path(path).read_bytes()
    if not data:
        raise WiiloadError(f"{Path(path).name} is empty")
    if data[:4] == ZIP_MAGIC:
        return (hbc_zip(data) if fix_zip else data), 0
    if compress:
        z = zlib.compress(data, 6)
        if len(z) < len(data):
            return z, len(data)
    return data, 0


def send(host: str, path: Path | str, args: Sequence[str] = (),
         progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None, *,
         port: int = PORT, timeout: float = 10.0, compress: bool = True) -> None:
    """Send ``path`` to the Homebrew Channel at ``host`` (the Wii's IP address).

    The Homebrew Channel must be open on the console. Raises :class:`WiiloadError`
    on connection problems, :class:`~rewii.core.progress.Cancelled` if cancelled.
    """
    path = Path(path)
    payload, raw_len = prepare(path, compress)
    arg_blob = build_args(path.name, args)
    header = build_header(len(payload), raw_len, len(arg_blob))
    rep = Reporter(progress, cancel, message=f"Sending {path.name}")
    rep.set_total(len(payload))
    rep.check()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(header)
            view = memoryview(payload)
            for off in range(0, len(payload), CHUNK):
                chunk = view[off:off + CHUNK]
                s.sendall(chunk)
                rep.advance(len(chunk))
            s.sendall(arg_blob)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    except TimeoutError as e:
        raise WiiloadError(f"Timed out talking to {host}:{port}. Is the Homebrew Channel open?") from e
    except OSError as e:
        raise WiiloadError(f"Could not send to {host}:{port}: {e}") from e
    rep.note(f"Sent {path.name}")
