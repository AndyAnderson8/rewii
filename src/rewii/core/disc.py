"""Reading Wii and GameCube disc images in every format a user might bring.

Supported natively: ISO/GCM (plain), WBFS files (incl. split .wbf1..), CISO.
Detected but needing conversion (see ``convert``): RVZ, WIA, GCZ, NKit, WDF.

Also implements Wii partition decryption, FST parsing and the "usage table"
used for scrubbing (copying only the sectors a game actually uses).
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from Crypto.Cipher import AES

WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D
WII_SECTOR = 0x8000              # raw disc sector / encrypted cluster size
WII_SECTOR_DATA = 0x7C00         # payload bytes per encrypted cluster
WII_SL_SECTORS = 143432          # single-layer Wii disc
WII_DL_SECTORS = 143432 * 2      # dual-layer Wii disc (max)
WII_SL_SIZE = WII_SL_SECTORS * WII_SECTOR   # 4,699,979,776
WII_DL_SIZE = 8_511_160_320
GC_DISC_SIZE = 1_459_978_240
FAT32_SPLIT = 4 * 1024 ** 3 - 32 * 1024     # 0xFFFF8000, standard split size

#: Formats that are recognised but must be converted to ISO first (see ``convert``),
#: and the file extensions they usually have. The one place these are defined.
NEEDS_CONVERSION = frozenset({"RVZ", "WIA", "GCZ", "NKIT", "WDF"})
NEEDS_CONVERSION_EXTS = frozenset({".rvz", ".wia", ".gcz", ".wdf"})   # NKit files are ".nkit.iso"
VALID_DISC_SIZES = (GC_DISC_SIZE, WII_SL_SIZE, WII_DL_SIZE)

MAX_TMD_SIZE = 1024 * 1024
MAX_FST_SIZE = 64 * 1024 * 1024
MAX_FST_ENTRIES = 0x80000
MAX_DISC_FILE_READ = 64 * 1024 * 1024
_BAD_DATA = (struct.error, ValueError, OverflowError, ZeroDivisionError, IndexError, MemoryError)
_GAME_ID = re.compile(rb"[A-Za-z0-9]{6}")    # a few unlicensed discs use lowercase

REGION_BY_CHAR = {
    "E": "NTSC-U", "N": "NTSC-U",
    "J": "NTSC-J", "K": "NTSC-K", "W": "NTSC-K", "T": "NTSC-K", "Q": "NTSC-K",
    "P": "PAL", "D": "PAL", "F": "PAL", "I": "PAL", "S": "PAL", "H": "PAL", "U": "PAL",
    "X": "PAL", "Y": "PAL", "Z": "PAL", "L": "PAL", "M": "PAL", "R": "PAL", "V": "PAL",
    "A": "Region free",
}


class DiscError(Exception):
    pass


class NeedsConversion(DiscError):
    """The image is in a format consoles can't load (RVZ/WIA/GCZ/NKit/WDF)."""

    def __init__(self, fmt: str, path: Path):
        super().__init__(f"{path.name} is {fmt}; it must be converted before use")
        self.fmt = fmt
        self.path = path


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(data)


def common_key(index: int = 0) -> bytes:
    from libWiiPy.title.commonkeys import get_common_key
    return get_common_key(index)


# --------------------------------------------------------------------------- readers
class DiscReader:
    """Random-access view of a full (logical, uncompressed) disc image."""

    format = "unknown"

    def read(self, offset: int, size: int) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def size(self) -> int:  # logical disc size
        raise NotImplementedError

    def sector_present(self, sector: int) -> bool:
        """False if the container knows this 0x8000 sector is absent (all zero)."""
        return True

    def missing(self, offset: int, size: int) -> bool:
        """True if part of this range should be stored in the file(s) but lies past
        their end (a truncated image or a lost split part). ``read`` returns
        zeros for such data, so callers that care must ask."""
        return False

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _MultiFile:
    """Treat split files (.wbfs + .wbf1 ... / .partN) as one contiguous stream."""

    def __init__(self, paths: list[Path]):
        self.files: list[BinaryIO] = []
        try:
            for p in paths:
                self.files.append(open(p, "rb"))
            self.sizes = [os.fstat(f.fileno()).st_size for f in self.files]
        except BaseException:
            self.close()
            raise
        self.starts = []
        s = 0
        for sz in self.sizes:
            self.starts.append(s)
            s += sz
        self.total = s

    def read(self, off: int, size: int) -> bytes:
        out = bytearray()
        while size > 0 and off < self.total:
            i = max(j for j, st in enumerate(self.starts) if st <= off)
            within = off - self.starts[i]
            n = min(size, self.sizes[i] - within)
            f = self.files[i]
            f.seek(within)
            b = f.read(n)
            out += b + bytes(n - len(b))
            off += n
            size -= n
        if size > 0:
            out += bytes(size)
        return bytes(out)

    def close(self) -> None:
        for f in self.files:
            try:
                f.close()
            except OSError:
                pass


