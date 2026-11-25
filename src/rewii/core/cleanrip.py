"""CleanRip disc dumps: find them, join the parts, verify the checksums.

CleanRip (github.com/emukidid/cleanrip, source/main.c) names its output after
the 6-character game ID, plus ``-disc2`` for a second disc:

* ``<name>.iso`` or, when split, ``<name>.part0.iso``, ``<name>.part1.iso`` ...
* ``<name>-dumpinfo.txt`` with ``MD5:``, ``SHA-1:`` and ``CRC32:`` lines
  (CRC32 only when checksums were disabled)
* ``<name>.bca`` (64 bytes of burst-cutting-area data)

If the dump matched CleanRip's redump DAT, ``<name>`` is the redump title
(e.g. ``Wii Sports (USA) (Rev 1)``) instead of the ID. CleanRip keeps those DATs
on the device root as ``wii.dat`` / ``gc.dat`` (redump XML), which ReWii can
also use to identify a dump.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .disc import GC_MAGIC, WII_MAGIC
from .progress import CancelToken, ProgressFn, Reporter

CHUNK = 4 * 1024 * 1024
_PART = re.compile(r"^(?P<base>.+)\.part(?P<n>\d+)\.iso$", re.IGNORECASE)


class CleanRipError(Exception):
    """A dump set is incomplete or unreadable."""


@dataclass
class DumpInfo:
    """Parsed ``<name>-dumpinfo.txt``."""

    filename: str = ""
    internal_name: str = ""
    md5: Optional[str] = None
    sha1: Optional[str] = None
    crc32: Optional[str] = None      # 8 lowercase hex digits
    version: str = ""
    verified: Optional[bool] = None  # CleanRip's own redump check
    raw: dict[str, str] = field(default_factory=dict)


def parse_dumpinfo(path: Path | str) -> DumpInfo:
    """Read a CleanRip dumpinfo file (``Key: value`` lines, CRLF)."""
    text = Path(path).read_text("utf-8", errors="replace")
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("--"):
            k, v = line.split(":", 1)
            raw[k.strip()] = v.strip()

    def hexval(key: str, n: int) -> Optional[str]:
        v = raw.get(key, "").lower()
        return v if re.fullmatch(rf"[0-9a-f]{{{n}}}", v) else None

    ver = raw.get("Verified", "").lower()
    return DumpInfo(
        filename=raw.get("Filename", ""),
        internal_name=raw.get("Internal Name", ""),
        md5=hexval("MD5", 32),
        sha1=hexval("SHA-1", 40),
        crc32=hexval("CRC32", 8),
        version=raw.get("Version", ""),
        verified=True if ver == "yes" else False if ver == "no" else None,
        raw=raw,
    )


@dataclass
class CleanRipDump:
    """One dumped disc found on a drive or folder."""

    name: str                       # base name (game ID, "GAMEID-disc2" or redump title)
    folder: Path
    parts: list[Path]               # in order; one entry for an unsplit .iso
    dumpinfo: Optional[Path] = None
    bca: Optional[Path] = None
    missing_parts: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for p in self.parts)

    @property
    def is_split(self) -> bool:
        return len(self.parts) > 1 or (bool(self.parts) and ".part" in self.parts[0].name.lower())

    @property
    def complete(self) -> bool:
        return not self.missing_parts and bool(self.parts)

    def info(self) -> Optional[DumpInfo]:
        return parse_dumpinfo(self.dumpinfo) if self.dumpinfo else None


def _ci_lookup(folder: Path) -> dict[str, Path]:
    return {p.name.lower(): p for p in folder.iterdir() if p.is_file()}


def find_dumps(folder: Path | str, *, recursive: bool = False) -> list[CleanRipDump]:
    """Find CleanRip dump sets in ``folder``.

    Split sets are found by ``*.part0.iso``; single ``<name>.iso`` files count
    only when a matching ``<name>-dumpinfo.txt`` exists (so ordinary ISOs
    aren't mistaken for dumps).
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    dirs = [folder] + ([d for d in folder.rglob("*") if d.is_dir()] if recursive else [])
    out: list[CleanRipDump] = []
    for d in dirs:
        files = _ci_lookup(d)
        bases: dict[str, dict[int, Path]] = {}
        for low, p in files.items():
            m = _PART.match(p.name)
            if m:
                bases.setdefault(m.group("base"), {})[int(m.group("n"))] = p
        for base, parts in sorted(bases.items()):
            if 0 not in parts:
                continue
            top = max(parts)
            missing = [i for i in range(top + 1) if i not in parts]
            out.append(CleanRipDump(
                name=base, folder=d, parts=[parts[i] for i in sorted(parts)],
                dumpinfo=files.get(f"{base}-dumpinfo.txt".lower()),
                bca=files.get(f"{base}.bca".lower()), missing_parts=missing))
        for low, p in sorted(files.items()):
            if low.endswith(".iso") and not _PART.match(p.name):
                base = p.name[:-4]
                info = files.get(f"{base}-dumpinfo.txt".lower())
                if info is not None and base not in bases:
                    out.append(CleanRipDump(name=base, folder=d, parts=[p], dumpinfo=info,
                                            bca=files.get(f"{base}.bca".lower())))
    return out


# --------------------------------------------------------------------------- redump DAT
@dataclass
class DatMatch:
    game: str
    rom: str
    size: int


def load_dat(path: Path | str) -> dict[str, DatMatch]:
    """Parse a redump/logiqx XML DAT into {md5: DatMatch} (also keyed by sha1 and crc)."""
    out: dict[str, DatMatch] = {}
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError) as e:
        raise CleanRipError(f"Could not read DAT file {Path(path).name}: {e}") from e
    for game in root.iter("game"):
        gname = game.get("name", "")
        for rom in game.iter("rom"):
            m = DatMatch(gname, rom.get("name", ""), int(rom.get("size", "0") or 0))
            for attr in ("md5", "sha1", "crc"):
                v = (rom.get(attr) or "").lower()
                if v:
                    out[f"{attr}:{v}"] = m
    return out


