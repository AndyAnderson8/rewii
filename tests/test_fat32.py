"""Tests for the pure-Python FAT32 formatter (image files only, never real devices)."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import warnings
from pathlib import Path

import pytest

from rewii.core.fat32 import (MAX_SIZE, MIN_CLUSTERS, PART_START_LBA, SECTOR, Fat32Error,
                              format_fat32, normalize_label, parse_boot_sector, parse_mbr,
                              plan_fat32)
from rewii.core.progress import Cancelled, CancelToken
from rewii.core.fileops import make_sparse, set_length

MiB = 1024 ** 2
GiB = 1024 ** 3
TiB = 1024 ** 4

FSCK = shutil.which("fsck.fat") or next(
    (p for p in ("/usr/sbin/fsck.fat", "/sbin/fsck.fat") if os.path.exists(p)), None)


def extract_partition(img: Path, out: Path) -> tuple[int, int]:
    """Copy the first MBR partition of a (sparse) image to ``out``, copying only data extents."""
    with open(img, "rb") as f:
        ptype, start, count = parse_mbr(f.read(512))[0]
        off, end = start * SECTOR, (start + count) * SECTOR
        with open(out, "wb") as o:
            o.truncate(count * SECTOR)
            pos = off
            while pos < end:
                try:
                    d = os.lseek(f.fileno(), pos, os.SEEK_DATA)
                except OSError:
                    break
                if d >= end:
                    break
                h = min(os.lseek(f.fileno(), d, os.SEEK_HOLE), end)
                f.seek(d)
                o.seek(d - off)
                while d < h:
                    n = min(h - d, 16 * MiB)
                    o.write(f.read(n))
                    d += n
                pos = h
    return start, count


def fsck(part: Path) -> subprocess.CompletedProcess:
    return subprocess.run([FSCK, "-n", "-v", str(part)], capture_output=True, text=True)


def read_back(img: Path) -> tuple[list, object, bytes, bytes]:
    with open(img, "rb") as f:
        mbr = f.read(512)
        parts = parse_mbr(mbr)
        start = parts[0][1]
        f.seek(start * SECTOR)
        boot = f.read(512)
        fsinfo = f.read(512)
        info = parse_boot_sector(boot)
        f.seek((start + 6) * SECTOR)
        backup = f.read(512)
        assert backup == boot
        f.seek((start + info.reserved_sectors) * SECTOR)
        fat1 = f.read(12)
        f.seek((start + info.reserved_sectors + info.fat_sectors) * SECTOR)
        fat2 = f.read(12)
        assert fat1 == fat2
        f.seek((start + info.reserved_sectors + 2 * info.fat_sectors) * SECTOR)
        root = f.read(32)
    return parts, info, fat1, root + fsinfo


# --------------------------------------------------------------------------- geometry
def test_plan_fatgen_math_and_alignment():
    geo = plan_fat32(64 * GiB, 32768)
    assert geo.cluster_size == 32768
    assert geo.partition_start == PART_START_LBA
    assert geo.reserved_sectors >= 32
    # data region aligned to the cluster size (absolute LBA)
    assert geo.data_start % geo.sectors_per_cluster == 0
    # FAT big enough for every cluster (+2 reserved entries), and not wastefully big
    assert geo.fat_sectors * SECTOR // 4 >= geo.cluster_count + 2
    assert geo.fat_sectors * SECTOR // 4 < geo.cluster_count + 2 + 2 * 128 + 128
    # clusters fill the volume
    used = geo.reserved_sectors + 2 * geo.fat_sectors + geo.cluster_count * geo.sectors_per_cluster
    assert geo.partition_sectors - used < geo.sectors_per_cluster


def test_small_volume_shrinks_cluster_size():
    geo = plan_fat32(64 * MiB, 32768)
    assert geo.cluster_count >= MIN_CLUSTERS
    assert geo.cluster_size < 32768
    with pytest.raises(Fat32Error):
        plan_fat32(64 * MiB, 32768, shrink_clusters=False)
    with pytest.raises(Fat32Error):
        plan_fat32(8 * MiB)


def test_size_limits():
    geo = plan_fat32(2 * TiB, 32768)
    assert geo.partition_start + geo.partition_sectors <= 2 ** 32
    assert geo.partition_sectors <= 0xFFFFFFFF
    with pytest.raises(Fat32Error):
        plan_fat32(2 * TiB + SECTOR)
    with pytest.raises(Fat32Error):
        plan_fat32(2 * TiB, partition=False)     # 2^32 sectors don't fit TotSec32
    with pytest.raises(Fat32Error):
        plan_fat32(GiB, cluster_size=3000)


def test_label_rules():
    assert normalize_label("ReWii") == "REWII"
    assert normalize_label("") == ""
    with pytest.raises(Fat32Error):
        normalize_label("TWELVE_CHARS")
    with pytest.raises(Fat32Error):
        normalize_label("A*B")


# --------------------------------------------------------------------------- images
@pytest.mark.parametrize("size", [64 * MiB, 1 * GiB, 64 * GiB, 2 * TiB])
def test_format_image_parses_back(tmp_path, size):
    img = tmp_path / "disk.img"
    events = []
    res = format_fat32(img, size, label="ReWii", progress=events.append,
                       volume_id=0x12345678, disk_signature=0xCAFEBABE)
    assert img.stat().st_size == size
    if hasattr(img.stat(), "st_blocks"):
        assert img.stat().st_blocks * 512 < 64 * MiB      # stayed sparse
    parts, info, fat, rest = read_back(img)
    assert parts == [(0x0C, PART_START_LBA, res.geometry.partition_sectors)]
    assert info.filesystem == "FAT32"
    assert info.cluster_size == res.cluster_size
    assert info.hidden_sectors == PART_START_LBA
    assert info.volume_id == 0x12345678
    assert info.label == "REWII"
    assert info.root_cluster == 2
    assert struct.unpack("<III", fat) == (0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF)
    root, fsinfo = rest[:32], rest[32:]
    assert root[:11] == b"REWII      " and root[11] == 0x08
    assert struct.unpack_from("<I", fsinfo, 0)[0] == 0x41615252
    assert struct.unpack_from("<I", fsinfo, 488)[0] == res.geometry.cluster_count - 1
    with open(img, "rb") as f:
        assert struct.unpack_from("<I", f.read(512), 440)[0] == 0xCAFEBABE
    assert events and events[-1].done == events[-1].total
    if size >= 64 * GiB:
        assert res.cluster_size == 32768


@pytest.mark.skipif(FSCK is None, reason="fsck.fat (dosfstools) not installed")
@pytest.mark.parametrize("size", [64 * MiB, 1 * GiB, 64 * GiB, 2 * TiB])
def test_fsck_clean(tmp_path, size):
    img = tmp_path / "disk.img"
    format_fat32(img, size, label="REWII")
    part = tmp_path / "part.bin"
    extract_partition(img, part)
    r = fsck(part)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1/" in r.stdout.splitlines()[-1]          # one cluster used (the root dir)


@pytest.mark.skipif(FSCK is None, reason="fsck.fat (dosfstools) not installed")
def test_superfloppy_fsck(tmp_path):
    img = tmp_path / "vol.img"
    res = format_fat32(img, 4 * GiB, label="", partition=False)
    with open(img, "rb") as f:
        info = parse_boot_sector(f.read(512))
    assert info.filesystem == "FAT32" and info.hidden_sectors == 0 and info.label == "NO NAME"
    assert res.geometry.partition_start == 0
    assert fsck(img).returncode == 0


def test_reformat_overwrites_old_data(tmp_path):
    """Formatting an existing non-empty image zeroes old GPT/FAT structures."""
    img = tmp_path / "disk.img"
    with open(img, "wb") as f:
        make_sparse(f)
        set_length(f, 256 * MiB)
        f.seek(512)
        f.write(b"EFI PART" + b"\xff" * 504)          # fake primary GPT header
        f.seek(256 * MiB - 512)
        f.write(b"EFI PART" + b"\xff" * 504)          # fake backup GPT header
        f.seek(3 * MiB)
        f.write(b"\xee" * MiB)                        # junk where the FATs will go
    res = format_fat32(img, label="REWII")            # size taken from the file
    assert res.geometry.total_sectors == 256 * MiB // 512
    with open(img, "rb") as f:
        f.seek(512)
        assert f.read(512) == bytes(512)
        f.seek(256 * MiB - 512)
        assert f.read(512) == bytes(512)
        geo = res.geometry
        f.seek((geo.fat_start * SECTOR) + 12)
        assert f.read(geo.fat_sectors * SECTOR - 12) == bytes(geo.fat_sectors * SECTOR - 12)
    if FSCK:
        part = tmp_path / "p.bin"
        extract_partition(img, part)
        assert fsck(part).returncode == 0


def test_file_object_target_and_aligned_writes(tmp_path):
    """Writes to an open object are whole, aligned sectors (raw devices require that)."""
    img = tmp_path / "disk.img"
    img.write_bytes(b"")
    os.truncate(img, 128 * MiB)

    class Recorder:
        def __init__(self, f):
            self.f, self.bad = f, []

        def seek(self, *a):
            return self.f.seek(*a)

        def tell(self):
            return self.f.tell()

        def write(self, b):
            if self.f.tell() % 512 or len(b) % 512:
                self.bad.append((self.f.tell(), len(b)))
            return self.f.write(b)

        def flush(self):
            self.f.flush()

        def fileno(self):
            return self.f.fileno()

    with open(img, "r+b", buffering=0) as raw:
        rec = Recorder(raw)
        format_fat32(rec, 128 * MiB)
    assert rec.bad == []


def test_cancel(tmp_path):
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        format_fat32(tmp_path / "x.img", 64 * MiB, cancel=tok)


def test_refuses_too_big_before_touching(tmp_path):
    img = tmp_path / "big.img"
    with pytest.raises(Fat32Error):
        format_fat32(img, MAX_SIZE + MiB)
    assert not img.exists()
    with pytest.raises(Fat32Error):
        format_fat32(tmp_path / "nosize.img")


def test_parse_boot_sector_other_filesystems():
    exfat = bytearray(512)
    exfat[3:11] = b"EXFAT   "
    exfat[108], exfat[109] = 9, 8
    assert parse_boot_sector(bytes(exfat)).filesystem == "exFAT"
    assert parse_boot_sector(bytes(exfat)).cluster_size == 512 * 256
    ntfs = bytearray(512)
    ntfs[3:11] = b"NTFS    "
    struct.pack_into("<HB", ntfs, 11, 512, 8)
    assert parse_boot_sector(bytes(ntfs)).cluster_size == 4096
    assert parse_boot_sector(b"WBFS" + bytes(508)).filesystem == "WBFS"
    assert parse_boot_sector(bytes(512)).filesystem == "unknown"
    assert parse_mbr(bytes(512)) == []


def test_pyfatfs_can_write_and_read(tmp_path):
    pyfatfs = pytest.importorskip("pyfatfs.PyFatFS")
    img = tmp_path / "disk.img"
    format_fat32(img, 64 * GiB, label="REWII")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fs = pyfatfs.PyFatFS(str(img), offset=PART_START_LBA * SECTOR)
        try:
            fs.makedirs("/apps/usbloader_gx")
            fs.writebytes("/apps/usbloader_gx/boot.dol", b"\x42" * 100_000)
        finally:
            fs.close()
        fs = pyfatfs.PyFatFS(str(img), offset=PART_START_LBA * SECTOR, read_only=True)
        try:
            assert fs.listdir("/") == ["apps"]
            assert fs.readbytes("/apps/usbloader_gx/boot.dol") == b"\x42" * 100_000
        finally:
            fs.close()