def split_parts(path: Path) -> list[Path]:
    """Return [path, path.wbf1, path.wbf2, ...] that exist (WBFS split naming)."""
    parts = [path]
    if path.suffix.lower() == ".wbfs":
        i = 1
        while True:
            p = path.with_suffix(f".wbf{i}")
            if not p.exists():
                break
            parts.append(p)
            i += 1
    return parts


class IsoReader(DiscReader):
    format = "ISO"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        self._size = os.fstat(self.f.fileno()).st_size

    def read(self, offset: int, size: int) -> bytes:
        self.f.seek(offset)
        b = self.f.read(size)
        return b + bytes(size - len(b))

    def missing(self, offset: int, size: int) -> bool:
        return size > 0 and offset + size > self._size

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self.f.close()


class WbfsFileReader(DiscReader):
    """A single-disc WBFS *file* (as produced by WBM / wit / TWBM / ReWii)."""

    format = "WBFS"

    def __init__(self, path: Path, slot: int = 0, _stream=None, _part_off: int = 0):
        self.path = Path(path)
        self._owns_stream = _stream is None      # a partition's stream is shared by all its games
        self.m = _stream or _MultiFile(split_parts(self.path))
        self.part_off = _part_off
        try:
            self._load(slot)
        except _BAD_DATA as e:
            self.close()
            raise DiscError(f"{self.path.name}: damaged WBFS header") from e
        except BaseException:
            self.close()
            raise

    def _load(self, slot: int) -> None:
        hdr = self.m.read(self.part_off, 12)
        magic, n_hd_sec, hd_s, wbfs_s = struct.unpack(">4sIBB", hdr[:10])
        if magic != b"WBFS":
            raise DiscError(f"{self.path.name}: not a WBFS file")
        # 512 B..64 KiB device sectors, 32 KiB..2 GiB WBFS blocks; anything else is a
        # damaged header (and would make the block table absurdly large)
        if not (9 <= hd_s <= 16 and 15 <= wbfs_s <= 31 and slot >= 0):
            raise DiscError(f"{self.path.name}: damaged WBFS header")
        self.hd_sz = 1 << hd_s
        self.sec_sz = 1 << wbfs_s
        self.n_hd_sec = n_hd_sec
        self.n_wbfs_sec_disc = (WII_DL_SECTORS * WII_SECTOR) // self.sec_sz
        disc_info_sz = _align(0x100 + self.n_wbfs_sec_disc * 2, self.hd_sz)
        info = self.m.read(self.part_off + self.hd_sz + slot * disc_info_sz, 0x100 + self.n_wbfs_sec_disc * 2)
        self.header = info[:0x100]
        self.wlba = struct.unpack(f">{self.n_wbfs_sec_disc}H", info[0x100:])
        used = [i for i, v in enumerate(self.wlba) if v]
        self._size = WII_DL_SIZE if used and (used[-1] + 1) * self.sec_sz > WII_SL_SIZE else WII_SL_SIZE
        self._phys = getattr(self.m, "total", None)      # None = unknown (e.g. a raw device)

    def read(self, offset: int, size: int) -> bytes:
        out = bytearray()
        while size > 0:
            idx, within = divmod(offset, self.sec_sz)
            n = min(size, self.sec_sz - within)
            lba = self.wlba[idx] if idx < len(self.wlba) else 0
            if lba == 0:
                out += bytes(n)
            else:
                out += self.m.read(self.part_off + lba * self.sec_sz + within, n)
            offset += n
            size -= n
        return bytes(out)

    def sector_present(self, sector: int) -> bool:
        idx = sector * WII_SECTOR // self.sec_sz
        return idx < len(self.wlba) and self.wlba[idx] != 0

    def missing(self, offset: int, size: int) -> bool:
        if self._phys is None or size <= 0:
            return False
        first, last = offset // self.sec_sz, (offset + size - 1) // self.sec_sz
        for idx in range(first, min(last, len(self.wlba) - 1) + 1):
            lba = self.wlba[idx]
            if lba == 0:
                continue
            lo = max(offset, idx * self.sec_sz) - idx * self.sec_sz
            hi = min(offset + size, (idx + 1) * self.sec_sz) - idx * self.sec_sz
            if lo < hi and self.part_off + lba * self.sec_sz + hi > self._phys:
                return True
        return False

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        if self._owns_stream:
            self.m.close()


