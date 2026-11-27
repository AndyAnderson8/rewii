"""Convert compressed / PC-only disc images into plain ISO.

RVZ, WIA and GCZ are converted with ``nodtool`` (github.com/encounter/nod),
which ReWii downloads on first use into ``cache_dir()/tools`` and verifies by
SHA-256. A ``nodtool`` or ``DolphinTool`` the user installed on PATH is used as
a fallback. CISO and WBFS files are expanded in pure Python. WDF needs
Wiimms ISO Tools (``wit``) on PATH because nod doesn't read WDF.

NKit ``.iso`` images (from the NKit tool) are refused: they have data removed
that neither nod nor Dolphin can put back, so the result would not be a real
disc image. NKit data inside RVZ/WIA/WBFS/CISO written by nod is restored by nod.

Wii results can then be written as WBFS with :func:`rewii.core.wbfs.write_wbfs`;
GameCube results stay ISO.
"""
from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import net
from .config import cache_dir
from .disc import CisoReader, DiscError, WbfsFileReader, sniff_format
from .progress import Cancelled, CancelToken, ProgressFn, Reporter

#: Pinned nodtool release and the SHA-256 digests GitHub reports for its assets
#: (api.github.com/repos/encounter/nod/releases, ``assets[].digest``).
NOD_VERSION = "v2.0.0-alpha.12"
NOD_RELEASE_URL = "https://github.com/encounter/nod/releases/download/{tag}/{asset}"
NOD_API_LATEST = "https://api.github.com/repos/encounter/nod/releases/latest"
NOD_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("windows", "x86_64"): ("nodtool-windows-x86_64.exe",
                            "f65de62c44f0ad5d179eff85bd1e92099713ffacbf717f651fb761129a25c7bc"),
    ("windows", "arm64"): ("nodtool-windows-arm64.exe",
                           "463d5f7fd7d753fde05c6772f8114ec6f8ab6eaf054790ed9b95a89ef8c63526"),
    ("windows", "x86"): ("nodtool-windows-x86.exe",
                         "293ad6cb4eed336c65e154878106a8edcc920905cb667e5d04a7baa4c3d65621"),
    ("macos", "arm64"): ("nodtool-macos-arm64",
                         "524c0b27c0c4289e435df4474d288ea7e63d6a34814cc7c650231688dd1e53b4"),
    ("macos", "x86_64"): ("nodtool-macos-x86_64",
                          "d1e7ba904c2e557fafbd3bf2d9b75a7901a2bf1568c0fb3a505fca0f30b0a833"),
    ("linux", "x86_64"): ("nodtool-linux-x86_64",
                          "ee8345c004248eb82a86dd0c6068e0fcf3dc75baf68d18fe949bf4589822f5a5"),
    ("linux", "arm64"): ("nodtool-linux-aarch64",
                         "33e801c0e4aee7e714ad84e89521f50e2f79005571b9d4779b695e44ea21ff99"),
    ("linux", "x86"): ("nodtool-linux-i686",
                       "efaa034f4d32a14856d5150c7eee45876ec38c07f75572cb6b4c33423c5850ce"),
}

#: Formats that need an external tool.
TOOL_FORMATS = {"RVZ", "WIA", "GCZ", "WDF"}
#: Formats this module converts to ISO (input side).
CONVERTIBLE = {"RVZ", "WIA", "GCZ", "WDF", "CISO", "WBFS", "ISO"}

POLL = 0.25


class ConvertError(Exception):
    """Conversion failed; the message is plain language."""


@dataclass(frozen=True)
class Tool:
    kind: str   # "nodtool" | "dolphintool" | "wit"
    path: Path


# --------------------------------------------------------------------------- platform / tool
def host_platform() -> tuple[str, str]:
    """("windows"|"macos"|"linux", "x86_64"|"arm64"|"x86")."""
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    m = _platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        arch = "x86_64"
    elif m in ("arm64", "aarch64", "armv8l"):
        arch = "arm64"
    elif m in ("i386", "i686", "x86"):
        arch = "x86"
    else:
        arch = m
    return osname, arch


def tools_dir() -> Path:
    p = cache_dir() / "tools"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cached_tool_path() -> Path:
    """Where ReWii keeps its own nodtool."""
    exe = ".exe" if host_platform()[0] == "windows" else ""
    return tools_dir() / f"nodtool-{NOD_VERSION}{exe}"


def _which(*names: str) -> Optional[Path]:
    for n in names:
        p = shutil.which(n)
        if p:
            return Path(p)
    return None


