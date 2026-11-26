"""Wii and GameCube save games: parse, discover on a drive, back up, restore.

Formats handled
---------------
* **Wii ``data.bin``** (Wii Menu Data Management, SaveGame Manager GX "compress",
  Dolphin export, and old SaveGame Manager ``saves/<Game>.bin`` files). Layout per
  Dolphin ``Source/Core/Core/HW/WiiSave.cpp`` / ``WiiSaveStructs.h``: a 0xF0C0 header
  encrypted with the public SD key/IV, a plain 0x80 "Bk" header, then per-file
  0x80 headers followed by AES-CBC data (per-file IV), then signature + certs.
* **Extracted Wii saves** written by SaveGame Manager GX "extract":
  ``savegames/<16 hex title id>/`` holding ``banner.bin`` + the NAND data files.
* **GameCube memory card images** (``.raw``) used by Nintendont
  (``saves/<ID4>.raw``, ``saves/ninmem.raw``, ``saves/ninmemj.raw``) and GCMM raw
  backups (``MCBACKUP/*.raw``). Layout per Dolphin ``GCMemcard.h``.
* **GameCube ``.gci``** single-save files (GCMM ``MCBACKUP/*.gci``).

Library layout: ``settings.saves_dir/<ID> - <Title>/<timestamp>/<original name>``
plus a ``rewii-save.json`` sidecar in each version folder.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from collections.abc import Iterable

from Crypto.Cipher import AES

from .config import Settings
from .layout import DriveLayout, safe_name
from .progress import CancelToken, ProgressFn, Reporter

# --------------------------------------------------------------------------- constants
SD_KEY = bytes.fromhex("ab01b9d8e1622b08afbad84dbfc2a55d")
SD_IV = bytes.fromhex("216712e6aa1f689f95c5a22324dc6a98")
MD5_BLANKER = bytes.fromhex("0e65378199be4517ab06ec22451a5793")

HEADER_SIZE = 0xF0C0
BK_HEADER_SIZE = 0x80
BK_LISTED_SIZE = 0x70
BK_MAGIC = 0x426B0001
FILE_HDR_SIZE = 0x80
FILE_HDR_MAGIC = 0x03ADF17E
CERTS_SIZE = 0x3C0
BANNER_MAGIC = b"WIBN"
BANNER_BASE_SIZE = 0x60A0
ICON_SIZE = 0x1200

GC_BLOCK = 0x2000
GC_SYSTEM_BLOCKS = 5
GC_DENTRY_SIZE = 0x40
GC_MAX_DENTRIES = 127
GC_CARD_MBITS = (4, 8, 16, 32, 64, 128)
GC_EPOCH = datetime(2000, 1, 1)

SIDECAR = "rewii-save.json"

# kinds of save files
KIND_DATABIN = "wii_databin"
KIND_EXTRACTED = "wii_extracted"
KIND_MEMCARD = "gc_memcard"
KIND_GCI = "gc_gci"

# apps / locations (where a save lives / should be restored to)
APP_SGM = "sgm_gx"                 # SaveGame Manager GX
APP_SGM_LEGACY = "sgm_legacy"      # old SaveGame Manager flat saves/<Game>.bin
APP_DATA_MGMT = "data_management"  # Wii Menu, SD private/wii/title/<ID4>/data.bin
APP_NINTENDONT = "nintendont"
APP_GCMM = "gcmm"
TARGET_APPS = (APP_SGM, APP_DATA_MGMT, APP_NINTENDONT, APP_GCMM)


class SaveError(Exception):
    """A save file is malformed or an operation on it is impossible."""


# --------------------------------------------------------------------------- helpers
def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


def _align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


def _cstr(b: bytes, encoding: str = "latin-1") -> str:
    return b.split(b"\x00", 1)[0].decode(encoding, "replace").strip()


def _utf16(b: bytes) -> str:
    raw = b.decode("utf-16-be", "replace")
    return raw.split("\x00", 1)[0].strip()


def _printable_id(b: bytes) -> str:
    """Return ASCII id text, or hex if the bytes aren't printable."""
    if all(0x20 < c < 0x7F for c in b):
        return b.decode("ascii")
    return b.hex().upper()