def _dat_for(dump: CleanRipDump, platform: Optional[str]) -> Optional[Path]:
    """CleanRip keeps wii.dat / gc.dat on the device root next to the dumps."""
    names = {"wii": ["wii.dat"], "gc": ["gc.dat"]}.get(platform or "", ["wii.dat", "gc.dat"])
    for d in (dump.folder, dump.folder.parent):
        files = _ci_lookup(d) if d.is_dir() else {}
        for n in names:
            if n in files:
                return files[n]
    return None


# --------------------------------------------------------------------------- verify / join
@dataclass
class VerifyResult:
    """Checksums of a joined / checked dump and how they compare."""

    path: Optional[Path]
    size: int
    md5: str
    sha1: str
    crc32: str
    platform: Optional[str] = None         # "wii" | "gc" from the disc header
    game_id: str = ""
    dumpinfo_match: Optional[bool] = None  # None = no checksums in dumpinfo to compare
    mismatches: list[str] = field(default_factory=list)
    redump_name: Optional[str] = None      # matched game in the DAT, if a DAT was used
    dat_checked: bool = False

    @property
    def ok(self) -> bool:
        """True unless a known checksum disagrees."""
        return not self.mismatches


def _compare(res: VerifyResult, info: Optional[DumpInfo]) -> None:
    if info is None:
        return
    pairs = [("MD5", info.md5, res.md5), ("SHA-1", info.sha1, res.sha1), ("CRC32", info.crc32, res.crc32)]
    known = [(n, want, got) for n, want, got in pairs if want]
    if not known:
        return
    for n, want, got in known:
        if want != got:
            res.mismatches.append(f"{n} is {got}, but CleanRip recorded {want}")
    res.dumpinfo_match = not res.mismatches