def find_tool(fmt: str = "RVZ") -> Optional[Tool]:
    """Best available converter for ``fmt`` without downloading anything.

    Order: ReWii's cached nodtool, nodtool on PATH, DolphinTool on PATH;
    for WDF only ``wit`` on PATH.
    """
    fmt = fmt.upper()
    if fmt == "WDF":
        p = _which("wit")
        return Tool("wit", p) if p else None
    c = cached_tool_path()
    if c.is_file():
        return Tool("nodtool", c)
    p = _which("nodtool")
    if p:
        return Tool("nodtool", p)
    p = _which("DolphinTool", "dolphin-tool", "dolphintool")
    if p:
        return Tool("dolphintool", p)
    return None


def tool_available() -> bool:
    """True if nodtool is cached or on PATH (never downloads)."""
    return cached_tool_path().is_file() or _which("nodtool") is not None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _asset_for_host(latest: bool) -> tuple[str, str, str]:
    """(download url, sha256, tag) for this machine."""
    key = host_platform()
    if not latest:
        if key not in NOD_ASSETS:
            raise ConvertError(f"There is no converter download for this computer ({key[0]} {key[1]}). "
                               "Install nodtool or DolphinTool yourself and try again.")
        asset, digest = NOD_ASSETS[key]
        return NOD_RELEASE_URL.format(tag=NOD_VERSION, asset=asset), digest, NOD_VERSION
    try:
        rel = net.get_json(NOD_API_LATEST)
    except Exception as e:
        raise ConvertError(f"Could not check for the latest converter: {e}") from e
    if key not in NOD_ASSETS:
        raise ConvertError(f"There is no converter download for this computer ({key[0]} {key[1]}).")
    want = NOD_ASSETS[key][0]
    for a in rel.get("assets", []):
        if a.get("name") == want:
            dig = a.get("digest") or ""
            if not dig.startswith("sha256:"):
                raise ConvertError("The converter download has no checksum to verify against.")
            return a["browser_download_url"], dig.split(":", 1)[1], rel.get("tag_name", "")
    raise ConvertError("The latest converter release has no build for this computer.")


def ensure_tool(progress: ProgressFn | None = None, cancel: CancelToken | None = None, *,
                latest: bool = False) -> Path:
    """Return a usable nodtool, downloading the pinned build if needed.

    The download is SHA-256 verified and made executable. ``latest=True``
    fetches the newest release instead, verified with GitHub's asset digest.
    """
    if not latest:
        t = find_tool("RVZ")
        if t is not None and t.kind == "nodtool":
            return t.path
    url, digest, _tag = _asset_for_host(latest)
    dest = cached_tool_path()
    tmp_dir = Path(tempfile.mkdtemp(prefix="nodtool-", dir=tools_dir()))
    tmp = tmp_dir / dest.name
    try:
        try:
            net.download(url, tmp, progress=progress, cancel=cancel)
        except net.NetError as e:
            raise ConvertError(f"Could not download the disc converter (nodtool): {e}") from e
        if _sha256(tmp) != digest.lower():
            raise ConvertError("The downloaded converter failed its checksum and was deleted.")
        if os.name != "nt":
            tmp.chmod(0o755)
        os.replace(tmp, dest)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return dest


# --------------------------------------------------------------------------- sizes
def expected_iso_size(src: Path, fmt: str | None = None) -> int:
    """Size of the ISO a conversion will produce, from the source header (0 if unknown)."""
    fmt = (fmt or sniff_format(src)).upper()
    try:
        with open(src, "rb") as f:
            head = f.read(0x40)
        if fmt in ("RVZ", "WIA") and len(head) >= 0x2C:
            return struct.unpack(">Q", head[0x24:0x2C])[0]      # WIAFileHeader.iso_file_size
        if fmt == "GCZ" and len(head) >= 0x18:
            return struct.unpack("<Q", head[0x10:0x18])[0]      # GCZ header data_size (LE)
        if fmt == "CISO":
            with CisoReader(src) as r:
                return r.size
        if fmt == "WBFS":
            with WbfsFileReader(src) as r:
                return r.size
        if fmt == "ISO":
            return src.stat().st_size
    except (OSError, struct.error, DiscError):
        return 0
    return 0


# --------------------------------------------------------------------------- running
def _command(tool: Tool, src: Path, out: Path) -> list[str]:
    if tool.kind == "nodtool":
        return [str(tool.path), "--no-color", "convert", str(src), str(out)]
    if tool.kind == "dolphintool":
        return [str(tool.path), "convert", "-f", "iso", "-i", str(src), "-o", str(out)]
    if tool.kind == "wit":
        return [str(tool.path), "copy", "--iso", "--overwrite", str(src), str(out)]
    raise ConvertError(f"Unknown tool {tool.kind}")


