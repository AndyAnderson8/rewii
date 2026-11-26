"""Regression tests for the data-safety review of the game / disc / channel core.

Each block names the finding it pins down (numbers match the review report).
"""
from __future__ import annotations

import os
import struct
import time

import pytest

import fakedisc
from fakedisc import DATA_REL, PART_OFF, make_ciso, make_gc_iso, make_wii_iso
from rewii.core import channels as CH
from rewii.core import cleanrip as C
from rewii.core import convert
from rewii.core import disc as D
from rewii.core import forwarder as F
from rewii.core import games as G
from rewii.core import gcforwarder as GC
from rewii.core import wbfs as W
from rewii.core.config import Settings
from rewii.core.layout import DriveLayout
from rewii.core.progress import Cancelled, CancelToken


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    from rewii.core import config
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path / "cache")


@pytest.fixture
def wii(tmp_path):
    return make_wii_iso(tmp_path / "game.iso")


@pytest.fixture
def lay(tmp_path):
    l = DriveLayout(tmp_path / "drive")
    l.ensure_dirs()
    return l


@pytest.fixture
def settings(tmp_path):
    s = Settings(library_dir=str(tmp_path / "lib"))
    s.ensure_library()
    return s


@pytest.fixture
def certs(monkeypatch):
    monkeypatch.setattr(F, "cert_chain", lambda: bytes(2560))


def big_wii(path, mb: int = 9):
    """A fake Wii disc with enough data to span several 2 MiB WBFS blocks."""
    return make_wii_iso(path, files={"opening.bnr": fakedisc.imet_banner(), "data.bin": os.urandom(mb << 20)})


# ------------------------------------------------------------------ #1 / #10 rename
def test_rename_flat_drive_game_moves_it_instead_of_renaming_wbfs(wii, lay):
    with D.open_disc(wii) as d:
        W.write_wbfs(d, lay.wbfs / "RTST01.wbfs", split=3 << 20)          # loose in wbfs/, split
    other = lay.wbfs / "Other [RXXX01]"
    other.mkdir()
    g = G.scan_drive(lay)[0]
    assert g.path.parent == lay.wbfs and len(g.files) > 1
    r = G.rename_game(lay, g, "New Name")
    assert lay.wbfs.is_dir() and other.is_dir()                          # container untouched
    assert r.path == lay.wbfs / "New Name [RTST01]" / "RTST01.wbfs"
    assert len(r.files) == len(g.files) and all(p.exists() for p in r.files)
    assert not (lay.wbfs / "RTST01.wbfs").exists() and not (lay.wbfs / "RTST01.wbf1").exists()
    assert G.verify_game(r.path).ok and not G.scan_drive(lay)[0].problem


def test_rename_game_in_shared_folder_leaves_the_other_game_alone(tmp_path, lay):
    shared = lay.gc_games / "stuff"
    shared.mkdir()
    make_gc_iso(shared / "a.iso", game_id="GAAE01")
    make_gc_iso(shared / "b.iso", game_id="GBBE01")
    g = next(x for x in G.scan_drive(lay) if x.game_id == "GAAE01")
    r = G.rename_game(lay, g, "Alpha")
    assert r.path == lay.gc_games / "Alpha [GAAE01]" / "game.iso"
    assert (shared / "b.iso").exists() and not (shared / "a.iso").exists()


def test_rename_flat_library_gc_game_keeps_library_folder(settings):
    make_gc_iso(settings.gc_games_dir / "Some Game.iso")
    keep = settings.gc_games_dir / "Other [GXXE01]"
    keep.mkdir()
    g = G.scan_library(settings)[0]
    r = G.rename_library_game(settings, g, "Renamed")
    assert settings.gc_games_dir.is_dir() and keep.is_dir()
    assert r.path == settings.gc_games_dir / "Renamed [GTST01]" / "game.iso" and r.path.exists()


