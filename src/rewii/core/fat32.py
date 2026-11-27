"""A small, pure-Python FAT32 formatter (and boot-sector parser).

Windows' built-in tools refuse to make FAT32 volumes larger than 32 GB, but the
Wii needs exactly that: one FAT32 partition on an MBR disk, ideally with 32 KiB
clusters. :func:`format_fat32` writes such a layout to an image file or a raw
device that the caller has already opened up for writing (see ``formatting``).

What gets written (all offsets in 512-byte sectors)::

    LBA 0                  MBR: disk signature + one partition, type 0x0C (FAT32 LBA)
    LBA 1 .. 2047          zeroed (wipes old GPT headers / boot loaders)
    LBA 2048 (1 MiB)       partition start
      +0                   FAT32 boot sector (BPB)
      +1                   FSInfo
      +6, +7               backup boot sector, backup FSInfo
      +reserved            FAT #1, then FAT #2 (zeroed, entries 0-2 set)
      data region          cluster 2 = root directory (zeroed, volume label entry)
    last 1 MiB of disk     zeroed (wipes a backup GPT header if there was one)

Only the reserved area, the two FATs, the root directory cluster and the two
1 MiB wipes are written, so formatting a 2 TB drive takes seconds, not hours.

Limits and assumptions:

* Logical sector size is assumed to be 512 bytes. Practically every USB drive
  (including "4K native" drives in USB enclosures, which emulate 512e) presents
  512-byte logical sectors; the Wii's USB stack only supports 512 anyway.
* The FAT size is computed with the algorithm from Microsoft's ``fatgen103``.
* Maximum volume size is 2 TiB (MBR and the 32-bit sector count in the BPB).
  :func:`format_fat32` refuses larger targets; callers can pass a smaller
  ``size_bytes`` to use just the first 2 TiB of a bigger disk.
* FAT32 needs at least 65,525 clusters. If the requested cluster size would give
  fewer (volumes below ~2 GiB at 32 KiB), the cluster size is halved until it
  fits; the result reports the cluster size actually used.
* The data region is aligned to a multiple of the cluster size (by padding the
  reserved area beyond the minimum 32 sectors), which is faster on flash media.
"""
from __future__ import annotations

import os
import secrets
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from .progress import CancelToken, ProgressFn, Reporter
from .errors import ReWiiError

SECTOR = 512
PART_START_LBA = 2048                 # 1 MiB alignment
MIN_RESERVED = 32
NUM_FATS = 2
MIN_CLUSTERS = 65525                  # fewer than this and it's FAT16 by definition
MAX_CLUSTERS = 0x0FFFFFF5
MAX_SECTORS = 0xFFFFFFFF              # 32-bit sector counts (MBR length, BPB TotSec32)
MAX_SIZE = 2 * 1024 ** 4              # 2 TiB: the largest disk we will partition
MBR_TYPE_FAT32_LBA = 0x0C
MEDIA_FIXED = 0xF8
EOC = 0x0FFFFFFF
WIPE_BYTES = 1024 * 1024
_CHUNK = 4 * 1024 * 1024              # write zeros in 4 MiB chunks (multiple of 512)

_LABEL_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&'()-@^_`{}~")


class Fat32Error(ReWiiError):
    """The requested FAT32 layout is impossible (too small/large, bad label...)."""


# --------------------------------------------------------------------------- geometry
@dataclass(frozen=True)
class Fat32Geometry:
    """Where every structure of the new volume goes. All values in sectors unless noted."""

    total_sectors: int          # whole target (disk or image)
    partition_start: int        # LBA of the volume (0 when partition=False)
    partition_sectors: int      # size of the volume
    sectors_per_cluster: int
    reserved_sectors: int
    fat_sectors: int            # size of ONE FAT
    cluster_count: int          # usable data clusters

    @property
    def cluster_size(self) -> int:
        """Cluster size in bytes."""
        return self.sectors_per_cluster * SECTOR

    @property
    def fat_start(self) -> int:
        """Absolute LBA of the first FAT."""
        return self.partition_start + self.reserved_sectors

    @property
    def data_start(self) -> int:
        """Absolute LBA of cluster 2 (the root directory)."""
        return self.fat_start + NUM_FATS * self.fat_sectors


