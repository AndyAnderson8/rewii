"""Writing disc images: WBFS files (with FAT32 splitting) and plain ISO.

Also reads legacy raw WBFS *partitions* (old WBFS-formatted drives or disk images
of them) so users can migrate to the FAT32 layout.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from .disc import (FAT32_SPLIT, WII_DL_SECTORS, WII_SECTOR, DiscError, DiscReader,
                   WbfsFileReader, incomplete_reason, parse_header, used_sectors, wii_usage_table)
from .progress import CancelToken, ProgressFn, Reporter

WBFS_SEC_S = 21                    # 2 MiB WBFS blocks, what WBM/wit use for files
HD_SEC_S = 9                       # 512-byte "hard disk" sectors
COPY_CHUNK = 4 * 1024 * 1024


class SplitWriter:
    """File writer that rolls over to .wbf1/.wbf2 (or .part1...) at ``split`` bytes."""

    def __init__(self, path: Path, split: Optional[int], naming: str = "wbfs"):
        self.base = Path(path)
        self.split = split
        self.naming = naming
        self.paths: list[Path] = []
        self.files: list[BinaryIO] = []
        self.pos = 0
        self._open(0)

    def _name(self, i: int) -> Path:
        if i == 0:
            return self.base
        if self.naming == "wbfs":
            return self.base.with_suffix(f".wbf{i}")
        return self.base.with_name(f"{self.base.stem}.part{i}{self.base.suffix}")

    def _open(self, i: int) -> BinaryIO:
        while len(self.files) <= i:
            p = self._name(len(self.files))
            self.paths.append(p)
            self.files.append(open(p, "w+b"))
        return self.files[i]

    def write_at(self, off: int, data: bytes) -> None:
        while data:
            if self.split:
                i, within = divmod(off, self.split)
                n = min(len(data), self.split - within)
            else:
                i, within, n = 0, off, len(data)
            f = self._open(i)
            f.seek(within)
            f.write(data[:n])
            data = data[n:]
            off += n

    def truncate(self, total: int) -> None:
        if self.split:
            last = max(0, (total - 1) // self.split)
            for i in range(last + 1):
                f = self._open(i)
                f.truncate(min(self.split, total - i * self.split))
        else:
            self._open(0).truncate(total)

    def close(self, sync: bool = True) -> None:
        """Close every part. With ``sync`` the data is flushed to the device first
        and the first error is raised - after all the files have been closed."""
        err: BaseException | None = None
        for f in self.files:
            try:
                if sync and not f.closed:
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                err = err or e
            finally:
                try:
                    f.close()
                except OSError as e:
                    err = err or e
        if sync and err is not None:
            raise err

    def abort(self) -> None:
        """Close without syncing and delete everything written (never raises)."""
        self.close(sync=False)
        _cleanup(self.paths)


def _cleanup(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def write_wbfs(d: DiscReader, dest: Path, *, scrub: bool = True, include_update: bool = False,
               split: Optional[int] = FAT32_SPLIT, progress: ProgressFn | None = None,
               cancel: CancelToken | None = None) -> list[Path]:
    """Write a Wii disc as a WBFS file. Returns the written file paths.

    Raises :class:`DiscError` if the source is missing data the game needs
    (a truncated image, a lost split part); nothing is written then."""
    hdr = d.read(0, 0x100)
    _gid, _t, platform, _n, _v = parse_header(hdr)
    if platform != "wii":
        raise DiscError("WBFS is only for Wii games; GameCube games stay ISO")
    sec_sz = 1 << WBFS_SEC_S
    hd_sz = 1 << HD_SEC_S
    per_blk = sec_sz // WII_SECTOR
    rep = Reporter(progress, cancel, message="Analysing disc")
    rep.note("Analysing disc")
    table = wii_usage_table(d, include_update)
    why = incomplete_reason(d, include_update=include_update, table=table)
    if why:
        raise DiscError(f"Can't copy this game: {why}")
    used = used_sectors(d, scrub=scrub, include_update=include_update, table=table)
    n_blocks_disc = WII_DL_SECTORS // per_blk
    blocks = [b for b in range(n_blocks_disc)
              if any(used[b * per_blk:(b + 1) * per_blk])]
    total = (len(blocks) + 1) * sec_sz
    w = SplitWriter(dest, split)
    try:
        # header + disc info + wlba table live in WBFS block 0
        wlba = [0] * n_blocks_disc
        rep.set_total(len(blocks) * sec_sz, "Copying")
        for n, b in enumerate(blocks, start=1):
            wlba[b] = n
            data = d.read(b * sec_sz, sec_sz)
            w.write_at(n * sec_sz, data)
            rep.advance(sec_sz)
        head = struct.pack(">4sIBBBB", b"WBFS", total // hd_sz, HD_SEC_S, WBFS_SEC_S, 0, 0)
        head += b"\x01" + bytes(hd_sz - len(head) - 1)
        info = hdr + struct.pack(f">{n_blocks_disc}H", *wlba)
        w.write_at(0, head)
        w.write_at(hd_sz, info)
        w.truncate(total)
        w.close()
    except BaseException:
        w.abort()
        raise
    return w.paths


def write_iso(d: DiscReader, dest: Path, *, split: Optional[int] = None, scrub: bool = False,
              progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> list[Path]:
    """Write a full plain ISO (zeros for absent/scrubbed sectors)."""
    hdr = d.read(0, 0x100)
    _gid, _t, platform, _n, _v = parse_header(hdr)
    total = d.size
    rep = Reporter(progress, cancel)
    used = used_sectors(d, scrub=scrub) if platform == "wii" else None
    w = SplitWriter(dest, split, naming="part")
    try:
        rep.set_total(total, "Copying")
        off = 0
        while off < total:
            n = min(COPY_CHUNK, total - off)
            if used is not None and not any(used[off // WII_SECTOR:(off + n + WII_SECTOR - 1) // WII_SECTOR]):
                pass  # leave a hole; truncate() extends with zeros
            else:
                data = bytearray(d.read(off, n))
                if used is not None:
                    for s in range(off // WII_SECTOR, (off + n) // WII_SECTOR):
                        if not used[s]:
                            a = s * WII_SECTOR - off
                            data[a:a + WII_SECTOR] = bytes(WII_SECTOR)
                w.write_at(off, bytes(data))
            off += n
            rep.advance(n)
        w.truncate(total)
        w.close()
    except BaseException:
        w.abort()
        raise
    return w.paths


def copy_file(src: Path, dest: Path, progress: ProgressFn | None = None,
              cancel: CancelToken | None = None, rep: Reporter | None = None) -> None:
    """Chunked copy with progress; removes the partial file on failure/cancel.

    ``dest`` is overwritten, so callers copy to a temporary name and rename."""
    size = src.stat().st_size
    rep = rep or Reporter(progress, cancel, total=size)
    if rep.total == 0:
        rep.set_total(size, f"Copying {src.name}")
    fo = None
    try:
        with open(src, "rb") as fi:
            fo = open(dest, "wb")
            while True:
                b = fi.read(COPY_CHUNK)
                if not b:
                    break
                fo.write(b)
                rep.advance(len(b))
            fo.flush()
            os.fsync(fo.fileno())
            fo.close()
    except BaseException:
        if fo is not None:
            try:
                fo.close()
            except OSError:
                pass
        _cleanup([dest])
        raise


# --------------------------------------------------------------------------- legacy partitions
class _RawFile:
    def __init__(self, path: Path, offset: int = 0):
        self.f = open(path, "rb")
        self.offset = offset
        try:                                   # works for image files and block devices
            end = self.f.seek(0, os.SEEK_END)
        except OSError:
            end = 0
        self.total: Optional[int] = end - offset if end > offset else None

    def read(self, off: int, size: int) -> bytes:
        self.f.seek(self.offset + off)
        b = self.f.read(size)
        return b + bytes(size - len(b))

    def close(self) -> None:
        self.f.close()


@dataclass
class WbfsPartitionGame:
    slot: int
    game_id: str
    title: str
    blocks: int
    size: int


class WbfsPartition:
    """A raw WBFS partition: a disk image file or a block device (needs admin)."""

    def __init__(self, path: Path | str, offset: int = 0):
        self.path = Path(path)
        self.raw = _RawFile(self.path, offset)
        try:
            hdr = self.raw.read(0, 512)
            magic, n_hd_sec, hd_s, wbfs_s = struct.unpack(">4sIBB", hdr[:10])
            if magic != b"WBFS":
                raise DiscError(f"{self.path}: no WBFS partition found")
            if not (9 <= hd_s <= 16 and 15 <= wbfs_s <= 31):
                raise DiscError(f"{self.path}: the WBFS partition header is damaged")
            self.hd_sz = 1 << hd_s
            self.sec_sz = 1 << wbfs_s
            self.n_hd_sec = n_hd_sec
            hdr = self.raw.read(0, self.hd_sz)
        except BaseException:
            self.raw.close()
            raise
        self.max_discs = (self.hd_sz - 12)
        self.disc_table = hdr[12:12 + self.max_discs]
        self.n_wbfs_sec_disc = (WII_DL_SECTORS * WII_SECTOR) // self.sec_sz
        self.skipped: list[tuple[int, str]] = []      # (slot, reason) for unreadable slots

    @property
    def size(self) -> int:
        return self.n_hd_sec * self.hd_sz

    def games(self) -> list[WbfsPartitionGame]:
        """The games on the partition. A slot that can't be read is left out and
        noted in :attr:`skipped` instead of failing the whole listing."""
        out = []
        self.skipped = []
        for slot, used in enumerate(self.disc_table):
            if not used:
                continue
            try:
                r = self.open_game(slot)
                gid, title, *_ = parse_header(r.header)
            except DiscError as e:
                self.skipped.append((slot, str(e)))
                continue
            blocks = sum(1 for x in r.wlba if x)
            out.append(WbfsPartitionGame(slot, gid, title, blocks, blocks * self.sec_sz))
        return out

    def open_game(self, slot: int) -> WbfsFileReader:
        """A reader for one game. It shares this partition's file handle, so
        closing the reader does not close the partition."""
        return WbfsFileReader(self.path, slot=slot, _stream=self.raw, _part_off=0)

    def close(self) -> None:
        self.raw.close()


def find_wbfs_partition(path: Path | str) -> int | None:
    """Locate a WBFS partition in a whole-disk image/device via the MBR. Returns byte offset."""
    with open(path, "rb") as f:
        head = f.read(512)
        if head[:4] == b"WBFS":
            return 0
        if head[510:512] != b"\x55\xaa":
            return None
        for i in range(4):
            e = head[446 + i * 16:462 + i * 16]
            lba = struct.unpack("<I", e[8:12])[0]
            if lba:
                f.seek(lba * 512)
                if f.read(4) == b"WBFS":
                    return lba * 512
    return None
