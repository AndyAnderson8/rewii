"""Tests for rewii.core.saves using small synthetic saves."""
from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from rewii.core import saves as S
from rewii.core.config import Settings


# --------------------------------------------------------------------------- builders
def make_banner(title: str, subtitle: str = "", icons: int = 1) -> bytes:
    b = bytearray(S.BANNER_BASE_SIZE + icons * S.ICON_SIZE)
    b[0:4] = b"WIBN"
    t = title.encode("utf-16-be")[:0x3E]
    b[0x20:0x20 + len(t)] = t
    st = subtitle.encode("utf-16-be")[:0x3E]
    b[0x60:0x60 + len(st)] = st
    return bytes(b)


def make_databin(game: str = "RMCE", title: str = "Mario Kart Wii",
                 files: dict[str, bytes] | None = None, upper: int = 0x00010000) -> bytes:
    files = files if files is not None else {"rksys.dat": b"hello world" * 10}
    tid = (upper << 32) | struct.unpack(">I", game.encode())[0]
    banner = make_banner(title, "Race!")
    hdr = bytearray(S.HEADER_SIZE)
    struct.pack_into(">QI", hdr, 0, tid, len(banner))
    hdr[0x0E:0x1E] = S.MD5_BLANKER
    hdr[0x20:0x20 + len(banner)] = banner
    hdr[0x0E:0x1E] = hashlib.md5(bytes(hdr)).digest()
    enc_hdr = AES.new(S.SD_KEY, AES.MODE_CBC, S.SD_IV).encrypt(bytes(hdr))
    body = bytearray()
    for name, data in files.items():
        fh = bytearray(0x80)
        struct.pack_into(">II", fh, 0, S.FILE_HDR_MAGIC, len(data))
        fh[8], fh[9], fh[0x0A] = 0x3C, 0, 1
        fh[0x0B:0x0B + len(name)] = name.encode()
        iv = bytes(range(16))
        fh[0x50:0x60] = iv
        padded = data + b"\0" * (-len(data) % 0x40)
        body += fh + AES.new(S.SD_KEY, AES.MODE_CBC, iv).encrypt(padded)
    bk = bytearray(0x80)
    struct.pack_into(">IIIII", bk, 0, 0x70, S.BK_MAGIC, 0x0403AC68, len(files), len(body))
    struct.pack_into(">I", bk, 0x1C, len(body) + 0x3C0)
    struct.pack_into(">Q", bk, 0x60, tid)
    return enc_hdr + bytes(bk) + bytes(body) + b"\0" * 0x3C0


def _dir_checksums(block: bytearray) -> None:
    cs, inv = S.gc_checksum(bytes(block[0:0x1FFC]))
    struct.pack_into(">HH", block, 0x1FFC, cs, inv)


def make_memcard(saves: list[tuple[str, str, str, int, str]], mbits: int = 4) -> bytes:
    """saves: (gamecode, maker, filename, blocks, comment)."""
    total = mbits * 16
    card = bytearray(b"\0" * (total * S.GC_BLOCK))
    hdr = bytearray(b"\xff" * S.GC_BLOCK)
    hdr[0:0x26] = b"\0" * 0x26
    struct.pack_into(">HHH", hdr, 0x20, 0, mbits, 0)
    card[0:S.GC_BLOCK] = hdr
    d = bytearray(b"\xff" * S.GC_BLOCK)
    bat = bytearray(S.GC_BLOCK)
    nxt = 5
    for i, (gc, mk, fn, blocks, comment) in enumerate(saves):
        e = bytearray(b"\xff" * 0x40)
        e[0:4], e[4:6], e[6], e[7] = gc.encode(), mk.encode(), 0xFF, 0
        e[8:0x28] = fn.encode().ljust(0x20, b"\0")
        struct.pack_into(">IIHHBBHHHI", e, 0x28, 100, 0xFFFFFFFF, 0, 0, 4, 0, nxt, blocks,
                         0xFFFF, 0)
        d[i * 0x40:(i + 1) * 0x40] = e
        for k in range(blocks):
            b = nxt + k
            struct.pack_into(">H", bat, 0x0A + (b - 5) * 2, 0xFFFF if k == blocks - 1 else b + 1)
            card[b * S.GC_BLOCK:(b + 1) * S.GC_BLOCK] = bytes([0x40 + i]) * S.GC_BLOCK
        card[nxt * S.GC_BLOCK:nxt * S.GC_BLOCK + 0x40] = comment.encode().ljust(0x40, b"\0")
        nxt += blocks
    struct.pack_into(">h", d, 0x1FFA, 1)
    _dir_checksums(d)
    struct.pack_into(">hHH", bat, 4, 1, total - nxt, nxt - 1)
    cs, inv = S.gc_checksum(bytes(bat[4:]))
    struct.pack_into(">HH", bat, 0, cs, inv)
    old = bytearray(b"\xff" * S.GC_BLOCK)          # stale copy with lower counter
    struct.pack_into(">h", old, 0x1FFA, 0)
    _dir_checksums(old)
    card[S.GC_BLOCK:2 * S.GC_BLOCK] = d
    card[2 * S.GC_BLOCK:3 * S.GC_BLOCK] = old
    card[3 * S.GC_BLOCK:4 * S.GC_BLOCK] = bat
    card[4 * S.GC_BLOCK:5 * S.GC_BLOCK] = bat
    return bytes(card)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(library_dir=str(tmp_path / "lib"))