def sha1_file(path: Path, rep: Reporter | None = None) -> str:
    """SHA-1 hex digest of a file, advancing ``rep`` by bytes read."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            if rep is not None:
                rep.advance(len(b))
    return h.hexdigest()


def sha1_tree(root: Path) -> str:
    """Deterministic SHA-1 over a directory tree (relative names + contents)."""
    h = hashlib.sha1()
    for p in sorted(root.rglob("*"), key=lambda x: x.relative_to(root).as_posix()):
        rel = p.relative_to(root).as_posix()
        if p.is_dir():
            h.update(b"D" + rel.encode("utf-8") + b"\0")
        elif p.is_file():
            h.update(b"F" + rel.encode("utf-8") + b"\0")
            h.update(bytes.fromhex(sha1_file(p)))
    return h.hexdigest()


def _sha1_path(p: Path) -> str:
    return sha1_tree(p) if p.is_dir() else sha1_file(p)


def _path_size(p: Path) -> int:
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return p.stat().st_size


def _path_mtime(p: Path) -> float:
    if p.is_dir():
        ts = [f.stat().st_mtime for f in p.rglob("*") if f.is_file()]
        return max(ts) if ts else p.stat().st_mtime
    return p.stat().st_mtime


def _copy_path(src: Path, dest: Path) -> None:
    """Copy a file or directory tree, preserving mtimes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=False)
    else:
        tmp = dest.with_name(dest.name + ".rewii-part")
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def _unique_dir(parent: Path, name: str) -> Path:
    p = parent / name
    i = 2
    while p.exists():
        p = parent / f"{name}-{i}"
        i += 1
    return p


# --------------------------------------------------------------------------- Wii data.bin
@dataclass
class WiiBanner:
    """Decoded ``WIBN`` banner (title, subtitle, icon count)."""
    title: str
    subtitle: str
    flags: int
    icon_count: int
    banner_image: bytes = field(repr=False, default=b"")   # 192x64 RGB5A3
    icons: list[bytes] = field(repr=False, default_factory=list)  # 48x48 RGB5A3 each


@dataclass
class WiiSaveFile:
    """One entry inside a ``data.bin``."""
    name: str
    size: int
    type: int            # 1 = file, 2 = directory
    permissions: int
    attrib: int

    @property
    def is_dir(self) -> bool:
        return self.type == 2


@dataclass
class WiiSaveInfo:
    """Header information of a Wii ``data.bin``."""
    title_id: int
    banner: Optional[WiiBanner]
    banner_size: int
    md5_ok: bool
    ng_id: int
    files: list[WiiSaveFile]
    total_size: int

    @property
    def title_id_hex(self) -> str:
        return f"{self.title_id:016x}"

    @property
    def game_id(self) -> str:
        """4-character game id (e.g. ``RMCE``), from the low title id."""
        return _printable_id(struct.pack(">I", self.title_id & 0xFFFFFFFF))

    @property
    def title(self) -> str:
        return self.banner.title if self.banner else ""