def test_rename_library_game_moves_split_parts_and_refuses_unknown_ids(wii, settings):
    with D.open_disc(wii) as d:
        parts = W.write_wbfs(d, settings.wii_games_dir / "Old [RTST01].wbfs", split=3 << 20)
    assert len(parts) > 1
    g = G.scan_library(settings)[0]
    r = G.rename_library_game(settings, g, "Fresh")
    assert [p.name for p in r.files][:2] == ["Fresh [RTST01].wbfs", "Fresh [RTST01].wbf1"]
    assert not any(p.exists() for p in parts) and G.verify_game(r.path).ok
    rvz = settings.wii_games_dir / "mystery.rvz"
    rvz.write_bytes(b"RVZ\x01" + bytes(100))
    unknown = next(x for x in G.scan_library(settings) if x.game_id == G.UNKNOWN_ID)
    with pytest.raises(G.GameError):
        G.rename_library_game(settings, unknown, "Whatever")
    assert rvz.exists()


def test_case_only_rename_is_allowed(wii, lay):
    g = G.install_game(lay, wii, title="mario")
    r = G.rename_game(lay, g, "Mario")
    assert r.path.parent.name == "Mario [RTST01]" and r.path.exists()
    assert "Mario [RTST01]" in os.listdir(lay.wbfs)
    with pytest.raises(G.GameError):                                      # a real clash still refuses
        (lay.wbfs / "Taken [RTST01]").mkdir()
        G.rename_game(lay, r, "Taken")


# ------------------------------------------------------------------ #2 incomplete images
def test_truncated_iso_fails_verify_and_is_refused(tmp_path, lay, settings):
    iso = big_wii(tmp_path / "t.iso", 2)
    assert G.verify_game(iso).ok
    with open(iso, "r+b") as f:
        f.truncate(PART_OFF + DATA_REL + 0x8000)                          # one cluster survives
    r = G.verify_game(iso)
    assert not r.ok and r.missing_clusters > 0 and "incomplete" in r.message
    with D.open_disc(iso) as d, pytest.raises(D.DiscError):
        W.write_wbfs(d, tmp_path / "out.wbfs", scrub=False)
    assert not (tmp_path / "out.wbfs").exists()
    with pytest.raises(G.GameError, match="incomplete"):
        G.install_game(lay, iso)
    with pytest.raises(G.GameError, match="incomplete"):
        G.add_to_library(settings, iso, move=True)
    assert iso.exists()                                                   # never deleted by a failed move
    assert G.scan_drive(lay) == [] and not list(lay.wbfs.iterdir())
    assert not list(settings.wii_games_dir.iterdir())


def test_missing_split_part_fails_verify_and_is_refused(tmp_path, lay):
    iso = big_wii(tmp_path / "b.iso")
    with D.open_disc(iso) as d:
        paths = W.write_wbfs(d, tmp_path / "RTST01.wbfs", split=5 << 20)
    assert len(paths) == 3 and G.verify_game(paths[0]).ok
    paths[2].unlink()
    r = G.verify_game(paths[0])
    assert not r.ok and r.missing_clusters > 0
    with pytest.raises(G.GameError):
        G.install_game(lay, paths[0])
    assert not list(lay.wbfs.iterdir())


def test_blank_cluster_the_game_needs_fails_verify(wii):
    with open(wii, "r+b") as f:
        f.seek(PART_OFF + DATA_REL + 0x8000 * 2)                          # a cluster holding file data
        f.write(bytes(0x8000))
    r = G.verify_game(wii)
    assert not r.ok and r.missing_clusters == 1 and r.bad_clusters == 0


def test_scrubbed_wbfs_without_update_partition_still_verifies(tmp_path, lay):
    iso = make_wii_iso(tmp_path / "u.iso", update_partition=True)
    g = G.install_game(lay, iso)                                          # scrub drops the update partition
    assert G.verify_game(g.path).ok
    with D.open_disc(g.path) as d:
        assert D.incomplete_reason(d, include_update=True) is None


def test_truncated_gamecube_image(tmp_path, lay):
    gc = make_gc_iso(tmp_path / "gc.iso")
    assert G.verify_game(gc).ok
    with open(gc, "r+b") as f:
        f.truncate(0x8100)                                                # cuts into opening.bnr
    assert not G.verify_game(gc).ok
    with pytest.raises(G.GameError, match="incomplete"):
        G.install_game(lay, gc)
    assert not list(lay.gc_games.iterdir())