def normalize_label(label: str) -> str:
    """Upper-case and validate a FAT volume label (max 11 chars). Raises Fat32Error."""
    lab = (label or "").upper().strip()
    if len(lab) > 11:
        raise Fat32Error("The drive name can be at most 11 characters.")
    bad = [c for c in lab if c not in _LABEL_OK]
    if bad:
        raise Fat32Error(f"The drive name can't contain: {''.join(sorted(set(bad)))}")
    return lab


def _fat_size(volume_sectors: int, reserved: int, spc: int) -> int:
    """FAT size in sectors for FAT32 (fatgen103, 512-byte sectors, RootDirSectors=0)."""
    tmp1 = volume_sectors - reserved
    tmp2 = ((256 * spc) + NUM_FATS) // 2
    return (tmp1 + tmp2 - 1) // tmp2


def plan_fat32(size_bytes: int, cluster_size: int = 32768, partition: bool = True,
               shrink_clusters: bool = True) -> Fat32Geometry:
    """Compute the FAT32 layout for a target of ``size_bytes``.

    Raises :class:`Fat32Error` for targets that are too small, larger than 2 TiB,
    or a cluster size that isn't a power of two between 512 B and 64 KiB.
    """
    if cluster_size < SECTOR or cluster_size > 65536 or cluster_size & (cluster_size - 1):
        raise Fat32Error("Cluster size must be a power of two between 512 bytes and 64 KiB.")
    if size_bytes > MAX_SIZE:
        raise Fat32Error("FAT32 on an MBR disk can use at most 2 TB; this target is larger.")
    total = size_bytes // SECTOR
    if partition:
        start = PART_START_LBA
        vol = min(total, MAX_SECTORS + 1) - start
    else:
        if total > MAX_SECTORS:
            raise Fat32Error("Volume too large for FAT32 (over 2 TB).")
        start, vol = 0, total
    if vol <= MIN_RESERVED:
        raise Fat32Error("Target is too small for FAT32.")

    spc = cluster_size // SECTOR
    while True:
        reserved = MIN_RESERVED
        fat = _fat_size(vol, reserved, spc)
        # Pad the reserved area so the data region starts on a cluster boundary.
        data = start + reserved + NUM_FATS * fat
        reserved += (-data) % spc
        # A larger reserved area can only shrink the FAT need; keep the (safe) larger FAT.
        clusters = (vol - reserved - NUM_FATS * fat) // spc if vol > reserved + NUM_FATS * fat else 0
        if clusters >= MIN_CLUSTERS:
            break
        if not shrink_clusters or spc == 1:
            raise Fat32Error(
                f"Target is too small for FAT32 ({size_bytes // (1024 * 1024)} MiB); "
                f"it would only have {clusters} clusters.")
        spc //= 2
    if clusters > MAX_CLUSTERS:
        raise Fat32Error("Too many clusters for FAT32; use a larger cluster size.")
    if fat * SECTOR // 4 < clusters + 2:   # pragma: no cover - guaranteed by fatgen103
        raise Fat32Error("internal error: FAT too small")
    return Fat32Geometry(total, start, vol, spc, reserved, fat, clusters)