# --------------------------------------------------------------------------- data.bin
def test_parse_databin(tmp_path: Path) -> None:
    p = tmp_path / "data.bin"
    p.write_bytes(make_databin(files={"a.dat": b"x" * 100, "b.dat": b"y" * 64}))
    info = S.parse_databin(p)
    assert info.game_id == "RMCE"
    assert info.title_id_hex == "00010000524d4345"
    assert info.title == "Mario Kart Wii"
    assert info.banner and info.banner.subtitle == "Race!" and info.banner.icon_count == 1
    assert info.md5_ok
    assert [(f.name, f.size) for f in info.files] == [("a.dat", 100), ("b.dat", 64)]
    assert S.verify_databin_roundtrip(p)


def test_extract_databin(tmp_path: Path) -> None:
    p = tmp_path / "data.bin"
    p.write_bytes(make_databin(files={"save.dat": b"abc" * 50}))
    out = S.extract_databin(p, tmp_path / "out")
    assert (tmp_path / "out" / "save.dat").read_bytes() == b"abc" * 50
    assert (tmp_path / "out" / "banner.bin").read_bytes()[:4] == b"WIBN"
    assert len(out) == 2


def test_parse_databin_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"\0" * 0x10000)
    with pytest.raises(S.SaveError):
        S.parse_databin(p)


# --------------------------------------------------------------------------- GameCube
def test_memcard_and_gci(tmp_path: Path) -> None:
    card = tmp_path / "GALE.raw"
    card.write_bytes(make_memcard([("GALE", "01", "SuperSmashBros", 3, "Melee"),
                                   ("GZLE", "01", "gczelda", 2, "Zelda")]))
    mc = S.parse_memcard(card)
    assert mc.size_mbits == 4 and mc.user_blocks == 59 and mc.active_dir == 0
    assert [(s.game_code, s.block_count, s.comment) for s in mc.saves] == [
        ("GALE", 3, "Melee"), ("GZLE", 2, "Zelda")]
    gci = S.export_gci(card, 1, tmp_path / "out")
    assert gci.name == "01-GZLE-gczelda.gci"
    assert gci.stat().st_size == 0x40 + 2 * S.GC_BLOCK
    g = S.parse_gci(gci)
    assert g.game_code == "GZLE" and g.comment == "Zelda"
    assert gci.read_bytes()[0x40 + 0x40] == 0x41   # data from the 2nd save's blocks


# --------------------------------------------------------------------------- discovery
def make_drive(root: Path) -> None:
    (root / "saves").mkdir(parents=True)
    (root / "saves" / "Mario Kart Wii.bin").write_bytes(make_databin())     # legacy SGM
    (root / "saves" / "GALE.raw").write_bytes(make_memcard([("GALE", "01", "ssbm", 2, "Melee")]))
    (root / "saves" / "ninmem.raw").write_bytes(
        make_memcard([("GALE", "01", "a", 1, "Melee"), ("GZLE", "01", "b", 1, "Zelda")]))
    (root / "saves" / "notes.txt").write_text("hi")
    tid = root / "savegames" / "0001000053534245"
    tid.mkdir(parents=True)
    (tid / "banner.bin").write_bytes(make_banner("Smash Bros. Brawl"))
    (tid / "advsv0.bin").write_bytes(b"1" * 100)
    pw = root / "private" / "wii" / "title" / "SMNE"
    pw.mkdir(parents=True)
    (pw / "data.bin").write_bytes(make_databin("SMNE", "New Super Mario Bros. Wii"))
    (root / "MCBACKUP").mkdir()
    card = root / "saves" / "GALE.raw"
    S.export_gci(card, 0, root / "MCBACKUP")