def test_ciso_reader_reports_missing_data(wii, tmp_path):
    c = make_ciso(wii, tmp_path / "g.ciso", block=0x8000)
    assert G.verify_game(c).ok
    with open(c, "r+b") as f:
        f.truncate(c.stat().st_size - 0x8000 * 3)
    assert not G.verify_game(c).ok


def _sparse_part(path, size, header=b""):
    with open(path, "wb") as f:
        f.write(header)
        f.truncate(size)


def test_cleanrip_detects_missing_last_part_from_sizes(tmp_path):
    hdr = bytearray(0x20)
    hdr[:6] = b"RSPE01"
    hdr[0x18:0x1C] = struct.pack(">I", D.WII_MAGIC)
    chunk = 2 << 30                                                       # CleanRip "2GB" chunks
    _sparse_part(tmp_path / "RSPE01.part0.iso", chunk, bytes(hdr))
    _sparse_part(tmp_path / "RSPE01.part1.iso", chunk)
    d = C.find_dumps(tmp_path)[0]
    assert d.missing_parts == [2] and not d.complete
    assert "not a whole disc" in d.size_problem()
    with pytest.raises(C.CleanRipError):
        C.join_dump(d, tmp_path / "out.iso")
    _sparse_part(tmp_path / "RSPE01.part2.iso", D.WII_SL_SIZE - 2 * chunk)
    d = C.find_dumps(tmp_path)[0]
    assert d.complete and d.size_problem() is None


def test_install_refuses_split_dump_that_is_not_a_whole_disc(wii, tmp_path, lay):
    cr = tmp_path / "cr"
    cr.mkdir()
    with open(wii, "rb") as f:
        (cr / "RTST01.part0.iso").write_bytes(f.read(PART_OFF + DATA_REL + 0x8000 * 3))
    with pytest.raises(G.GameError, match="not a whole disc"):
        G.install_game(lay, cr / "RTST01.part0.iso")
    (cr / "RTST01.part0.iso").rename(cr / "RTST01.part1.iso")             # part 0 gone altogether
    with pytest.raises(G.GameError, match="first part"):
        G.install_game(lay, cr / "RTST01.part1.iso")
    assert not list(lay.wbfs.iterdir())


# ------------------------------------------------------------------ #3 ticket key index
def test_ticket_always_uses_the_standard_common_key(certs):
    banner, dol = fakedisc.imet_banner(), b"DOL" * 100
    seen = set()
    for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        for b in "ABCDEFGHIJKL":                                           # 312 game IDs
            gid = f"R{a}{b}E01"
            for chan, kw in ((F.channel_id_for(gid), {}),
                             (GC.gc_channel_id(gid), dict(channel_id=GC.gc_channel_id(gid), ios=GC.IOS58,
                                                         access_rights=GC.TMD_ACCESS_AHB_DVD))):
                r = F.unpack_wad(F.build_wad(banner, dol, gid, **kw))
                assert r["key_index"] == 0, gid
                assert r["hashes_ok"] and r["fakesigned"] and r["title_id"] == chan
                assert r["contents"][0] == banner and r["contents"][1] == dol
            seen.add(gid)
    assert len(seen) == 312


def test_channel_builds_for_ids_that_used_to_fail(tmp_path, certs):
    for gid in ("RAAE01", "RADE01"):                                      # old key index came out 1 and 2
        ci = F.make_channel(make_wii_iso(tmp_path / f"{gid}.iso", game_id=gid), tmp_path / "out", style="classic")
        assert F.verify_wad(ci.path) == (True, "ok") and F.channel_game_id(ci.path) == gid
    assert not list((tmp_path / "out").glob("*.rewii-part"))


def test_unpack_wad_key_index_handling(certs):
    wad = bytearray(F.build_wad(fakedisc.imet_banner(), b"DOL" * 100, "RTST01"))
    tik = 0x40 + 2560
    wad[tik + F.TIK_KEY_INDEX] = 29                                       # CRAP-style junk: normal key
    assert F.unpack_wad(bytes(wad))["hashes_ok"]
    wad[tik + F.TIK_KEY_INDEX] = 1                                        # Korean key: contents don't decrypt
    assert not F.unpack_wad(bytes(wad))["hashes_ok"]


