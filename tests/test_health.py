"""Health check + fixes on a synthetic drive folder."""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from rewii.core.disc import GC_MAGIC, WII_DL_SECTORS, WII_MAGIC, WII_SECTOR
from rewii.core.drives import Drive, Volume
from rewii.core.health import (ERROR, INFO, WARN, Finding, FixError, check, check_drive, fix,
                               split_file)
from rewii.core.layout import DriveLayout
from rewii.core.progress import Cancelled, CancelToken

GiB = 1024 ** 3


def disc_header(gid: str, title: str, wii: bool, disc: int = 0) -> bytes:
    h = bytearray(0x400)
    h[0:6] = gid.encode()
    h[6] = disc
    struct.pack_into(">II", h, 0x18, WII_MAGIC if wii else 0, 0 if wii else GC_MAGIC)
    h[0x20:0x20 + len(title)] = title.encode()
    return bytes(h)


def make_iso(p: Path, gid: str, title: str, wii: bool = True, disc: int = 0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(disc_header(gid, title, wii, disc))
    return p


def make_wbfs(p: Path, gid: str, title: str) -> Path:
    """Minimal WBFS file: header, disc info and one 2 MiB block holding the disc header."""
    p.parent.mkdir(parents=True, exist_ok=True)
    blk = 1 << 21
    n_disc = WII_DL_SECTORS * WII_SECTOR // blk
    hdr = disc_header(gid, title, True)
    head = struct.pack(">4sIBBBB", b"WBFS", 2 * blk // 512, 9, 21, 0, 0).ljust(512, b"\0")
    info = hdr[:0x100] + struct.pack(">H", 1) + bytes((n_disc - 1) * 2)
    with open(p, "wb") as f:
        f.write(head + info)
        f.seek(blk)
        f.write(hdr)
        f.truncate(2 * blk)
    return p


def app(layout: DriveLayout, *parts: str, boot: bool = True) -> Path:
    d = layout.apps.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    if boot:
        (d / "boot.dol").write_bytes(b"dol")
    return d


@pytest.fixture
def layout(tmp_path) -> DriveLayout:
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    lay.ensure_dirs()
    for name in ("usbloader_gx", "nintendont", "yawmme", "savegame_manager_gx", "wiixplorer"):
        app(lay, name)
    return lay


def ids(findings: list[Finding]) -> list[str]:
    return sorted(f.id for f in findings)


def good_drive(**kw) -> Drive:
    base = dict(id="x", device="/dev/sdb", size=500_000_000_000, model="WD Elements", bus="USB",
                removable=True, is_usb=True, partition_style="MBR",
                volumes=[Volume("/dev/sdb1", 500_000_000_000, "FAT32", "REWII", ["/m"],
                                free=200 * GiB, cluster_size=32768)])
    base.update(kw)
    return Drive(**base)


# --------------------------------------------------------------------------- drive checks
def test_clean_drive_has_no_findings(layout):
    make_wbfs(layout.wii_game_file("Mario Kart Wii", "RMCE01"), "RMCE01", "MARIO KART WII")
    make_iso(layout.gc_game_file("Melee", "GALE01"), "GALE01", "Melee", wii=False)
    make_iso(layout.gc_game_file("Melee", "GALE01", disc=1), "GALE01", "Melee", wii=False, disc=1)
    assert check(layout, good_drive()) == []


def test_drive_level_checks():
    vol = lambda fs, cs=32768, free=200 * GiB: [Volume("/dev/sdb1", 1, fs, cluster_size=cs, free=free)]
    assert ids(check_drive(good_drive(volumes=vol("NTFS")))) == ["fs_ntfs"]
    assert check_drive(good_drive(volumes=vol("NTFS")))[0].severity == WARN
    f = check_drive(good_drive(volumes=vol("exFAT")))
    assert ids(f) == ["fs_exfat"] and f[0].severity == ERROR and f[0].fix_id == "format_drive"
    assert ids(check_drive(good_drive(volumes=vol("WBFS")))) == ["fs_wbfs"]
    assert ids(check_drive(good_drive(volumes=[]))) == ["fs_other"]
    assert ids(check_drive(good_drive(partition_style="GPT"))) == ["gpt"]
    assert ids(check_drive(good_drive(volumes=vol("FAT32", 4096)))) == ["cluster_size"]
    assert ids(check_drive(good_drive(size=4_000_000_000_000))) == ["too_big"]
    flash = check_drive(good_drive(model="SanDisk Cruzer Blade"))
    assert ids(flash) == ["flash_stick"] and flash[0].severity == INFO
    low = check_drive(good_drive(volumes=vol("FAT32", free=100 * 1024 ** 2)))
    assert ids(low) == ["low_space"] and low[0].severity == WARN


# --------------------------------------------------------------------------- apps
def test_nested_apps_and_fix(layout):
    app(layout, "apps", "wiimc")
    app(layout, "apps", "usbloader_gx")          # conflicts with existing one: kept
    found = [f for f in check(layout) if f.id == "nested_apps"]
    assert len(found) == 1 and found[0].fix_id == "flatten_nested_apps"
    log = fix(layout, found[0])
    assert (layout.apps / "wiimc" / "boot.dol").exists()
    assert any("Kept" in line for line in log)
    # the conflicting copy stays in apps/apps, so the finding remains until the user decides
    assert (layout.apps / "apps" / "usbloader_gx" / "boot.dol").exists()
    assert not (layout.apps / "apps" / "wiimc").exists()


def test_app_inside_app_apps_folder(layout):
    app(layout, "homebrew_browser", boot=False)
    app(layout, "homebrew_browser", "apps", "homebrew_browser")
    f = next(f for f in check(layout) if f.id == "nested_apps")
    fix(layout, f)
    assert (layout.apps / "homebrew_browser" / "boot.dol").exists()
    assert not (layout.apps / "homebrew_browser" / "apps").exists()
    assert "nested_apps" not in ids(check(layout))


def test_double_folder_and_missing_boot(layout):
    app(layout, "wiimc", boot=False)
    app(layout, "wiimc", "wiimc")
    app(layout, "broken", boot=False)
    (layout.apps / "broken" / "meta.xml").write_text("x")
    found = {f.id: f for f in check(layout)}
    assert found["app_no_boot"].path.name == "broken" and found["app_no_boot"].fix_id is None
    fix(layout, found["app_double_folder"])
    assert (layout.apps / "wiimc" / "boot.dol").exists()
    assert not (layout.apps / "wiimc" / "wiimc").exists()
    with pytest.raises(FixError):
        fix(layout, found["app_no_boot"])


def test_missing_starter_apps(tmp_path):
    lay = DriveLayout(tmp_path)
    lay.ensure_dirs()
    app(lay, "usbloader_gx")
    f = next(f for f in check(lay) if f.id == "missing_apps")
    assert f.severity == WARN and "nintendont" in f.data["missing"]
    assert f.fix_id == "install_starter_apps" and not f.auto_fixable
    app(lay, "Nintendont")                        # case-insensitive
    f = next(f for f in check(lay) if f.id == "missing_apps")
    assert f.severity == INFO


# --------------------------------------------------------------------------- Wii games
def test_wii_flat_game_fix(layout):
    src = make_wbfs(layout.wbfs / "mario kart.wbfs", "RMCE01", "MARIO KART WII")
    (layout.wbfs / "mario kart.wbf1").write_bytes(b"part2")
    f = next(f for f in check(layout) if f.id == "wii_flat")
    assert f.fix_id == "move_wii_game" and f.data["game_id"] == "RMCE01"
    fix(layout, f)
    dest = layout.wbfs / "MARIO KART WII [RMCE01]"
    assert (dest / "RMCE01.wbfs").exists() and (dest / "RMCE01.wbf1").read_bytes() == b"part2"
    assert not src.exists()
    assert check(layout) == []


def test_wii_wrong_folder_and_file_names(layout):
    make_iso(layout.wbfs / "Zelda" / "game.iso", "RZDE01", "Twilight Princess")
    keep = layout.wbfs / "My Title [SB4E01]"
    make_wbfs(keep / "galaxy2.wbfs", "SB4E01", "SUPER MARIO GALAXY 2")
    found = {f.id: f for f in check(layout)}
    assert set(found) == {"wii_folder_name", "wii_file_name"}
    for f in found.values():
        fix(layout, f)
    assert (layout.wbfs / "Twilight Princess [RZDE01]" / "RZDE01.iso").exists()
    assert not (layout.wbfs / "Zelda").exists()
    assert (keep / "SB4E01.wbfs").exists()        # user's folder title kept
    assert check(layout) == []


def test_fix_refuses_to_overwrite(layout):
    make_wbfs(layout.wii_game_file("MKW", "RMCE01"), "RMCE01", "MKW")
    make_wbfs(layout.wbfs / "dupe.wbfs", "RMCE01", "MKW")
    f = next(f for f in check(layout) if f.id == "wii_flat")
    # destination derived from the header title is "MKW [RMCE01]/RMCE01.wbfs", which exists
    with pytest.raises(FixError):
        fix(layout, f)
    assert (layout.wbfs / "dupe.wbfs").exists()


def test_orphan_split_and_conversion_formats(layout):
    d = layout.wbfs / "Game [RMCE01]"
    d.mkdir()
    (d / "RMCE01.wbf1").write_bytes(b"x")
    (layout.wbfs / "Brawl.rvz").write_bytes(b"RVZ\x01" + bytes(100))
    (layout.gc_games / "Melee.nkit.iso").write_bytes(bytes(100))
    (layout.wbfs / "games.zip").write_bytes(b"PK")
    found = check(layout)
    assert ids(found) == ["archive_on_drive", "needs_conversion", "needs_conversion", "orphan_split"]
    conv = [f for f in found if f.id == "needs_conversion"]
    assert {f.data["format"] for f in conv} == {"RVZ", "NKit"}
    assert all(f.fix_id == "convert_game" for f in conv)


def test_big_file_on_fat32_target(layout):
    p = make_wbfs(layout.wii_game_file("Brawl", "RSBE01"), "RSBE01", "BRAWL")
    os.truncate(p, 5 * GiB)                       # sparse
    f = next(f for f in check(layout) if f.id == "file_too_big")
    assert f.fix_id == "split_file" and f.severity == ERROR
    # on an NTFS drive big files are fine
    ntfs = good_drive(volumes=[Volume("/dev/sdb1", 1, "NTFS", free=100 * GiB)])
    assert "file_too_big" not in ids(check(layout, ntfs))


def test_gc_in_wbfs_and_wii_in_games(layout):
    make_iso(layout.wbfs / "melee.iso", "GALE01", "Melee", wii=False)
    make_iso(layout.gc_games / "Mario Kart Wii [RMCE01]" / "game.iso", "RMCE01", "MKW", wii=True)
    found = {f.id: f for f in check(layout)}
    assert set(found) == {"gc_in_wbfs", "wii_in_games"}
    fix(layout, found["gc_in_wbfs"])
    fix(layout, found["wii_in_games"])
    assert (layout.gc_games / "Melee [GALE01]" / "game.iso").exists()
    assert (layout.wbfs / "MKW [RMCE01]" / "RMCE01.iso").exists()
    assert check(layout) == []


# --------------------------------------------------------------------------- GC games
def test_gc_flat_and_disc2(layout):
    make_iso(layout.gc_games / "melee.iso", "GALE01", "Super Smash Bros. Melee", wii=False)
    make_iso(layout.gc_games / "rs2.iso", "GLME01", "Luigis Mansion", wii=False)
    make_iso(layout.gc_games / "Metal Gear [GGSEA4]" / "disc 2.iso", "GGSEA4", "MGS", wii=False, disc=1)
    make_iso(layout.gc_games / "Metal Gear [GGSEA4]" / "game.iso", "GGSEA4", "MGS", wii=False)
    make_iso(layout.gc_games / "Tales [GTOJAF]" / "disc2.iso", "GTOJAF", "Tales", wii=False, disc=1)
    make_iso(layout.gc_games / "Tales [GTOJAF]" / "Tales.iso", "GTOJAF", "Tales", wii=False)
    found = check(layout)
    assert ids(found) == ["gc_disc2_name", "gc_file_name", "gc_flat", "gc_flat"]
    assert next(f for f in found if f.id == "gc_flat").severity == INFO
    for f in found:
        fix(layout, f)
    assert (layout.gc_games / "Super Smash Bros. Melee [GALE01]" / "game.iso").exists()
    assert (layout.gc_games / "Metal Gear [GGSEA4]" / "disc2.iso").exists()
    assert (layout.gc_games / "Tales [GTOJAF]" / "game.iso").exists()
    assert check(layout) == []


def test_gc_disc2_joins_existing_folder(layout):
    make_iso(layout.gc_games / "Resident Evil 4 [G4BE08]" / "game.iso", "G4BE08", "RE4", wii=False)
    make_iso(layout.gc_games / "re4 disc2.iso", "G4BE08", "RE4", wii=False, disc=1)
    f = next(f for f in check(layout) if f.id == "gc_flat")
    fix(layout, f)
    assert (layout.gc_games / "Resident Evil 4 [G4BE08]" / "disc2.iso").exists()


# --------------------------------------------------------------------------- mac junk
def test_mac_junk(layout):
    (layout.root / ".Spotlight-V100" / "Store").mkdir(parents=True)
    (layout.root / ".Spotlight-V100" / "Store" / "x").write_bytes(b"x")
    (layout.root / ".Trashes").mkdir()
    (layout.root / ".fseventsd").mkdir()
    (layout.apps / "usbloader_gx" / "._boot.dol").write_bytes(b"x")
    (layout.wbfs / "._junk.wbfs").write_bytes(b"x")
    (layout.root / ".DS_Store").write_bytes(b"x")
    f = next(f for f in check(layout) if f.id == "mac_junk")
    assert len(f.data["paths"]) == 5 and f.auto_fixable
    events = []
    fix(layout, f, progress=events.append)
    assert events[-1].done == 5
    assert not (layout.root / ".Spotlight-V100").exists()
    assert (layout.root / ".Trashes").exists()          # the Mac trash is the user's data
    assert (layout.apps / "usbloader_gx" / "boot.dol").exists()
    assert check(layout) == []


# --------------------------------------------------------------------------- split
def test_split_file(tmp_path):
    p = tmp_path / "RMCE01.wbfs"
    data = os.urandom(5000)
    p.write_bytes(data)
    parts = split_file(p, part_size=2048)
    assert [x.name for x in parts] == ["RMCE01.wbfs", "RMCE01.wbf1", "RMCE01.wbf2"]
    assert [x.stat().st_size for x in parts] == [2048, 2048, 904]
    assert b"".join(x.read_bytes() for x in parts) == data
    with pytest.raises(ValueError):
        split_file(tmp_path / "x.iso")


def test_split_file_cancel_rolls_back(tmp_path):
    p = tmp_path / "RMCE01.wbfs"
    data = os.urandom(10 * 1024)
    p.write_bytes(data)
    tok = CancelToken()
    seen = []

    def progress(ev):
        seen.append(ev.done)
        if len(seen) == 3:                        # cancel during the second part
            tok.cancel()

    with pytest.raises(Cancelled):
        split_file(p, part_size=2048, progress=progress, cancel=tok)
    assert p.read_bytes() == data
    assert sorted(x.name for x in tmp_path.iterdir()) == ["RMCE01.wbfs"]


def test_split_fix_via_finding(layout, monkeypatch):
    import rewii.core.health as h
    p = make_wbfs(layout.wii_game_file("G", "RMCE01"), "RMCE01", "G")
    f = Finding("file_too_big", ERROR, "", "split_file", p)
    orig = h.split_file
    monkeypatch.setattr(h, "split_file", lambda path, **kw: orig(path, part_size=1 << 20, **kw))
    fix(layout, f)
    assert p.stat().st_size == 1 << 20 and p.with_suffix(".wbf1").exists()