# --------------------------------------------------------------------------- structures
def _chs(lba: int) -> bytes:
    """Encode an LBA as legacy CHS (255 heads, 63 sectors); saturates at 1023/254/63."""
    heads, spt = 255, 63
    c = lba // (heads * spt)
    if c > 1023:
        return b"\xfe\xff\xff"
    h = (lba // spt) % heads
    s = lba % spt + 1
    return bytes([h, ((c >> 2) & 0xC0) | s, c & 0xFF])


def build_mbr(geo: Fat32Geometry, disk_signature: int) -> bytes:
    """Return the 512-byte MBR with one bootable-flag-off FAT32 LBA partition."""
    mbr = bytearray(SECTOR)
    # tiny boot stub: "int 18h" (no bootable OS -> let the BIOS try the next device)
    mbr[0:3] = b"\xcd\x18\xf4"
    struct.pack_into("<IH", mbr, 440, disk_signature, 0)
    end = geo.partition_start + geo.partition_sectors - 1
    entry = (b"\x00" + _chs(geo.partition_start) + bytes([MBR_TYPE_FAT32_LBA]) + _chs(end)
             + struct.pack("<II", geo.partition_start, geo.partition_sectors))
    mbr[446:462] = entry
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def build_boot_sector(geo: Fat32Geometry, volume_id: int, label: str) -> bytes:
    """Return the FAT32 boot sector (BPB + extended BPB)."""
    bs = bytearray(SECTOR)
    bs[0:3] = b"\xeb\x58\x90"
    bs[3:11] = b"MSWIN4.1"
    struct.pack_into("<HBHBHHBHHHII", bs, 11,
                     SECTOR, geo.sectors_per_cluster, geo.reserved_sectors, NUM_FATS,
                     0, 0, MEDIA_FIXED, 0, 63, 255, geo.partition_start, geo.partition_sectors)
    struct.pack_into("<IHHIHH", bs, 36, geo.fat_sectors, 0, 0, 2, 1, 6)
    struct.pack_into("<BBBI", bs, 64, 0x80, 0, 0x29, volume_id)
    bs[71:82] = (label or "NO NAME").ljust(11).encode("ascii")
    bs[82:90] = b"FAT32   "
    # boot code: print nothing, just "int 18h" so a PC skips this disk
    bs[90:93] = b"\xcd\x18\xf4"
    bs[510:512] = b"\x55\xaa"
    return bytes(bs)


def build_fsinfo(free_clusters: int, next_free: int = 3) -> bytes:
    """Return the FSInfo sector."""
    fi = bytearray(SECTOR)
    struct.pack_into("<I", fi, 0, 0x41615252)
    struct.pack_into("<IIII", fi, 484, 0x61417272, free_clusters, next_free, 0)
    struct.pack_into("<I", fi, 508, 0xAA550000)
    return bytes(fi)


def _fat_time(t: float) -> tuple[int, int]:
    lt = time.localtime(t)
    d = ((max(lt.tm_year, 1980) - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    tm = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return d, tm


def build_label_entry(label: str, now: Optional[float] = None) -> bytes:
    """Return a 32-byte volume-label directory entry (ATTR_VOLUME_ID)."""
    d, tm = _fat_time(time.time() if now is None else now)
    e = bytearray(32)
    e[0:11] = label.ljust(11).encode("ascii")
    e[11] = 0x08
    struct.pack_into("<HH", e, 14, tm, d)        # creation time/date
    struct.pack_into("<H", e, 18, d)             # last access date
    struct.pack_into("<HH", e, 22, tm, d)        # write time/date
    return bytes(e)


# --------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class BootSectorInfo:
    """What :func:`parse_boot_sector` found in a volume's first sector."""

    filesystem: str             # "FAT32" | "FAT16" | "FAT12" | "exFAT" | "NTFS" | "WBFS" | "unknown"
    bytes_per_sector: int = 0
    sectors_per_cluster: int = 0
    reserved_sectors: int = 0
    num_fats: int = 0
    total_sectors: int = 0
    fat_sectors: int = 0
    root_cluster: int = 0
    hidden_sectors: int = 0
    volume_id: int = 0
    label: str = ""

    @property
    def cluster_size(self) -> int:
        """Bytes per cluster (0 if unknown)."""
        return self.bytes_per_sector * self.sectors_per_cluster


def parse_boot_sector(data: bytes) -> BootSectorInfo:
    """Identify the filesystem in a partition's first sector and read its geometry.

    Recognises FAT12/16/32, exFAT, NTFS and raw WBFS partitions.
    """
    if len(data) < 512:
        return BootSectorInfo("unknown")
    if data[:4] == b"WBFS":
        return BootSectorInfo("WBFS")
    if data[3:11] == b"EXFAT   ":
        bps = 1 << data[108]
        spc = 1 << data[109]
        return BootSectorInfo("exFAT", bps, spc, total_sectors=struct.unpack_from("<Q", data, 72)[0],
                              volume_id=struct.unpack_from("<I", data, 100)[0])
    if data[3:11] == b"NTFS    ":
        bps = struct.unpack_from("<H", data, 11)[0]
        spc = data[13]
        if spc > 0x80:                        # NTFS encodes big clusters as 2^-n
            spc = 1 << (256 - spc)
        return BootSectorInfo("NTFS", bps, spc,
                              total_sectors=struct.unpack_from("<Q", data, 40)[0])
    if data[510:512] != b"\x55\xaa":
        return BootSectorInfo("unknown")
    bps, spc, rsvd, nfats, root_ents, tot16, _media, fat16 = struct.unpack_from("<HBHBHHBH", data, 11)
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1) or nfats == 0:
        return BootSectorInfo("unknown")
    hidden, tot32 = struct.unpack_from("<II", data, 28)
    total = tot16 or tot32
    if fat16 == 0:
        fat32, _flags, _ver, root_clus = struct.unpack_from("<IHHI", data, 36)
        vol_id = struct.unpack_from("<I", data, 67)[0]
        label = data[71:82].decode("ascii", "replace").rstrip()
        return BootSectorInfo("FAT32", bps, spc, rsvd, nfats, total, fat32, root_clus, hidden,
                              vol_id, label)
    root_secs = (root_ents * 32 + bps - 1) // bps
    clusters = (total - rsvd - nfats * fat16 - root_secs) // spc
    vol_id = struct.unpack_from("<I", data, 39)[0]
    label = data[43:54].decode("ascii", "replace").rstrip()
    return BootSectorInfo("FAT12" if clusters < 4085 else "FAT16", bps, spc, rsvd, nfats, total,
                          fat16, 0, hidden, vol_id, label)


def parse_mbr(data: bytes) -> list[tuple[int, int, int]]:
    """Return ``[(type, start_lba, sectors), ...]`` for the non-empty MBR entries.

    Returns ``[]`` if the sector has no 0x55AA signature. A single type-0xEE
    entry means the disk is really GPT (protective MBR).
    """
    if len(data) < 512 or data[510:512] != b"\x55\xaa":
        return []
    out = []
    for i in range(4):
        e = data[446 + 16 * i: 462 + 16 * i]
        ptype = e[4]
        start, count = struct.unpack_from("<II", e, 8)
        if ptype and count:
            out.append((ptype, start, count))
    return out


# --------------------------------------------------------------------------- formatting
@dataclass(frozen=True)
class FormatResult:
    """Summary of a finished format."""

    geometry: Fat32Geometry
    label: str
    volume_id: int
    disk_signature: int
    bytes_written: int

    @property
    def cluster_size(self) -> int:
        return self.geometry.cluster_size


def _target_size(f: BinaryIO) -> int:
    f.seek(0, os.SEEK_END)
    return f.tell()


def format_fat32(target: str | os.PathLike[str] | BinaryIO, size_bytes: Optional[int] = None,
                 label: str = "REWII", cluster_size: int = 32768, partition: bool = True,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 *, volume_id: Optional[int] = None,
                 disk_signature: Optional[int] = None) -> FormatResult:
    """Write an MBR + FAT32 volume to ``target``.

    ``target`` is an image-file path, a raw device path (``/dev/sdX``,
    ``/dev/rdiskN``, ``\\\\.\\PhysicalDriveN``) or an already-open binary file
    object (opened ``r+b``, unbuffered for raw devices). The caller is
    responsible for unmounting/locking real devices first (see ``formatting``).

    ``size_bytes`` defaults to the current size of the target; pass it
    explicitly for devices whose size can't be read by seeking (macOS rdisk,
    Windows PhysicalDrive). For image files, a missing file is created (sparse)
    at ``size_bytes``; an existing file is grown to ``size_bytes`` if smaller.

    With ``partition=False`` the target itself becomes the volume (no MBR,
    "superfloppy"); use that to format an existing partition device.

    All writes are whole, 512-byte-aligned sectors. The MBR is written last, so
    an interrupted format never looks like a valid volume. Raises
    :class:`Fat32Error` for impossible layouts and ``Cancelled`` if cancelled.
    """
    lab = normalize_label(label)
    rep = Reporter(progress, cancel)
    rep.check()

    own = not hasattr(target, "write")
    fresh = False
    if own:
        path = Path(os.fspath(target))  # type: ignore[arg-type]
        is_file = not path.exists() or stat.S_ISREG(path.stat().st_mode)
        if is_file:
            if size_bytes is None and not path.exists():
                raise Fat32Error("size_bytes is required to create a new image file.")
            fresh = not path.exists() or path.stat().st_size == 0
            if size_bytes is not None:
                # validate before touching anything
                plan_fat32(size_bytes, cluster_size, partition)
                if not path.exists():
                    path.touch()
                if path.stat().st_size < size_bytes:
                    os.truncate(path, size_bytes)
        f: BinaryIO = open(path, "r+b", buffering=0)
    else:
        f = target  # type: ignore[assignment]
    try:
        if size_bytes is None:
            size_bytes = _target_size(f)
        geo = plan_fat32(size_bytes, cluster_size, partition)
        vol_id = secrets.randbits(32) if volume_id is None else volume_id
        sig = (secrets.randbits(32) or 1) if disk_signature is None else disk_signature

        # (offset, length) regions to zero, in sectors
        zero: list[tuple[int, int]] = []
        if partition:
            zero.append((1, geo.partition_start - 1))
        zero.append((geo.partition_start, geo.reserved_sectors + NUM_FATS * geo.fat_sectors
                     + geo.sectors_per_cluster))
        if partition:
            tail = WIPE_BYTES // SECTOR
            first = max(geo.data_start + geo.sectors_per_cluster, geo.total_sectors - tail)
            if first < geo.total_sectors:
                zero.append((first, geo.total_sectors - first))
        if fresh:
            zero = []  # a newly created sparse file already reads as zeros

        total = sum(n for _, n in zero) * SECTOR + (8 + NUM_FATS) * SECTOR
        rep.set_total(total, "Formatting")
        written = 0
        buf = bytes(_CHUNK)

        def put(lba: int, data: bytes) -> None:
            nonlocal written
            f.seek(lba * SECTOR)
            mv = memoryview(data)
            while mv:
                n = f.write(mv)
                if not n:
                    raise OSError("short write")
                mv = mv[n:]
            written += len(data)
            rep.advance(len(data))

        rep.note("Erasing old partition and filesystem data")
        for lba, count in zero:
            remaining = count * SECTOR
            pos = lba
            while remaining:
                n = min(remaining, _CHUNK)
                put(pos, buf[:n])
                pos += n // SECTOR
                remaining -= n

        rep.note("Writing file allocation tables")
        fat0 = struct.pack("<III", 0x0FFFFF00 | MEDIA_FIXED, EOC, EOC).ljust(SECTOR, b"\0")
        for i in range(NUM_FATS):
            put(geo.fat_start + i * geo.fat_sectors, fat0)

        rep.note("Writing root folder")
        root = (build_label_entry(lab) if lab else b"").ljust(SECTOR, b"\0")
        put(geo.data_start, root)

        rep.note("Writing boot sectors")
        boot = build_boot_sector(geo, vol_id, lab)
        fsinfo = build_fsinfo(geo.cluster_count - 1)
        # write the boot sectors' "trail" sector 2 with a signature, as Windows does
        sec2 = bytearray(SECTOR)
        sec2[510:512] = b"\x55\xaa"
        for base in (0, 6):
            put(geo.partition_start + base, boot)
            put(geo.partition_start + base + 1, fsinfo)
            put(geo.partition_start + base + 2, bytes(sec2))
        if partition:
            put(0, build_mbr(geo, sig))
        else:
            rep.advance(SECTOR)
        f.flush()
        try:
            os.fsync(f.fileno())
        except (OSError, ValueError, AttributeError):  # pragma: no cover - some devices/objects
            pass
        rep.update(total, "Done")
        return FormatResult(geo, lab, vol_id, sig if partition else 0, written)
    finally:
        if own:
            f.close()