class CisoReader(DiscReader):
    """CISO (compact ISO) used by USB loaders and Nintendont."""

    format = "CISO"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        try:
            hdr = self.f.read(0x8000)
            if hdr[:4] != b"CISO" or len(hdr) < 0x8000:
                raise DiscError(f"{self.path.name}: not a CISO file")
            self.block = struct.unpack("<I", hdr[4:8])[0]
            if self.block < WII_SECTOR or self.block > 1 << 30 or self.block & (self.block - 1):
                raise DiscError(f"{self.path.name}: damaged CISO header")
            self._fsize = os.fstat(self.f.fileno()).st_size
        except BaseException:
            self.f.close()
            raise
        self.map: list[int] = []
        n = 0
        for b in hdr[8:]:
            self.map.append(n if b else -1)
            if b:
                n += 1
        last = max((i for i, v in enumerate(self.map) if v >= 0), default=0)
        logical = (last + 1) * self.block
        self._size = WII_DL_SIZE if logical > WII_SL_SIZE else (
            WII_SL_SIZE if logical > GC_DISC_SIZE else GC_DISC_SIZE)
        self.base = 0x8000

    def read(self, offset: int, size: int) -> bytes:
        out = bytearray()
        while size > 0:
            idx, within = divmod(offset, self.block)
            n = min(size, self.block - within)
            phys = self.map[idx] if idx < len(self.map) else -1
            if phys < 0:
                out += bytes(n)
            else:
                self.f.seek(self.base + phys * self.block + within)
                b = self.f.read(n)
                out += b + bytes(n - len(b))
            offset += n
            size -= n
        return bytes(out)

    def sector_present(self, sector: int) -> bool:
        idx = sector * WII_SECTOR // self.block
        return idx < len(self.map) and self.map[idx] >= 0

    def missing(self, offset: int, size: int) -> bool:
        # the last block of a CISO may legitimately be short, so only data that
        # *starts* past the end of the file counts as missing
        if size <= 0:
            return False
        for idx in range(offset // self.block, min((offset + size - 1) // self.block, len(self.map) - 1) + 1):
            phys = self.map[idx]
            if phys < 0:
                continue
            lo = max(offset, idx * self.block) - idx * self.block
            if self.base + phys * self.block + lo >= self._fsize:
                return True
        return False

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self.f.close()


def sniff_format(path: Path) -> str:
    """Identify an image by magic bytes. Returns ISO/WBFS/CISO/RVZ/WIA/GCZ/WDF/NKIT/UNKNOWN."""
    with open(path, "rb") as f:
        head = f.read(0x400)
    if head[:4] == b"WBFS":
        return "WBFS"
    if head[:4] == b"CISO":
        return "CISO"
    if head[:4] == b"RVZ\x01":
        return "RVZ"
    if head[:4] == b"WIA\x01":
        return "WIA"
    if head[:4] == b"\x01\xc0\x0b\xb1":
        return "GCZ"
    if head[:8] == b"WII\x01DISC":
        return "WDF"
    if len(head) >= 0x204 and head[0x200:0x204] == b"NKIT":
        return "NKIT"
    if len(head) >= 0x20:
        wii, gc = struct.unpack(">II", head[0x18:0x20])
        if wii == WII_MAGIC or gc == GC_MAGIC:
            return "ISO"
    return "UNKNOWN"


def open_disc(path: Path | str) -> DiscReader:
    """Open an ISO/WBFS/CISO image. Raises :class:`DiscError` for anything that
    is not a readable disc image (:class:`NeedsConversion` for RVZ and friends)."""
    path = Path(path)
    fmt = sniff_format(path)
    if fmt == "ISO":
        return IsoReader(path)
    if fmt == "WBFS":
        return WbfsFileReader(path)
    if fmt == "CISO":
        return CisoReader(path)
    if fmt in NEEDS_CONVERSION:
        raise NeedsConversion(fmt, path)
    raise DiscError(f"{path.name}: not a recognised Wii or GameCube image")


# --------------------------------------------------------------------------- info
@dataclass
class DiscInfo:
    game_id: str
    title: str
    platform: str            # "wii" | "gc"
    disc_number: int
    version: int
    region: str
    fmt: str
    path: Optional[Path] = None
    file_size: int = 0

    @property
    def is_wii(self) -> bool:
        return self.platform == "wii"


def parse_header(hdr: bytes) -> tuple[str, str, str, int, int]:
    """(game_id, title, platform, disc_number, version) from the first 0x60 bytes.

    The game ID ends up in folder and file names, so it is validated here once:
    anything but six characters of A-Z/0-9 is a :class:`DiscError`."""
    if len(hdr) < 0x60:
        raise DiscError("disc header is too short")
    wii, gc = struct.unpack(">II", hdr[0x18:0x20])
    if wii == WII_MAGIC:
        platform = "wii"
    elif gc == GC_MAGIC:
        platform = "gc"
    else:
        raise DiscError("disc header has no Wii/GameCube magic")
    if not _GAME_ID.fullmatch(hdr[:6]):
        raise DiscError("disc header has an invalid game ID")
    game_id = hdr[:6].decode("ascii")
    title = hdr[0x20:0x60].split(b"\0")[0].decode("utf-8", "replace").strip()
    return game_id, title, platform, hdr[6], hdr[7]


def region_of(game_id: str) -> str:
    return REGION_BY_CHAR.get(game_id[3:4].upper(), "Unknown") if len(game_id) >= 4 else "Unknown"


def disc_info(path: Path | str) -> DiscInfo:
    path = Path(path)
    with open_disc(path) as d:
        gid, title, platform, num, ver = parse_header(d.read(0, 0x60))
        size = sum(p.stat().st_size for p in split_parts(path))
        return DiscInfo(gid, title, platform, num, ver, region_of(gid), d.format, path, size)


# --------------------------------------------------------------------------- Wii partitions
@dataclass
class WiiPartition:
    index: int
    type: int            # 0 = game, 1 = update, 2 = channel
    offset: int          # absolute disc offset
    data_offset: int     # absolute offset of the encrypted data area
    data_size: int
    header_size: int     # bytes from offset to data_offset (ticket/tmd/cert/h3)
    title_key: bytes = b""
    ticket: bytes = b""
    tmd: bytes = b""


def wii_partitions(d: DiscReader) -> list[WiiPartition]:
    try:
        return _wii_partitions(d)
    except _BAD_DATA as e:
        raise DiscError("damaged partition table") from e


def _wii_partitions(d: DiscReader) -> list[WiiPartition]:
    parts: list[WiiPartition] = []
    tables = d.read(0x40000, 0x20)
    for t in range(4):
        count, tbl = struct.unpack(">II", tables[t * 8:t * 8 + 8])
        tbl <<= 2
        if count == 0 or count > 64:
            continue
        ents = d.read(tbl, count * 8)
        for i in range(count):
            poff, ptype = struct.unpack(">II", ents[i * 8:i * 8 + 8])
            poff <<= 2
            hdr = d.read(poff, 0x2C0)
            tmd_size, tmd_off = struct.unpack(">II", hdr[0x2A4:0x2AC])
            data_off, data_size = struct.unpack(">II", hdr[0x2B8:0x2C0])
            if tmd_size > MAX_TMD_SIZE:
                raise DiscError("damaged partition header")
            p = WiiPartition(len(parts), ptype, poff, poff + (data_off << 2), data_size << 2,
                             data_off << 2, ticket=hdr[:0x2A4])
            p.tmd = d.read(poff + (tmd_off << 2), tmd_size)
            try:
                ck = common_key(hdr[0x1F1] if hdr[0x1F1] in (0, 1, 2) else 0)
                p.title_key = aes_cbc_decrypt(ck, hdr[0x1DC:0x1E4] + bytes(8), hdr[0x1BF:0x1CF])
            except Exception:
                p.title_key = b""
            parts.append(p)
    return parts


class PartitionReader:
    """Decrypted view of a Wii partition's data area."""

    def __init__(self, d: DiscReader, part: WiiPartition):
        self.d = d
        self.p = part
        self._cache_idx = -1
        self._cache = b""

    def cluster(self, n: int) -> bytes:
        if n != self._cache_idx:
            raw = self.d.read(self.p.data_offset + n * WII_SECTOR, WII_SECTOR)
            self._cache = aes_cbc_decrypt(self.p.title_key, raw[0x3D0:0x3E0], raw[0x400:])
            self._cache_idx = n
        return self._cache

    def read(self, off: int, size: int) -> bytes:
        out = bytearray()
        while size > 0:
            n, within = divmod(off, WII_SECTOR_DATA)
            take = min(size, WII_SECTOR_DATA - within)
            out += self.cluster(n)[within:within + take]
            off += take
            size -= take
        return bytes(out)


@dataclass
class FstEntry:
    path: str
    offset: int      # offset within the partition (or disc, for GC)
    size: int


def parse_fst(read, shift: int) -> tuple[list[FstEntry], dict]:
    """Parse the FST of a partition (Wii, shift=2) or GameCube disc (shift=0).

    ``read(offset, size)`` reads the decrypted partition / GC disc.
    Returns (files, layout) where layout holds boot/apploader/dol/fst extents.
    Raises :class:`DiscError` if the table is damaged.
    """
    try:
        return _parse_fst(read, shift)
    except _BAD_DATA as e:
        raise DiscError("damaged file table") from e


def _parse_fst(read, shift: int) -> tuple[list[FstEntry], dict]:
    boot = read(0, 0x440)
    dol_off = struct.unpack(">I", boot[0x420:0x424])[0] << shift
    fst_off = struct.unpack(">I", boot[0x424:0x428])[0] << shift
    fst_sz = struct.unpack(">I", boot[0x428:0x42C])[0] << shift
    app_hdr = read(0x2440, 0x20)
    app_sz = 0x20 + struct.unpack(">I", app_hdr[0x14:0x18])[0] + struct.unpack(">I", app_hdr[0x18:0x1C])[0]
    dol_hdr = read(dol_off, 0x100)
    dol_sz = 0x100
    for i in range(18):
        o = struct.unpack(">I", dol_hdr[i * 4:i * 4 + 4])[0]
        s = struct.unpack(">I", dol_hdr[0x90 + i * 4:0x94 + i * 4])[0]
        if o and s:
            dol_sz = max(dol_sz, o + s)
    layout = {"dol": (dol_off, dol_sz), "fst": (fst_off, fst_sz), "apploader": (0x2440, app_sz)}
    files: list[FstEntry] = []
    if fst_sz == 0:
        return files, layout
    if fst_sz > MAX_FST_SIZE:
        raise DiscError("damaged file table")
    fst = read(fst_off, fst_sz)
    count = struct.unpack(">I", fst[8:12])[0]
    if count > MAX_FST_ENTRIES or count * 12 > len(fst):
        raise DiscError("damaged file table")
    strings = fst[count * 12:]

    def name_at(o: int) -> str:
        e = strings.find(b"\0", o)
        return strings[o:e if e >= 0 else None].decode("shift_jis", "replace")

    # iterative walk keeping directory end indices
    stack: list[tuple[int, str]] = [(count, "")]
    i = 1
    while i < count:
        while stack and i >= stack[-1][0]:
            stack.pop()
        word, a, b = struct.unpack(">III", fst[i * 12:i * 12 + 12])
        is_dir, noff = word >> 24, word & 0xFFFFFF
        name = name_at(noff)
        prefix = stack[-1][1] if stack else ""
        if is_dir:
            stack.append((b, f"{prefix}{name}/"))
        else:
            files.append(FstEntry(prefix + name, a << shift, b))
        i += 1
    return files, layout


def wii_game_partition(d: DiscReader) -> PartitionReader:
    for p in wii_partitions(d):
        if p.type == 0:
            if not p.title_key:
                raise DiscError("could not decrypt game partition")
            return PartitionReader(d, p)
    raise DiscError("no game partition on disc")


def read_wii_file(d: DiscReader, name: str, max_size: int = MAX_DISC_FILE_READ) -> bytes:
    """Read a file from the game partition by path (case-insensitive).

    The size comes from the disc, so files over ``max_size`` are refused
    instead of being decrypted into memory."""
    pr = wii_game_partition(d)
    files, _ = parse_fst(pr.read, 2)
    for f in files:
        if f.path.lower() == name.lower().lstrip("/"):
            if f.size > max_size:
                raise DiscError(f"{name} is implausibly large ({f.size} bytes)")
            return pr.read(f.offset, f.size)
    raise DiscError(f"{name} not found on disc")


# --------------------------------------------------------------------------- usage / scrubbing
def wii_usage_table(d: DiscReader, include_update: bool = False) -> list[bool]:
    """Which 0x8000 disc sectors hold real data (like libwbfs' usage table).

    Partitions the container has dropped altogether (an update partition removed
    by an earlier scrub) are skipped. A partition whose file table can't be read
    or doesn't fit inside it is marked as used in full."""
    used = [False] * WII_DL_SECTORS

    def mark(off: int, size: int) -> None:
        if size <= 0:
            return
        a = max(0, off // WII_SECTOR)
        b = min(WII_DL_SECTORS, (off + size + WII_SECTOR - 1) // WII_SECTOR)
        if b > a:
            used[a:b] = [True] * (b - a)

    mark(0, 0x50000)                       # header, partition table, region info
    for p in wii_partitions(d):
        if p.type == 1 and not include_update:
            continue
        if not d.sector_present(p.offset // WII_SECTOR):
            continue                       # stripped from this image
        mark(p.offset, p.header_size)
        if not p.title_key:
            mark(p.data_offset, p.data_size)
            continue
        pr = PartitionReader(d, p)
        n_clusters = (p.data_size + WII_SECTOR - 1) // WII_SECTOR
        extents: list[tuple[int, int]] = []

        def add(o: int, s: int, n_clusters: int = n_clusters,
                extents: list[tuple[int, int]] = extents) -> None:
            if s <= 0:
                return
            c0 = o // WII_SECTOR_DATA
            c1 = (o + s + WII_SECTOR_DATA - 1) // WII_SECTOR_DATA
            if c1 > n_clusters:
                raise DiscError("file table points outside the partition")
            extents.append((c0, c1))

        try:
            files, layout = parse_fst(pr.read, 2)
            add(0, 0x2440)
            for o, s in layout.values():
                add(o, s)
            for f in files:
                add(f.offset, f.size)
        except DiscError:
            mark(p.data_offset, p.data_size)
            continue
        for c0, c1 in extents:
            mark(p.data_offset + c0 * WII_SECTOR, (c1 - c0) * WII_SECTOR)
    return used


def used_sectors(d: DiscReader, scrub: bool = True, include_update: bool = False,
                 table: list[bool] | None = None) -> list[bool]:
    """Sectors to copy. Without scrubbing, every sector the container has.
    ``table`` is a usage table already computed by :func:`wii_usage_table`."""
    n = d.size // WII_SECTOR
    if scrub:
        u = (table if table is not None else wii_usage_table(d, include_update))[:n]
        u += [False] * (n - len(u))
    else:
        u = [True] * n
    return [x and d.sector_present(i) for i, x in enumerate(u)]


INCOMPLETE = "the image is incomplete (it ends early, or a split part is missing)"


def incomplete_reason(d: DiscReader, *, include_update: bool = False,
                      table: list[bool] | None = None) -> Optional[str]:
    """Why this image can't be a whole game, or None if all the data it needs is there.

    Readers return zeros for data beyond the end of a file, so a truncated
    download or a lost ``.wbf1`` would otherwise be copied (and "verified")
    without complaint. Wii: every sector the file tables use must be stored in
    the image. GameCube: the image must reach the end of its last file."""
    try:
        _gid, _t, platform, *_ = parse_header(d.read(0, 0x60))
        if d.missing(0, 0x440):
            return INCOMPLETE
        if platform == "gc":
            files, layout = parse_fst(d.read, 0)
            end = max([o + s for o, s in layout.values()] + [f.offset + f.size for f in files if f.size])
            if end > 2 * GC_DISC_SIZE:
                return "the image's file table is damaged"
            if any(d.missing(o, min(WII_SECTOR, end - o)) for o in range(0, end, WII_SECTOR)):
                return INCOMPLETE
            return None
        if table is None:
            table = wii_usage_table(d, include_update)
        for sec, u in enumerate(table):
            if not u:
                continue
            if d.missing(sec * WII_SECTOR, WII_SECTOR):
                return INCOMPLETE
            if not d.sector_present(sec):
                return "the image is incomplete (data the game needs was removed from it)"
    except DiscError as e:
        return f"the image is damaged ({e})"
    return None


def _align(n: int, a: int) -> int:
    return (n + a - 1) // a * a