def test_discover_classifies_by_content(tmp_path: Path) -> None:
    make_drive(tmp_path)
    found = {(s.path.name, s.kind, s.app, s.game_id) for s in S.discover_saves(tmp_path)}
    assert found == {
        ("Mario Kart Wii.bin", S.KIND_DATABIN, S.APP_SGM_LEGACY, "RMCE"),
        ("GALE.raw", S.KIND_MEMCARD, S.APP_NINTENDONT, "GALE"),
        ("ninmem.raw", S.KIND_MEMCARD, S.APP_NINTENDONT, "ninmem"),
        ("0001000053534245", S.KIND_EXTRACTED, S.APP_SGM, "SSBE"),
        ("data.bin", S.KIND_DATABIN, S.APP_DATA_MGMT, "SMNE"),
        ("01-GALE-ssbm.gci", S.KIND_GCI, S.APP_GCMM, "GALE"),
    }
    cat = S.catalog_by_game(S.discover_saves(tmp_path))
    assert len(cat["GALE"]) == 3          # per-game card, multi card, gci
    assert "GZLE" in cat


# --------------------------------------------------------------------------- library
def test_backup_dedupe_and_restore(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    found = S.discover_saves(drive)
    mkw = next(s for s in found if s.game_id == "RMCE")
    r1 = S.backup_save(mkw, settings, now=datetime(2026, 1, 1, 12, 0, 0))
    assert r1.created
    assert r1.version.dir.parent.name == "RMCE - Mario Kart Wii"
    assert r1.version.dir.name == "20260101-120000"
    assert (r1.version.dir / "Mario Kart Wii.bin").is_file()
    meta = json.loads((r1.version.dir / S.SIDECAR).read_text())
    assert meta["kind"] == S.KIND_DATABIN and len(meta["sha1"]) == 40
    r2 = S.backup_save(mkw, settings, now=datetime(2026, 1, 2))
    assert not r2.created and r2.version.dir == r1.version.dir
    # restore to Data Management location
    res = S.restore_save(r1.version, drive, S.APP_DATA_MGMT, settings)
    assert res.dest == drive / "private" / "wii" / "title" / "RMCE" / "data.bin"
    assert res.copied and res.previous_backup is None
    again = S.restore_save(r1.version, drive, S.APP_SGM, settings)
    assert not again.copied                      # identical already there


def test_restore_backs_up_different_existing(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    card = next(s for s in S.discover_saves(drive) if s.path.name == "GALE.raw")
    v = S.backup_save(card, settings).version
    # the drive save changes afterwards
    card.path.write_bytes(make_memcard([("GALE", "01", "newer", 4, "Melee")]))
    res = S.restore_save(v, drive, S.APP_NINTENDONT, settings)
    assert res.copied and res.previous_backup is not None
    assert res.previous_backup.sha1 != v.sha1
    assert S.sha1_file(drive / "saves" / "GALE.raw") == v.sha1
    assert len(S.list_backups(settings, "GALE")) == 2


def test_restore_destinations(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    vs = {s.path.name: S.backup_save(s, settings).version for s in S.discover_saves(drive)}
    ext = vs["0001000053534245"]
    assert S.restore_destination(ext, drive, S.APP_SGM) == drive / "savegames" / "0001000053534245"
    with pytest.raises(S.SaveError):
        S.restore_destination(ext, drive, S.APP_DATA_MGMT)
    assert S.restore_destination(vs["ninmem.raw"], drive, S.APP_GCMM) == drive / "MCBACKUP" / "ninmem.raw"
    with pytest.raises(S.SaveError):
        S.restore_destination(vs["01-GALE-ssbm.gci"], drive, S.APP_NINTENDONT)
    # extracted folder round-trip
    import shutil
    shutil.rmtree(drive / "savegames")
    res = S.restore_save(ext, drive, S.APP_SGM, settings)
    assert (res.dest / "advsv0.bin").read_bytes() == b"1" * 100


# --------------------------------------------------------------------------- regressions
def test_game_id_from_save_cannot_leave_the_drive(tmp_path: Path, settings: Settings) -> None:
    assert S._printable_id(b"/tmp") == "2F746D70" and S._printable_id(b"RMCE") == "RMCE"
    assert S._printable_id(b"a:\\b") != "a:\\b" and S._printable_id(b"../.") != "../."
    evil = tmp_path / "evil.bin"
    evil.write_bytes(make_databin(game="/tmp"))
    s = S.classify_save(evil)
    assert s is not None and s.game_id == "2F746D70"
    v = S.backup_save(s, settings).version
    with pytest.raises(S.SaveError):
        S.restore_destination(v, tmp_path / "drive", S.APP_DATA_MGMT)
    # a hand-edited sidecar can't smuggle a path in either
    for gid in ("/tmp", "..\\x", "C:/a", "a/..", "    "):
        v.game_id = gid
        with pytest.raises(S.SaveError):
            S.restore_destination(v, tmp_path / "drive", S.APP_DATA_MGMT)
    v.kind, v.title_id = S.KIND_EXTRACTED, "../../outside"
    with pytest.raises(S.SaveError):
        S.restore_destination(v, tmp_path / "drive", S.APP_SGM)


def test_backup_is_not_trusted_when_library_copy_is_gone(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    mkw = next(s for s in S.discover_saves(drive) if s.game_id == "RMCE")
    r1 = S.backup_save(mkw, settings, now=datetime(2026, 1, 1))
    r1.version.path.unlink()                      # sidecar stays, the save itself is gone
    r2 = S.backup_save(mkw, settings, now=datetime(2026, 1, 2))
    assert r2.created and r2.version.path.is_file() and r2.version.dir != r1.version.dir
    r2.version.path.write_bytes(b"bit rot")       # or silently damaged
    r3 = S.backup_save(mkw, settings, now=datetime(2026, 1, 3))
    assert r3.created and S.sha1_file(r3.version.path) == r3.version.sha1


def test_restore_keeps_drive_save_when_library_copy_is_bad(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    card = next(s for s in S.discover_saves(drive) if s.path.name == "GALE.raw")
    v = S.backup_save(card, settings).version
    newer = make_memcard([("GALE", "01", "newer", 4, "Melee")])
    card.path.write_bytes(newer)
    v.path.unlink()                               # library copy missing
    with pytest.raises(S.SaveError, match="nothing was changed"):
        S.restore_save(v, drive, S.APP_NINTENDONT, settings)
    assert card.path.read_bytes() == newer
    v.path.write_bytes(b"damaged")                # library copy damaged
    with pytest.raises(S.SaveError, match="nothing was changed"):
        S.restore_save(v, drive, S.APP_NINTENDONT, settings)
    assert card.path.read_bytes() == newer
    assert [p.name for p in card.path.parent.iterdir() if "rewii" in p.name] == []


def test_restore_failure_midway_leaves_drive_save(tmp_path: Path, settings: Settings, monkeypatch) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    card = next(s for s in S.discover_saves(drive) if s.path.name == "GALE.raw")
    v = S.backup_save(card, settings).version
    newer = make_memcard([("GALE", "01", "newer", 4, "Melee")])
    card.path.write_bytes(newer)
    real = S._copy_path

    def failing(src: Path, dest: Path) -> None:
        if Path(dest).parent == card.path.parent:  # the copy onto the drive fails (drive full)
            raise OSError(28, "No space left on device")
        real(src, dest)
    monkeypatch.setattr(S, "_copy_path", failing)
    with pytest.raises(OSError):
        S.restore_save(v, drive, S.APP_NINTENDONT, settings)
    assert card.path.read_bytes() == newer         # still there, and it was backed up first
    assert len(S.list_backups(settings, "GALE")) == 2
    assert sorted(p.name for p in card.path.parent.iterdir() if "rewii" in p.name) == []


def test_sidecar_cannot_point_outside_its_version_folder(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    make_drive(drive)
    v = S.backup_save(S.discover_saves(drive)[0], settings).version
    side = v.dir / S.SIDECAR
    meta = json.loads(side.read_text())
    for bad in ("../../x", "/etc/passwd", "", ".."):
        side.write_text(json.dumps({**meta, "name": bad}))
        assert S._read_version(v.dir) is None
    side.write_text("[1, 2]")
    assert S._read_version(v.dir) is None