# ------------------------------------------------------------------ #4 game ID validation
@pytest.mark.parametrize("gid", [b"../../", b"/../..", b"R?P*01", b"RSP\x0001", b"rspe01", b"RSP\xff01"])
def test_invalid_game_id_is_not_a_game(tmp_path, lay, settings, gid):
    hdr = bytearray(0x60)
    hdr[:6] = gid
    hdr[0x18:0x1C] = struct.pack(">I", D.WII_MAGIC)
    with pytest.raises(D.DiscError):
        D.parse_header(bytes(hdr))
    for make in (make_gc_iso, make_wii_iso):
        iso = make(tmp_path / "evil.iso")
        with open(iso, "r+b") as f:
            f.write(gid)
        assert G.identify(iso) is None
        with pytest.raises(G.GameError):
            G.install_game(lay, iso)
        with pytest.raises(G.GameError):
            G.add_to_library(settings, iso)
        assert not G.verify_game(iso).ok
    created = [p for p in list(lay.root.rglob("*")) + list(settings.library.rglob("*")) if p.is_file()]
    assert created == []
    assert not [p for p in tmp_path.iterdir() if p.name not in ("drive", "lib", "cache", "evil.iso")]


def test_channel_refuses_invalid_game_id(tmp_path, certs):
    iso = make_wii_iso(tmp_path / "evil.iso")
    with open(iso, "r+b") as f:
        f.write(b"../../")
    with pytest.raises(F.ForwarderError):
        F.make_channel(iso, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


# ------------------------------------------------------------------ #5 untrusted sizes
def test_damaged_container_headers_are_rejected_quickly(tmp_path):
    for shift in (0, 3, 6, 14, 40, 255):
        w = tmp_path / f"s{shift}.wbfs"
        w.write_bytes(struct.pack(">4sIBB", b"WBFS", 100, 9, shift) + bytes(4096))
        t = time.time()
        assert G.identify(w) is None
        with pytest.raises(D.DiscError):
            D.open_disc(w)
        assert time.time() - t < 1
        assert convert.expected_iso_size(w) == 0
    w.write_bytes(struct.pack(">4sIBB", b"WBFS", 100, 200, 21) + bytes(4096))
    assert G.identify(w) is None
    for block in (0, 1, 0x7000, 0x18000, 0x80000000):
        c = tmp_path / "z.ciso"
        c.write_bytes(b"CISO" + struct.pack("<I", block) + b"\x01" + bytes(0x8000))
        assert G.identify(c) is None
        assert not G.verify_game(c).ok
    img = tmp_path / "part.img"
    img.write_bytes(struct.pack(">4sIBB", b"WBFS", 100, 255, 21) + bytes(4096))
    with pytest.raises(ValueError):
        W.WbfsPartition(img)


def test_huge_tmd_size_in_partition_header(wii):
    with open(wii, "r+b") as f:
        f.seek(PART_OFF + 0x2A4)
        f.write(struct.pack(">II", 0xFFFFFFF0, 0x2C0 >> 2))
    with D.open_disc(wii) as d, pytest.raises(D.DiscError):
        D.wii_partitions(d)
    assert not G.verify_game(wii).ok                                      # no MemoryError
    assert G.estimate_install_size(wii) > 0


def test_huge_file_sizes_in_file_tables(tmp_path, wii):
    gc = make_gc_iso(tmp_path / "g.iso")
    data = bytearray(gc.read_bytes())
    fst_off = struct.unpack(">I", data[0x424:0x428])[0]
    struct.pack_into(">I", data, fst_off + 12 + 8, 0xFFFFFFF0)            # opening.bnr "is 4 GB"
    gc.write_bytes(data)
    assert GC.read_gc_banner(gc) is None
    assert not G.verify_game(gc).ok
    with D.open_disc(wii) as d:
        with pytest.raises(D.DiscError, match="implausibly large"):
            D.read_wii_file(d, "data.bin", max_size=1000)
        assert len(D.read_wii_file(d, "data.bin")) == 70000


def test_damaged_file_table_raises_discerror_and_marks_whole_partition(tmp_path):
    def bad_read(off, size):
        boot = bytearray(0x440)
        struct.pack_into(">III", boot, 0x420, 0x1000, 0x2000, 0x100)      # fst: 0x100 bytes
        if off == 0:
            return bytes(boot[:size])
        if off == 0x2000:
            return (bytes(8) + struct.pack(">I", 5000) + bytes(size))[:size]   # claims 5000 entries
        return bytes(size)
    with pytest.raises(D.DiscError):
        D.parse_fst(bad_read, 0)
    # a file extent reaching outside its partition -> the partition is copied whole, not trusted
    iso = make_wii_iso(tmp_path / "w.iso")
    with D.open_disc(iso) as d:
        normal = sum(D.wii_usage_table(d))
        p = D.wii_partitions(d)[0]
    with open(iso, "r+b") as f:                                           # shrink the partition's data size
        f.seek(PART_OFF + 0x2BC)
        f.write(struct.pack(">I", 0x8000 >> 2))
    with D.open_disc(iso) as d:
        assert sum(D.wii_usage_table(d)) < normal
        assert D.wii_partitions(d)[0].data_size == 0x8000 < p.data_size


# ------------------------------------------------------------------ #6 staging files
def test_staging_files_are_hidden_and_stale_ones_removed(wii, lay):
    g = G.install_game(lay, wii)
    fresh = lay.wbfs / f"{G.TMP_PREFIX}RXXX01.wbfs"
    stale = lay.gc_games / "Some Game [GTST01]" / f"{G.TMP_PREFIX}game.iso"
    stale.parent.mkdir()
    make_gc_iso(stale)                                                    # a valid-looking partial copy
    make_gc_iso(fresh)
    stale_part = lay.wbfs / f"{G.TMP_PREFIX}RYYY01.wbf1"
    stale_part.write_bytes(b"x")
    old = time.time() - G.STALE_TMP_AGE - 60
    os.utime(stale, (old, old))
    os.utime(stale_part, (old, old))
    assert [x.game_id for x in G.scan_drive(lay)] == [g.game_id]
    assert fresh.exists() and not stale.exists() and not stale_part.exists()
    assert G.normalize_drive(lay) == [] and fresh.exists()               # never "fixed" into place


def test_failed_install_leaves_no_staging_files(wii, lay, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("drive unplugged")
    monkeypatch.setattr(G, "_replace_files", boom)
    with pytest.raises(OSError):
        G.install_game(lay, wii)
    assert [p for p in lay.wbfs.rglob("*") if p.is_file()] == []
    monkeypatch.undo()
    gc = make_gc_iso(tmp_path / "gc.iso")
    tok = CancelToken()
    with pytest.raises(Cancelled):
        G.install_game(lay, gc, progress=lambda e: tok.cancel(), cancel=tok)
    assert [p for p in lay.gc_games.rglob("*") if p.is_file()] == []


def test_split_writer_close_closes_every_file_and_abort_cleans_up(tmp_path, monkeypatch):
    w = W.SplitWriter(tmp_path / "a.wbfs", 10)
    w.write_at(0, b"x" * 25)
    calls = []

    def bad_fsync(fd):
        calls.append(fd)
        raise OSError(5, "I/O error")
    monkeypatch.setattr(W.os, "fsync", bad_fsync)
    with pytest.raises(OSError):
        w.close()
    assert len(calls) == 3 and all(f.closed for f in w.files)
    w.abort()
    assert not list(tmp_path.glob('a.*'))


def test_partition_game_reader_does_not_close_the_partition(wii, tmp_path):
    with D.open_disc(wii) as d:
        wf = W.write_wbfs(d, tmp_path / "one.wbfs")[0]
    data = bytearray(wf.read_bytes())
    data[12] = 1
    data[13] = 1                                                          # slot 1: used but unreadable
    img = tmp_path / "part.img"
    img.write_bytes(data)
    p = W.WbfsPartition(img)
    with p.open_game(0) as r:
        assert r.read(0, 6) == b"RTST01"
    assert [g.game_id for g in p.games()] == ["RTST01"]                   # still open, bad slot skipped
    assert [slot for slot, _why in p.skipped] == [1]
    p.close()


# ------------------------------------------------------------------ #7 work folder
def test_conversions_are_staged_in_the_cache_work_folder(wii, lay, tmp_path, monkeypatch):
    work = tmp_path / "cache" / "work"
    seen = {}

    def fake_to_iso(src, dest, progress=None, cancel=None):
        seen["dest"] = dest
        dest.write_bytes(b"")                                             # stand-in output
        return wii                                                        # hand back a real image
    rvz = tmp_path / "game.rvz"
    rvz.write_bytes(b"RVZ\x01" + bytes(0x20) + struct.pack(">Q", 1 << 20) + bytes(100))
    monkeypatch.setattr(convert, "to_iso", fake_to_iso)
    g = G.install_game(lay, rvz)
    assert g.game_id == "RTST01"
    assert work in seen["dest"].parents
    assert list(work.iterdir()) == []                                     # cleaned up afterwards

    # not enough room for the temporary ISO -> refused before any work is done
    seen.clear()
    monkeypatch.setattr(G, "free_space", lambda p: 1000)
    with pytest.raises(G.GameError, match="temporary space"):
        G.install_game(lay, rvz, overwrite=True)
    assert not seen and list(work.iterdir()) == []


def test_work_folder_is_cleaned_after_a_failure_and_stale_jobs_are_purged(tmp_path, lay, monkeypatch):
    work = tmp_path / "cache" / "work"
    stale = work / "job-old"
    stale.mkdir(parents=True)
    (stale / "joined.iso").write_bytes(b"x")
    old = time.time() - G.STALE_TMP_AGE - 60
    os.utime(stale, (old, old))
    rvz = tmp_path / "game.rvz"
    rvz.write_bytes(b"RVZ\x01" + bytes(200))

    def failing(src, dest, progress=None, cancel=None):
        dest.write_bytes(b"partial")
        raise convert.ConvertError("The converter failed")
    monkeypatch.setattr(convert, "to_iso", failing)
    with pytest.raises(G.GameError, match="converter failed"):
        G.install_game(lay, rvz)
    assert list(work.iterdir()) == []


# ------------------------------------------------------------------ #8 redump DAT is optional
def _split_dump(folder, data, base="RTST01", parts=2):
    size = -(-len(data) // parts)
    for i in range(parts):
        (folder / f"{base}.part{i}.iso").write_bytes(data[i * size:(i + 1) * size])


def test_broken_dat_does_not_fail_a_join(tmp_path):
    hdr = bytearray(0x400)
    hdr[:6] = b"RSPE01"
    hdr[0x18:0x1C] = struct.pack(">I", D.WII_MAGIC)
    data = bytes(hdr) + bytes(range(256)) * 400
    _split_dump(tmp_path, data, "RSPE01")
    (tmp_path / "wii.dat").write_text("<datafile><game name='x'><rom size='lots'")      # not even XML
    res = C.join_dump(C.find_dumps(tmp_path)[0], tmp_path / "o.iso")
    assert res.ok and (tmp_path / "o.iso").read_bytes() == data
    assert not res.dat_checked and res.dat_error
    (tmp_path / "wii.dat").write_text('<datafile><game name="G"><rom name="r" size="lots" md5="00"/></game></datafile>')
    res = C.verify_dump(C.find_dumps(tmp_path)[0])
    assert res.ok and res.dat_checked and res.redump_name is None


def test_install_from_split_dump_ignores_the_dat(tmp_path, lay, monkeypatch):
    gc = make_gc_iso(tmp_path / "gc.iso")
    data = gc.read_bytes()
    cr = tmp_path / "cr"
    cr.mkdir()
    _split_dump(cr, data, "GTST01")
    (cr / "gc.dat").write_text("this is not a DAT file")
    monkeypatch.setattr(C, "VALID_DISC_SIZES", (len(data),))              # a tiny "whole disc" for the test
    monkeypatch.setattr(C, "load_dat", lambda p: pytest.fail("the DAT must not be read during an install"))
    g = G.install_game(lay, cr / "GTST01.part1.iso")                      # any part selects the dump
    assert g.path.read_bytes() == data


# ------------------------------------------------------------------ #9 / #13 / #14 / #15
def test_normalize_does_not_split_a_game_across_folders(wii, lay):
    with D.open_disc(wii) as d:
        W.write_wbfs(d, lay.wbfs / "loose.wbfs", split=3 << 20)
    dest = lay.wbfs / "TEST GAME [RTST01]"
    dest.mkdir()
    (dest / "RTST01.wbf1").write_bytes(b"stale part of an older copy")
    msgs = G.normalize_drive(lay)
    assert len(msgs) == 1 and msgs[0].startswith("skipped")
    assert (lay.wbfs / "loose.wbfs").exists() and (lay.wbfs / "loose.wbf1").exists()
    assert (dest / "RTST01.wbf1").read_bytes() == b"stale part of an older copy"
    assert G.verify_game(lay.wbfs / "loose.wbfs").ok


def test_install_uses_a_given_scan_and_migration_skips_what_is_there(wii, lay, settings, tmp_path, monkeypatch):
    with D.open_disc(wii) as d:
        wf = W.write_wbfs(d, tmp_path / "one.wbfs")[0]
    data = bytearray(wf.read_bytes())
    data[12] = 1
    img = tmp_path / "part.img"
    img.write_bytes(data)
    listed = G.scan_drive(lay)
    monkeypatch.setattr(G, "scan_drive", lambda *a, **k: pytest.fail("the drive was rescanned"))
    g = G.install_game(lay, wii, title="My Own Name", games=listed)
    # same game under another name is already there -> not copied a second time
    assert G.migrate_wbfs_partition(img, lay, None, games=[g]) == []
    assert len(list(lay.wbfs.iterdir())) == 1
    monkeypatch.setattr(G, "free_space", lambda p: 1000)
    msgs = G.migrate_wbfs_partition(img, None, settings)
    assert len(msgs) == 1 and "not enough free space" in msgs[0]
    assert not list(settings.wii_games_dir.iterdir())


def test_to_iso_refuses_to_replace_its_source_and_keeps_bystanders(wii, tmp_path):
    with D.open_disc(wii) as d:
        src = W.write_wbfs(d, tmp_path / "game.wbfs")[0]
    with pytest.raises(convert.ConvertError):
        convert.to_iso(src, src)
    assert D.sniff_format(src) == "WBFS"
    bystander = tmp_path / "out.part.iso"                                # the old staging name
    bystander.write_bytes(b"mine")
    bad = tmp_path / "bad.wbfs"
    bad.write_bytes(struct.pack(">4sIBB", b"WBFS", 100, 9, 3) + bytes(4096))
    with pytest.raises(convert.ConvertError):
        convert.to_iso(bad, tmp_path / "out.iso")
    assert bystander.read_bytes() == b"mine" and not (tmp_path / "out.iso").exists()


def test_cached_tool_is_only_used_if_unchanged(tmp_path, monkeypatch):
    tool = tmp_path / "nodtool-x"
    tool.write_bytes(b"\x7fELF real tool")
    monkeypatch.setattr(convert, "cached_tool_path", lambda: tool)
    monkeypatch.setattr(convert.shutil, "which", lambda name: None)
    assert convert.find_tool("RVZ") is None                              # no recorded digest
    import hashlib
    convert._digest_file(tool).write_text(hashlib.sha256(tool.read_bytes()).hexdigest())
    convert._verified.clear()
    assert convert.find_tool("RVZ").path == tool
    tool.write_bytes(b"\x7fELF something else entirely")
    assert convert.find_tool("RVZ") is None and not convert.tool_available()


def test_channel_id_collisions():
    assert CH.channel_id_collisions(["RMCE01", "RSPE01", "GALE01"]) == {}
    assert CH.channel_id_collisions(["RMCE01", "SMCE01", "RSPE01", "RMCE01"]) == {"UMCE": ["RMCE01", "SMCE01"]}
    got = CH.channel_id_collisions([("GALE01", "gc"), ("DALE01", "gc"), ("RALE01", "wii")])
    assert got == {"GALE": ["GALE01", "DALE01"]}
    assert CH.channel_problem("gc", 0, "GTST01", True) is None
