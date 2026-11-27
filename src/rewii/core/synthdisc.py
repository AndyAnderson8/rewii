"""Builds tiny but structurally valid Wii / GameCube disc images: for the test suite and
for ``rewii selftest`` (which runs inside the packaged app, where the tests aren't).

The Wii image has a real encrypted game partition (ticket with an encrypted
title key, H0 hashes per cluster) containing boot.bin, bi2, an apploader, a DOL,
an FST and files, so the decryption, scrubbing, WBFS and verify code paths run
for real. Images are sparse (the logical Wii size is 4.7 GB).
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from .disc import (GC_MAGIC, WII_MAGIC, WII_SECTOR, WII_SECTOR_DATA, WII_SL_SIZE,
                   aes_cbc_encrypt, common_key)

PART_OFF = 0x50000
DATA_REL = 0x20000            # data area offset inside the partition


def _header(game_id: str, title: str, wii: bool, disc: int = 0) -> bytearray:
    h = bytearray(0x440)
    h[0:6] = game_id.encode()
    h[6] = disc
    if wii:
        struct.pack_into(">I", h, 0x18, WII_MAGIC)
    else:
        struct.pack_into(">I", h, 0x1C, GC_MAGIC)
    t = title.encode()[:0x3F]
    h[0x20:0x20 + len(t)] = t
    return h


def imet_banner() -> bytes:
    b = bytearray(0x600)
    b[0x40:0x44] = b"IMET"
    name = "Test Game".encode("utf-16-be")
    b[0x5C:0x5C + len(name)] = name
    return bytes(b)


def _build_fs(game_id: str, title: str, files: dict[str, bytes], shift: int) -> bytes:
    """boot.bin + bi2 + apploader + dol + fst + file data, as one flat image."""
    img = bytearray(0x2440 + 0x20 + 0x100)
    img[0:0x440] = _header(game_id, title, shift == 2)
    # apploader header: size fields at 0x14/0x18
    struct.pack_into(">II", img, 0x2440 + 0x14, 0x100, 0)
    dol_off = 0x3000
    dol = bytearray(0x200)
    struct.pack_into(">I", dol, 0, 0x100)          # text0 offset
    struct.pack_into(">I", dol, 0x48, 0x80003100)  # text0 addr
    struct.pack_into(">I", dol, 0x90, 0x100)       # text0 size
    names = list(files)
    strtab = b"".join(n.encode() + b"\0" for n in names)
    fst_off = 0x4000
    n = len(names) + 1
    fst = bytearray(n * 12) + strtab
    struct.pack_into(">III", fst, 0, 0x01000000, 0, n)
    data_off = 0x8000
    blobs = []
    soff = 0
    for i, name in enumerate(names, 1):
        struct.pack_into(">III", fst, i * 12, soff, data_off >> shift, len(files[name]))
        soff += len(name) + 1
        blobs.append((data_off, files[name]))
        data_off = (data_off + len(files[name]) + 0x7FFF) & ~0x7FFF
    struct.pack_into(">III", img, 0x420, dol_off >> shift, fst_off >> shift, (len(fst) + 3) >> shift)
    total = max(data_off, fst_off + len(fst))
    img += bytes(total - len(img))
    img[dol_off:dol_off + len(dol)] = dol
    img[fst_off:fst_off + len(fst)] = fst
    for o, b in blobs:
        img[o:o + len(b)] = b
    return bytes(img)


GAME_OFF_RETAIL = 0xF800000    # where retail discs put the game partition


def make_wii_iso(path: Path, game_id: str = "RTST01", title: str = "TEST GAME",
                 files: dict[str, bytes] | None = None, update_partition: bool = False,
                 retail_layout: bool = False, logical_size: int = WII_SL_SIZE) -> Path:
    """``update_partition``: a tiny empty update partition after the game.
    ``logical_size``: how long the (sparse) file is - a real single-layer disc by default.
    ``retail_layout``: like a real disc - an update partition WITH files at 0x50000
    (the same 2 MiB WBFS block as the disc header) and the game at 0xF800000."""
    files = files if files is not None else {"opening.bnr": imet_banner(), "data.bin": b"\x5A" * 70000}
    ck = common_key(0)
    title_key = bytes(range(16))
    tid = b"\x00\x01\x00\x00" + game_id[:4].encode()
    tik = bytearray(0x2A4)
    tik[0x1BF:0x1CF] = aes_cbc_encrypt(ck, tid + bytes(8), title_key)
    tik[0x1DC:0x1E4] = tid

    def clusters(fs: bytes) -> tuple[bytes, int]:
        n = (len(fs) + WII_SECTOR_DATA - 1) // WII_SECTOR_DATA
        return fs + bytes(n * WII_SECTOR_DATA - len(fs)), n

    game_fs, game_n = clusters(_build_fs(game_id, title, files, 2))
    if retail_layout:
        upd_fs, upd_n = clusters(_build_fs("RUPD01", "UPDATE", {"sys.wad": b"\xA5" * (6 << 20)}, 2))
        parts = [(PART_OFF, 1, upd_fs, upd_n), (GAME_OFF_RETAIL, 0, game_fs, game_n)]
    else:
        parts = [(PART_OFF, 0, game_fs, game_n)]
        if update_partition:
            parts.append((PART_OFF + DATA_REL + game_n * WII_SECTOR + 0x8000, 1, bytes(WII_SECTOR_DATA), 1))
    with open(path, "wb") as f:
        f.truncate(logical_size)
        f.seek(0)
        f.write(_header(game_id, title, True))
        f.seek(0x40000)
        f.write(struct.pack(">II", len(parts), 0x40020 >> 2))
        f.seek(0x40020)
        for off, typ, _fs, _n in parts:
            f.write(struct.pack(">II", off >> 2, typ))
        for off, _typ, fs, n in parts:
            hdr = bytearray(tik)
            hdr += bytes(0x2C0 - len(hdr))
            struct.pack_into(">II", hdr, 0x2A4, 0, 0)
            struct.pack_into(">II", hdr, 0x2B8, DATA_REL >> 2, (n * WII_SECTOR) >> 2)
            f.seek(off)
            f.write(hdr)
            for c in range(n):
                chunk = fs[c * WII_SECTOR_DATA:(c + 1) * WII_SECTOR_DATA]
                h0 = b"".join(hashlib.sha1(chunk[i * 0x400:(i + 1) * 0x400]).digest() for i in range(31))
                hashes = bytearray(0x400)
                hashes[:len(h0)] = h0
                enc_h = aes_cbc_encrypt(title_key, bytes(16), bytes(hashes))
                iv = enc_h[0x3D0:0x3E0]
                enc_d = aes_cbc_encrypt(title_key, iv, chunk)
                f.seek(off + DATA_REL + c * WII_SECTOR)
                f.write(enc_h + enc_d)
    return Path(path)


def make_gc_iso(path: Path, game_id: str = "GTST01", title: str = "GC TEST", disc: int = 0,
                size: int = 0x200000) -> Path:
    bnr = bytearray(0x1960)
    bnr[0:4] = b"BNR1"
    for i in range(0x20, 0x1820, 2):                     # opaque red RGB5A3 pixels
        bnr[i:i + 2] = b"\xfc\x00"
    bnr[0x1820:0x1820 + 7] = b"GC TEST"
    bnr[0x1840:0x1840 + 7] = b"TESTCO."
    bnr[0x18E0:0x18E0 + 16] = b"A test GC game.\n"
    fs = bytearray(_build_fs(game_id, title, {"opening.bnr": bytes(bnr)}, 0))
    fs[0:0x400] = _header(game_id, title, False, disc)[:0x400]
    with open(path, "wb") as f:
        f.write(fs)
        f.truncate(max(size, len(fs)))
    return Path(path)


def make_ciso(iso: Path, dest: Path, block: int = 0x200000, scan: int = 64 << 20) -> Path:
    """Pack the first ``scan`` bytes of an ISO as a CISO (zero blocks left out)."""
    mp = bytearray(0x7FF8)
    body = bytearray()
    with open(iso, "rb") as f:
        for i in range(scan // block):
            data = f.read(block)
            if any(data):
                mp[i] = 1
                body += data + bytes(block - len(data))
    Path(dest).write_bytes(b"CISO" + struct.pack("<I", block) + bytes(mp) + bytes(body))
    return Path(dest)