def _run(cmd: list[str], out: Path, expected: int, rep: Reporter) -> None:
    """Run a converter, reporting progress from the output file's size."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    env = dict(os.environ, NO_COLOR="1")
    with tempfile.TemporaryFile() as log:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=flags, env=env)
        except OSError as e:
            raise ConvertError(f"Could not start the converter: {e}") from e
        try:
            while True:
                rc = proc.poll()
                if rep.cancel is not None and rep.cancel.cancelled:
                    raise Cancelled()
                try:
                    size = out.stat().st_size
                except OSError:
                    size = 0
                if rc is not None:
                    break
                rep.update(min(size, expected) if expected else size)
                time.sleep(POLL)
        except BaseException:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        if rc != 0:
            log.seek(0)
            tail = log.read()[-1500:].decode("utf-8", "replace").strip().splitlines()[-5:]
            raise ConvertError("The converter failed: " + (" / ".join(tail) or f"exit code {rc}"))


def _copy(src: Path, out: Path, rep: Reporter) -> None:
    with open(src, "rb") as fi, open(out, "wb") as fo:
        for b in iter(lambda: fi.read(4 << 20), b""):
            fo.write(b)
            rep.advance(len(b))


def _is_nkit_iso(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0x200)
            return f.read(4) == b"NKIT"
    except OSError:
        return False


NKIT_MESSAGE = ("This is an NKit image. NKit removes parts of the disc that a real console needs, and "
                "ReWii can't put them back. Restore it to a full ISO with the NKit tool (\"Recover to "
                "ISO\"), or use a full ISO, WBFS or RVZ copy instead.")


def to_iso(src: Path | str, dest: Path | str, progress: ProgressFn | None = None,
           cancel: CancelToken | None = None) -> Path:
    """Convert an RVZ/WIA/GCZ/WDF/CISO/WBFS image to a plain ``.iso`` at ``dest``.

    Downloads nodtool if needed (RVZ/WIA/GCZ). Partial output is removed on
    failure or cancellation. Returns ``dest``. Raises :class:`ConvertError`
    (plain-language) or :class:`rewii.core.progress.Cancelled`.
    """
    src, dest = Path(src), Path(dest)
    if not src.is_file():
        raise ConvertError(f"{src.name} was not found")
    try:
        fmt = sniff_format(src)
    except OSError as e:
        raise ConvertError(f"Could not read {src.name}: {e}") from e
    if fmt == "NKIT":
        raise ConvertError(NKIT_MESSAGE)
    if fmt not in CONVERTIBLE:
        raise ConvertError(f"{src.name} is not a Wii or GameCube disc image ReWii can convert")
    rep = Reporter(progress, cancel)
    expected = expected_iso_size(src, fmt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.with_name(dest.stem + ".part.iso")
    work.unlink(missing_ok=True)
    try:
        if fmt == "ISO":
            rep.set_total(expected, f"Copying {src.name}")
            _copy(src, work, rep)
        elif fmt in ("CISO", "WBFS"):
            from .wbfs import write_iso
            reader = CisoReader(src) if fmt == "CISO" else WbfsFileReader(src)
            with reader as r:
                write_iso(r, work, progress=progress, cancel=cancel)
        else:
            tool = find_tool(fmt)
            if tool is None and fmt == "WDF":
                raise ConvertError("WDF images need Wiimms ISO Tools (wit). Install wit, or convert the "
                                   "file to ISO or WBFS with Wii Backup Manager first.")
            if tool is None:
                rep.note("Downloading the disc converter")
                tool = Tool("nodtool", ensure_tool(progress, cancel))
            rep.set_total(expected, f"Converting {src.name}")
            _run(_command(tool, src, work), work, expected, rep)
        if not work.is_file():
            raise ConvertError("The converter did not produce a file")
        if _is_nkit_iso(work):
            raise ConvertError(NKIT_MESSAGE)
        if sniff_format(work) != "ISO":
            raise ConvertError("The converted file is not a valid disc image")
        if expected:
            rep.update(expected, "Done")
        os.replace(work, dest)
    except BaseException:
        work.unlink(missing_ok=True)
        raise
    return dest


__all__ = [
    "ConvertError", "Tool", "NOD_VERSION", "NOD_ASSETS", "find_tool", "tool_available", "ensure_tool",
    "to_iso", "expected_iso_size", "host_platform", "cached_tool_path",
]