def parse_banner(data: bytes) -> WiiBanner:
    """Parse a ``banner.bin`` (WIBN) blob."""
    if len(data) < 0xA0 or data[:4] != BANNER_MAGIC:
        raise SaveError("not a Wii save banner (WIBN)")
    flags = _u32(data, 4)
    title = _utf16(data[0x20:0x60])
    subtitle = _utf16(data[0x60:0xA0])
    icon_count = max(0, (len(data) - BANNER_BASE_SIZE) // ICON_SIZE)
    image = data[0xA0:BANNER_BASE_SIZE]
    icons = [data[BANNER_BASE_SIZE + i * ICON_SIZE: BANNER_BASE_SIZE + (i + 1) * ICON_SIZE]
             for i in range(icon_count)]
    return WiiBanner(title, subtitle, flags, icon_count, image, icons)


def decrypt_header(enc: bytes) -> bytes:
    """Decrypt the 0xF0C0-byte ``data.bin`` header with the SD key/IV."""
    if len(enc) != HEADER_SIZE:
        raise SaveError("data.bin header must be 0xF0C0 bytes")
    return AES.new(SD_KEY, AES.MODE_CBC, SD_IV).decrypt(enc)


def encrypt_header(plain: bytes) -> bytes:
    """Re-encrypt a decrypted header (used to round-trip-verify copies)."""
    if len(plain) != HEADER_SIZE:
        raise SaveError("data.bin header must be 0xF0C0 bytes")
    return AES.new(SD_KEY, AES.MODE_CBC, SD_IV).encrypt(plain)


def header_md5(plain: bytes) -> bytes:
    """MD5 of a plaintext header with the md5 field replaced by the blanker."""
    blanked = plain[:0x0E] + MD5_BLANKER + plain[0x1E:]
    return hashlib.md5(blanked).digest()


def looks_like_databin(head: bytes) -> bool:
    """Cheap check: ``head`` holds at least 0xF0C8 bytes with the Bk magic at 0xF0C4."""
    return (len(head) >= HEADER_SIZE + 8 and _u32(head, HEADER_SIZE) == BK_LISTED_SIZE
            and _u32(head, HEADER_SIZE + 4) == BK_MAGIC)


def parse_databin(source: Path | bytes) -> WiiSaveInfo:
    """Decrypt and parse a ``data.bin`` header + Bk header + file table.

    ``source`` is a path or the full file contents. File data isn't decrypted
    (see :func:`extract_databin`).
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        read = lambda o, n: data[o:o + n]  # noqa: E731
        fsize = len(data)
        return _parse_databin(read, fsize)
    path = Path(source)
    fsize = path.stat().st_size
    with open(path, "rb") as f:
        def read(o: int, n: int) -> bytes:
            f.seek(o)
            return f.read(n)
        return _parse_databin(read, fsize)


def _parse_databin(read, fsize: int) -> WiiSaveInfo:
    if fsize < HEADER_SIZE + BK_HEADER_SIZE:
        raise SaveError("file too small to be a Wii data.bin")
    bk = read(HEADER_SIZE, BK_HEADER_SIZE)
    if _u32(bk, 0) != BK_LISTED_SIZE or _u32(bk, 4) != BK_MAGIC:
        raise SaveError("not a Wii data.bin (missing Bk header)")
    plain = decrypt_header(read(0, HEADER_SIZE))
    title_id = struct.unpack_from(">Q", plain, 0)[0]
    banner_size = _u32(plain, 8)
    md5_ok = header_md5(plain) == plain[0x0E:0x1E]
    banner: Optional[WiiBanner] = None
    if BANNER_BASE_SIZE <= banner_size <= 0xF0A0:
        try:
            banner = parse_banner(plain[0x20:0x20 + banner_size])
        except SaveError:
            banner = None
    ng_id, nfiles = _u32(bk, 8), _u32(bk, 12)
    total_size = _u32(bk, 0x1C)
    bk_tid = struct.unpack_from(">Q", bk, 0x60)[0]
    if bk_tid and bk_tid != title_id and md5_ok:
        raise SaveError("data.bin title ids disagree")
    files: list[WiiSaveFile] = []
    off = HEADER_SIZE + BK_HEADER_SIZE
    for _ in range(nfiles):
        fh = read(off, FILE_HDR_SIZE)
        if len(fh) < FILE_HDR_SIZE or _u32(fh, 0) != FILE_HDR_MAGIC:
            raise SaveError("corrupt data.bin file table")
        size = _u32(fh, 4)
        ftype = fh[0x0A]
        name = _cstr(fh[0x0B:0x4B], "latin-1")
        files.append(WiiSaveFile(name, size, ftype, fh[8], fh[9]))
        off += FILE_HDR_SIZE + (_align(size, 0x40) if ftype == 1 else 0)
    if not md5_ok and banner is None:
        raise SaveError("data.bin header failed to decrypt (bad MD5 and banner)")
    return WiiSaveInfo(title_id, banner, banner_size, md5_ok, ng_id, files, total_size)


def extract_databin(path: Path, dest: Path) -> list[Path]:
    """Decrypt every file in a ``data.bin`` into ``dest`` (plus ``banner.bin``).

    Returns the written paths. File names are kept as stored; path traversal is
    rejected.
    """
    info = parse_databin(path)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with open(path, "rb") as f:
        plain = decrypt_header(f.read(HEADER_SIZE))
        bp = dest / "banner.bin"
        bp.write_bytes(plain[0x20:0x20 + info.banner_size])
        out.append(bp)
        off = HEADER_SIZE + BK_HEADER_SIZE
        for _ in info.files:
            f.seek(off)
            fh = f.read(FILE_HDR_SIZE)
            size, ftype = _u32(fh, 4), fh[0x0A]
            name = _cstr(fh[0x0B:0x4B], "latin-1")
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents:
                raise SaveError(f"unsafe file name in save: {name!r}")
            off += FILE_HDR_SIZE
            if ftype == 2:
                target.mkdir(parents=True, exist_ok=True)
                continue
            enc = f.read(_align(size, 0x40))
            iv = fh[0x50:0x60]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(AES.new(SD_KEY, AES.MODE_CBC, iv).decrypt(enc)[:size])
            out.append(target)
            off += _align(size, 0x40)
    return out


def verify_databin_roundtrip(path: Path) -> bool:
    """True if decrypting then re-encrypting the header reproduces the file bytes."""
    with open(path, "rb") as f:
        enc = f.read(HEADER_SIZE)
    return len(enc) == HEADER_SIZE and encrypt_header(decrypt_header(enc)) == enc


# --------------------------------------------------------------------------- GameCube
@dataclass
class GcSave:
    """One directory entry on a GameCube memory card (or inside a .gci)."""
    index: int
    game_code: str
    maker_code: str
    filename: str
    modified: Optional[datetime]
    first_block: int
    block_count: int
    comment: str = ""
    comment2: str = ""

    @property
    def game_id(self) -> str:
        return self.game_code + self.maker_code

    @property
    def size(self) -> int:
        return self.block_count * GC_BLOCK


@dataclass
class GcMemcard:
    """Parsed GameCube memory card image."""
    size_mbits: int
    encoding: str        # "cp1252" or "shift_jis"
    saves: list[GcSave]
    active_dir: int      # 0 or 1
    free_blocks: int

    @property
    def total_blocks(self) -> int:
        return self.size_mbits * 16

    @property
    def user_blocks(self) -> int:
        return self.total_blocks - GC_SYSTEM_BLOCKS


def gc_checksum(data: bytes) -> tuple[int, int]:
    """GameCube memory card checksum pair over big-endian u16 words."""
    cs = inv = 0
    for (w,) in struct.iter_unpack(">H", data):
        cs = (cs + w) & 0xFFFF
        inv = (inv + (w ^ 0xFFFF)) & 0xFFFF
    return (0 if cs == 0xFFFF else cs), (0 if inv == 0xFFFF else inv)


def _gc_block_ok(block: bytes, start: int, end: int, cs_off: int) -> bool:
    return gc_checksum(block[start:end]) == (_u16(block, cs_off), _u16(block, cs_off + 2))


def _gc_time(secs: int) -> Optional[datetime]:
    if secs in (0, 0xFFFFFFFF):
        return None
    try:
        from datetime import timedelta
        return GC_EPOCH + timedelta(seconds=secs)
    except OverflowError:
        return None


def _parse_dentry(e: bytes, index: int, encoding: str) -> Optional[GcSave]:
    if e[:4] == b"\xff\xff\xff\xff":
        return None
    return GcSave(
        index=index,
        game_code=_printable_id(e[0:4]),
        maker_code=_printable_id(e[4:6]),
        filename=_cstr(e[8:0x28], encoding),
        modified=_gc_time(_u32(e, 0x28)),
        first_block=_u16(e, 0x36),
        block_count=_u16(e, 0x38),
    )


def looks_like_memcard(path: Path) -> bool:
    """True if ``path`` has a valid GameCube memory card size + header."""
    try:
        size = path.stat().st_size
        if size not in {m * 0x20000 for m in GC_CARD_MBITS}:
            return False
        with open(path, "rb") as f:
            hdr = f.read(0x200)
        return _u16(hdr, 0x22) * 0x20000 == size
    except OSError:
        return False


def looks_like_gci(path: Path) -> bool:
    """True if ``path`` is a .gci (0x40 dentry + block_count * 0x2000 bytes)."""
    try:
        size = path.stat().st_size
        if size < GC_DENTRY_SIZE + GC_BLOCK or (size - GC_DENTRY_SIZE) % GC_BLOCK:
            return False
        with open(path, "rb") as f:
            e = f.read(GC_DENTRY_SIZE)
        return e[:4] != b"\xff\xff\xff\xff" and _u16(e, 0x38) * GC_BLOCK + GC_DENTRY_SIZE == size
    except OSError:
        return False


def _comments(data: bytes, addr: int, encoding: str) -> tuple[str, str]:
    if addr == 0xFFFFFFFF or addr + 0x40 > len(data):
        return "", ""
    return _cstr(data[addr:addr + 0x20], encoding), _cstr(data[addr + 0x20:addr + 0x40], encoding)


def parse_memcard(path: Path) -> GcMemcard:
    """Parse a raw GameCube memory card image (Nintendont/GCMM ``.raw``)."""
    path = Path(path)
    if not looks_like_memcard(path):
        raise SaveError(f"{path.name} is not a GameCube memory card image")
    with open(path, "rb") as f:
        sysblocks = f.read(GC_BLOCK * GC_SYSTEM_BLOCKS)
        hdr = sysblocks[:GC_BLOCK]
        size_mbits = _u16(hdr, 0x22)
        encoding = "shift_jis" if _u16(hdr, 0x24) == 1 else "cp1252"
        dirs = [sysblocks[GC_BLOCK:2 * GC_BLOCK], sysblocks[2 * GC_BLOCK:3 * GC_BLOCK]]
        bats = [sysblocks[3 * GC_BLOCK:4 * GC_BLOCK], sysblocks[4 * GC_BLOCK:5 * GC_BLOCK]]
        dir_ok = [_gc_block_ok(d, 0, 0x1FFC, 0x1FFC) for d in dirs]
        ctr = [struct.unpack_from(">h", d, 0x1FFA)[0] for d in dirs]
        if dir_ok[0] and dir_ok[1]:
            active = 0 if ctr[0] >= ctr[1] else 1
        elif dir_ok[0] or dir_ok[1]:
            active = 0 if dir_ok[0] else 1
        else:
            raise SaveError(f"{path.name}: both memory card directories are corrupt")
        bat_ok = [_gc_block_ok(b, 4, GC_BLOCK, 0) for b in bats]
        bctr = [struct.unpack_from(">h", b, 4)[0] for b in bats]
        bat = bats[0] if (bat_ok[0] and (not bat_ok[1] or bctr[0] >= bctr[1])) else bats[1]
        free = _u16(bat, 6)
        saves: list[GcSave] = []
        d = dirs[active]
        for i in range(GC_MAX_DENTRIES):
            e = d[i * GC_DENTRY_SIZE:(i + 1) * GC_DENTRY_SIZE]
            s = _parse_dentry(e, i, encoding)
            if s is None:
                continue
            addr = _u32(e, 0x3C)
            if addr != 0xFFFFFFFF and s.first_block >= GC_SYSTEM_BLOCKS:
                blk, inner = divmod(addr, GC_BLOCK)
                chain = _bat_chain(bat, s.first_block, s.block_count)
                if blk < len(chain):
                    f.seek(chain[blk] * GC_BLOCK + inner)
                    s.comment, s.comment2 = _comments(f.read(0x40), 0, encoding)
            saves.append(s)
    return GcMemcard(size_mbits, encoding, saves, active, free)


def _bat_chain(bat: bytes, first: int, count: int) -> list[int]:
    chain: list[int] = []
    b = first
    while GC_SYSTEM_BLOCKS <= b < 0xFFFF and len(chain) < count:
        chain.append(b)
        nxt = _u16(bat, 0x0A + (b - GC_SYSTEM_BLOCKS) * 2)
        if nxt in (0, 0xFFFF):
            break
        b = nxt
    return chain


def parse_gci(path: Path) -> GcSave:
    """Parse a .gci file's directory entry (with comment strings)."""
    path = Path(path)
    if not looks_like_gci(path):
        raise SaveError(f"{path.name} is not a .gci save")
    data = path.read_bytes()
    e = data[:GC_DENTRY_SIZE]
    s = _parse_dentry(e, 0, "cp1252")
    assert s is not None
    s.comment, s.comment2 = _comments(data[GC_DENTRY_SIZE:], _u32(e, 0x3C), "cp1252")
    return s


def export_gci(card: Path, index: int, dest_dir: Path) -> Path:
    """Export save ``index`` (dentry index) from a raw memory card as a .gci.

    The file is named like Dolphin/GCMM: ``<maker>-<game>-<filename>.gci``.
    """
    card = Path(card)
    mc = parse_memcard(card)
    s = next((x for x in mc.saves if x.index == index), None)
    if s is None:
        raise SaveError(f"no save #{index} on {card.name}")
    with open(card, "rb") as f:
        f.seek(GC_BLOCK * (1 + mc.active_dir))
        d = f.read(GC_BLOCK)
        bats = []
        for k in (3, 4):
            f.seek(GC_BLOCK * k)
            bats.append(f.read(GC_BLOCK))
        bat_ok = [_gc_block_ok(b, 4, GC_BLOCK, 0) for b in bats]
        bctr = [struct.unpack_from(">h", b, 4)[0] for b in bats]
        bat = bats[0] if (bat_ok[0] and (not bat_ok[1] or bctr[0] >= bctr[1])) else bats[1]
        chain = _bat_chain(bat, s.first_block, s.block_count)
        if len(chain) != s.block_count:
            raise SaveError(f"{s.filename}: block chain is broken")
        body = bytearray()
        for b in chain:
            f.seek(b * GC_BLOCK)
            body += f.read(GC_BLOCK)
    dentry = d[index * GC_DENTRY_SIZE:(index + 1) * GC_DENTRY_SIZE]
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = safe_name(f"{s.maker_code}-{s.game_code}-{s.filename}", maxlen=120) + ".gci"
    out = dest_dir / name
    out.write_bytes(bytes(dentry) + bytes(body))
    return out


# --------------------------------------------------------------------------- discovery
@dataclass
class SaveGame:
    """A game whose save lives in a :class:`DriveSave` (cards can hold many)."""
    game_id: str
    title: str
    size: int = 0
    modified: Optional[datetime] = None


@dataclass
class DriveSave:
    """A save file/folder found on a drive (or anywhere)."""
    path: Path
    kind: str                 # KIND_*
    app: str                  # APP_* where it was found
    game_id: str              # primary id (ID4 for Wii, card name for multi cards)
    title: str
    size: int
    mtime: float
    title_id: str = ""        # 16-hex Wii title id
    games: list[SaveGame] = field(default_factory=list)
    files: list[str] = field(default_factory=list)   # names inside (Wii files / GC filenames)
    note: str = ""

    @property
    def modified(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)


_HEX16 = re.compile(r"^[0-9a-fA-F]{16}$")


def classify_save(path: Path, app: str = "") -> Optional[DriveSave]:
    """Identify any save file/folder by content. Returns None if it isn't one."""
    path = Path(path)
    try:
        if path.is_dir():
            return _classify_extracted(path, app or APP_SGM)
        if not path.is_file():
            return None
        size = path.stat().st_size
        mtime = path.stat().st_mtime
        with open(path, "rb") as f:
            head = f.read(HEADER_SIZE + 8)
        if looks_like_databin(head):
            info = parse_databin(path)
            title = info.title or path.stem
            return DriveSave(path, KIND_DATABIN, app, info.game_id, title, size, mtime,
                             title_id=info.title_id_hex,
                             games=[SaveGame(info.game_id, title, size,
                                             datetime.fromtimestamp(mtime))],
                             files=[f.name for f in info.files],
                             note=info.banner.subtitle if info.banner else "")
        if looks_like_memcard(path):
            mc = parse_memcard(path)
            games: dict[str, SaveGame] = {}
            for s in mc.saves:
                g = games.setdefault(s.game_code, SaveGame(s.game_code, s.comment or s.filename))
                g.size += s.size
                if s.modified and (g.modified is None or s.modified > g.modified):
                    g.modified = s.modified
            stem = path.stem
            gid = stem.upper() if re.fullmatch(r"[A-Za-z0-9]{4}", stem) else stem
            if gid in games:
                title = games[gid].title
            elif len(games) == 1:
                title = next(iter(games.values())).title
            else:
                title = f"Memory card ({stem})"
            return DriveSave(path, KIND_MEMCARD, app, gid, title, size, mtime,
                             games=list(games.values()),
                             files=[f"{s.game_code}: {s.filename}" for s in mc.saves],
                             note=f"{mc.user_blocks - mc.free_blocks}/{mc.user_blocks} blocks used")
        if looks_like_gci(path):
            s = parse_gci(path)
            title = s.comment or s.filename
            return DriveSave(path, KIND_GCI, app, s.game_code, title, size, mtime,
                             games=[SaveGame(s.game_code, title, s.size, s.modified)],
                             files=[s.filename])
    except (OSError, SaveError, struct.error):
        return None
    return None


def _classify_extracted(path: Path, app: str) -> Optional[DriveSave]:
    """SaveGame Manager GX extracted folder: ``<16 hex tid>/banner.bin`` + files."""
    banner_p = path / "banner.bin"
    if not (_HEX16.match(path.name) or banner_p.is_file()):
        return None
    if not banner_p.is_file():
        return None
    try:
        banner = parse_banner(banner_p.read_bytes())
    except (OSError, SaveError):
        return None
    tid = path.name.lower() if _HEX16.match(path.name) else ""
    gid = _printable_id(bytes.fromhex(tid[8:])) if tid else path.name
    files = sorted(p.relative_to(path).as_posix() for p in path.rglob("*")
                   if p.is_file() and p != banner_p)
    size, mtime = _path_size(path), _path_mtime(path)
    return DriveSave(path, KIND_EXTRACTED, app, gid, banner.title or gid, size, mtime,
                     title_id=tid,
                     games=[SaveGame(gid, banner.title or gid, size, datetime.fromtimestamp(mtime))],
                     files=files, note=banner.subtitle)


def discover_saves(root: Path, progress: ProgressFn | None = None,
                   cancel: CancelToken | None = None) -> list[DriveSave]:
    """Find every save on a drive (USB or SD root).

    Looks in ``savegames/`` (SaveGame Manager GX), ``saves/`` (Nintendont cards
    *and* old SaveGame Manager ``.bin`` files - told apart by content),
    ``private/wii/title/*/data.bin`` (Data Management) and ``MCBACKUP/`` (GCMM).
    """
    lay = DriveLayout(Path(root))
    rep = Reporter(progress, cancel, message="Looking for saves")
    found: list[DriveSave] = []
    seen: set[Path] = set()

    def add(p: Path, app: str) -> None:
        rp = p.resolve()
        if rp in seen:
            return
        rep.check()
        s = classify_save(p, app)
        if s is not None:
            seen.add(rp)
            found.append(s)

    def children(d: Path) -> list[Path]:
        try:
            return sorted(d.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            return []

    # SaveGame Manager GX: savegames/<tid16>/ (extract) and any data.bin inside
    for p in children(lay.savegames):
        if p.is_dir():
            if (p / "banner.bin").is_file():
                add(p, APP_SGM)
            else:
                for q in children(p):
                    if q.is_file() and q.suffix.lower() == ".bin":
                        add(q, APP_SGM)
        elif p.suffix.lower() in (".bin",):
            add(p, APP_SGM)
    # saves/: Nintendont .raw cards, GCI, and legacy SaveGame Manager <Game>.bin
    for p in children(lay.nintendont_saves):
        if not p.is_file():
            continue
        s = classify_save(p, "")
        if s is None:
            continue
        s.app = APP_SGM_LEGACY if s.kind == KIND_DATABIN else APP_NINTENDONT
        if p.resolve() not in seen:
            seen.add(p.resolve())
            found.append(s)
    # Data Management (SD; SGM GX also uses it on USB when no SD is inserted)
    for p in children(lay.private_saves):
        db = p / "data.bin"
        if db.is_file():
            add(db, APP_DATA_MGMT)
    # GCMM
    for p in children(lay.gcmm_backups):
        if p.is_file() and p.suffix.lower() in (".gci", ".raw", ".gcp", ".mci"):
            add(p, APP_GCMM)
    rep.note(f"Found {len(found)} saves")
    return found


def catalog_by_game(saves: Iterable[DriveSave]) -> dict[str, list[tuple[DriveSave, SaveGame]]]:
    """Group saves by game id (a memory card contributes one row per game on it)."""
    out: dict[str, list[tuple[DriveSave, SaveGame]]] = {}
    for s in saves:
        for g in (s.games or [SaveGame(s.game_id, s.title, s.size, s.modified)]):
            out.setdefault(g.game_id, []).append((s, g))
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- library
@dataclass
class SaveVersion:
    """One backed-up version of a save in the library."""
    dir: Path                 # version folder
    path: Path                # the saved file or folder inside it
    game_id: str
    title: str
    kind: str
    app: str
    sha1: str
    source: str
    created: str
    size: int
    title_id: str = ""

    @property
    def game_folder(self) -> Path:
        return self.dir.parent


def _game_folder_name(game_id: str, title: str) -> str:
    t = safe_name(title) if title and title != game_id else ""
    return safe_name(f"{game_id} - {t}" if t else game_id, maxlen=96)


def _read_version(vdir: Path) -> Optional[SaveVersion]:
    try:
        meta = json.loads((vdir / SIDECAR).read_text("utf-8"))
        return SaveVersion(dir=vdir, path=vdir / meta["name"], game_id=meta["game_id"],
                           title=meta.get("title", ""), kind=meta["kind"], app=meta.get("app", ""),
                           sha1=meta["sha1"], source=meta.get("source", ""),
                           created=meta.get("created", ""), size=int(meta.get("size", 0)),
                           title_id=meta.get("title_id", ""))
    except (OSError, ValueError, KeyError):
        return None


def list_backups(settings: Settings, game_id: str | None = None) -> list[SaveVersion]:
    """All library save versions (optionally for one game), oldest first."""
    root = settings.saves_dir
    out: list[SaveVersion] = []
    if not root.is_dir():
        return out
    for gdir in sorted(root.iterdir()):
        if not gdir.is_dir():
            continue
        for vdir in sorted(gdir.iterdir()):
            v = _read_version(vdir) if vdir.is_dir() else None
            if v and (game_id is None or v.game_id == game_id):
                out.append(v)
    out.sort(key=lambda v: (v.game_id, v.created, v.dir.name))
    return out


@dataclass
class BackupResult:
    """Result of :func:`backup_save`."""
    version: SaveVersion
    created: bool             # False when an identical version already existed


def backup_save(save: DriveSave, settings: Settings, *, now: datetime | None = None,
                progress: ProgressFn | None = None,
                cancel: CancelToken | None = None) -> BackupResult:
    """Copy a drive save into the library as a new timestamped version.

    Identical content (same SHA-1) already in the library for that game is not
    copied again; the existing version is returned with ``created=False``.
    """
    rep = Reporter(progress, cancel, message=f"Backing up {save.title}")
    rep.check()
    digest = _sha1_path(save.path)
    for v in list_backups(settings, save.game_id):
        if v.sha1 == digest:
            return BackupResult(v, False)
    gdir = settings.saves_dir / _game_folder_name(save.game_id, save.title)
    gdir.mkdir(parents=True, exist_ok=True)
    vdir = _unique_dir(gdir, _timestamp(now))
    vdir.mkdir()
    target = vdir / save.path.name
    try:
        _copy_path(save.path, target)
        if _sha1_path(target) != digest:
            raise SaveError(f"copy of {save.path.name} did not verify")
        meta = {
            "name": save.path.name, "game_id": save.game_id, "title": save.title,
            "kind": save.kind, "app": save.app, "sha1": digest, "source": str(save.path),
            "size": save.size, "mtime": save.mtime, "title_id": save.title_id,
            "created": (now or datetime.now()).isoformat(timespec="seconds"),
            "games": [g.game_id for g in save.games],
        }
        (vdir / SIDECAR).write_text(json.dumps(meta, indent=2), "utf-8")
    except BaseException:
        shutil.rmtree(vdir, ignore_errors=True)
        raise
    rep.note("Backed up")
    got = _read_version(vdir)
    assert got is not None
    return BackupResult(got, True)


def backup_all(saves: Iterable[DriveSave], settings: Settings, *,
               progress: ProgressFn | None = None,
               cancel: CancelToken | None = None) -> list[BackupResult]:
    """Back up many saves; cancellable between saves."""
    saves = list(saves)
    rep = Reporter(progress, cancel, total=len(saves), message="Backing up saves")
    out = []
    for s in saves:
        rep.check()
        out.append(backup_save(s, settings))
        rep.advance(1, s.title)
    return out


def restore_destination(version: SaveVersion, drive_root: Path, target_app: str) -> Path:
    """Where ``version`` goes on the drive for ``target_app`` (one of TARGET_APPS).

    * Wii data.bin -> ``private/wii/title/<ID4>/data.bin`` (Data Management on SD;
      SaveGame Manager GX reads the same path on SD or USB).
    * Wii extracted folder -> ``savegames/<tid16>/`` (SaveGame Manager GX only).
    * GC memory card -> ``saves/<name>.raw`` (Nintendont) or ``MCBACKUP/<name>`` (GCMM).
    * GC .gci -> ``MCBACKUP/<name>.gci`` (GCMM only).
    """
    lay = DriveLayout(Path(drive_root))
    if target_app not in TARGET_APPS:
        raise SaveError(f"unknown target app {target_app!r}")
    k = version.kind
    if k == KIND_DATABIN:
        if target_app not in (APP_SGM, APP_DATA_MGMT):
            raise SaveError("Wii saves can only be restored for SaveGame Manager GX or Data Management")
        gid = version.game_id
        if len(gid) != 4:
            raise SaveError(f"can't place a Wii save with game id {gid!r}")
        return lay.private_saves / gid / "data.bin"
    if k == KIND_EXTRACTED:
        if target_app != APP_SGM:
            raise SaveError("extracted Wii saves can only be restored for SaveGame Manager GX")
        tid = version.title_id or version.path.name
        return lay.savegames / tid
    if k == KIND_MEMCARD:
        if target_app == APP_NINTENDONT:
            stem = version.path.stem
            if not (re.fullmatch(r"[A-Za-z0-9]{4}", stem) or stem.lower().startswith("ninmem")):
                stem = version.game_id if len(version.game_id) == 4 else "ninmem"
            return lay.nintendont_saves / f"{stem}.raw"
        if target_app == APP_GCMM:
            return lay.gcmm_backups / version.path.name
        raise SaveError("GameCube memory cards can only be restored for Nintendont or GCMM")
    if k == KIND_GCI:
        if target_app != APP_GCMM:
            raise SaveError(".gci saves can only be restored for GCMM (import them with GCMM)")
        return lay.gcmm_backups / version.path.name
    raise SaveError(f"unknown save kind {k!r}")


@dataclass
class RestoreResult:
    """Result of :func:`restore_save`."""
    dest: Path
    copied: bool                          # False if the identical save was already there
    previous_backup: Optional[SaveVersion] = None   # backup of the replaced drive save


def restore_save(version: SaveVersion, drive_root: Path, target_app: str, settings: Settings, *,
                 progress: ProgressFn | None = None,
                 cancel: CancelToken | None = None) -> RestoreResult:
    """Restore a library version to the drive for ``target_app``.

    A different save already at the destination is backed up into the library
    first (and the restore aborts if that backup fails).
    """
    rep = Reporter(progress, cancel, message=f"Restoring {version.title}")
    dest = restore_destination(version, drive_root, target_app)
    rep.check()
    prev: Optional[SaveVersion] = None
    if dest.exists():
        if _sha1_path(dest) == version.sha1:
            return RestoreResult(dest, False)
        existing = classify_save(dest, target_app)
        if existing is None:
            # not recognisable as a save: still keep a copy under the version's game id
            existing = DriveSave(dest, version.kind, target_app, version.game_id,
                                 version.title, _path_size(dest), _path_mtime(dest),
                                 title_id=version.title_id)
        prev = backup_save(existing, settings).version
        rep.check()
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    _copy_path(version.path, dest)
    if _sha1_path(dest) != version.sha1:
        raise SaveError(f"restored copy at {dest} did not verify")
    rep.note("Restored")
    return RestoreResult(dest, True, prev)


def save_to_dict(s: DriveSave) -> dict:
    """JSON-friendly representation of a :class:`DriveSave`."""
    d = asdict(s)
    d["path"] = str(s.path)
    for g in d["games"]:
        if g["modified"] is not None:
            g["modified"] = g["modified"].isoformat()
    return d