def _check_dat(res: VerifyResult, dat: Optional[Path]) -> None:
    if dat is None:
        return
    table = load_dat(dat)
    res.dat_checked = True
    m = table.get(f"md5:{res.md5}") or table.get(f"sha1:{res.sha1}") or table.get(f"crc:{res.crc32}")
    if m:
        res.redump_name = m.game


def _header_info(first: bytes) -> tuple[Optional[str], str]:
    if len(first) < 0x20:
        return None, ""
    wii, gc = struct.unpack(">II", first[0x18:0x20])
    plat = "wii" if wii == WII_MAGIC else "gc" if gc == GC_MAGIC else None
    gid = first[:6].decode("ascii", "replace") if plat else ""
    return plat, gid


def _stream(parts: list[Path], dest: Optional[Path], rep: Reporter) -> VerifyResult:
    md5, sha1, crc = hashlib.md5(), hashlib.sha1(), 0
    size = 0
    first = b""
    fo = open(dest, "wb") if dest is not None else None
    try:
        for p in parts:
            with open(p, "rb") as fi:
                for b in iter(lambda: fi.read(CHUNK), b""):
                    if not first:
                        first = b[:0x20]
                    md5.update(b)
                    sha1.update(b)
                    crc = zlib.crc32(b, crc)
                    if fo is not None:
                        fo.write(b)
                    size += len(b)
                    rep.advance(len(b))
        if fo is not None:
            fo.flush()
            os.fsync(fo.fileno())
    finally:
        if fo is not None:
            fo.close()
    plat, gid = _header_info(first)
    return VerifyResult(dest, size, md5.hexdigest(), sha1.hexdigest(), f"{crc & 0xFFFFFFFF:08x}",
                        platform=plat, game_id=gid)


def join_dump(dump: CleanRipDump, dest: Path | str, progress: ProgressFn | None = None,
              cancel: CancelToken | None = None, *, dat: Path | str | None = None,
              use_dat: bool = True) -> VerifyResult:
    """Join a dump's parts into one ISO at ``dest`` and verify it.

    Checksums are compared with ``-dumpinfo.txt`` when present, and the dump is
    looked up in ``dat`` (or ``wii.dat``/``gc.dat`` next to the dump when
    ``use_dat``). The joined file is kept even if a checksum differs; check
    :attr:`VerifyResult.ok`. Partial output is deleted on error/cancel.
    """
    if not dump.complete:
        raise CleanRipError(f"{dump.name}: part(s) {', '.join(map(str, dump.missing_parts))} are missing")
    dest = Path(dest)
    if dest.resolve() in {p.resolve() for p in dump.parts}:
        raise CleanRipError("The joined file can't overwrite one of the parts")
    dest.parent.mkdir(parents=True, exist_ok=True)
    rep = Reporter(progress, cancel)
    rep.set_total(dump.size, f"Joining {dump.name}")
    tmp = dest.with_name(dest.name + ".part")
    try:
        res = _stream(dump.parts, tmp, rep)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    res.path = dest
    _compare(res, dump.info())
    _check_dat(res, Path(dat) if dat else (_dat_for(dump, res.platform) if use_dat else None))
    return res


def verify_dump(dump: CleanRipDump, progress: ProgressFn | None = None,
                cancel: CancelToken | None = None, *, dat: Path | str | None = None,
                use_dat: bool = True) -> VerifyResult:
    """Checksum a dump (split or not) in place without writing anything."""
    if not dump.complete:
        raise CleanRipError(f"{dump.name}: part(s) {', '.join(map(str, dump.missing_parts))} are missing")
    rep = Reporter(progress, cancel)
    rep.set_total(dump.size, f"Checking {dump.name}")
    res = _stream(dump.parts, None, rep)
    res.path = dump.parts[0] if len(dump.parts) == 1 else None
    _compare(res, dump.info())
    _check_dat(res, Path(dat) if dat else (_dat_for(dump, res.platform) if use_dat else None))
    return res


def delete_parts(dump: CleanRipDump) -> None:
    """Remove a dump's ``.partN.iso`` files (call only after a successful, verified join)."""
    for p in dump.parts:
        p.unlink(missing_ok=True)
