"""Regression tests for the problems found by using ReWii on a real FAT32 drive.

Unit tests run on a case-sensitive filesystem and can't mount FAT32, so where a
test needs FAT32's behaviour it either checks the code path that FAT32 requires
(e.g. the temporary-name hop for case-only renames) or fakes the OS answer
(``errno``, read-only, "these two names are the same file").
"""
from __future__ import annotations

import errno
import os
import time
import zipfile
from pathlib import Path

import httpx
import pytest

import fakedisc
from fakedisc import make_gc_iso, make_wii_iso
from rewii import __main__ as entry
from rewii import cli
from rewii.core import backup as B
from rewii.core import disc as D
from rewii.core import fileops as FO
from rewii.core import games as G
from rewii.core import health as H
from rewii.core import net, osc
from rewii.core import riivolution as R
from rewii.core import sysfiles as S
from rewii.core import wbfs as W
from rewii.core.config import Settings
from rewii.core.drives import FAT32, Drive, Volume
from rewii.core.layout import DriveLayout

OLD = time.time() - 3 * 3600          # "nothing has written to it for hours"


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    from rewii.core import config
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path / "cache")
    yield
    net.set_client(None)


@pytest.fixture
def lay(tmp_path) -> DriveLayout:
    l = DriveLayout(tmp_path / "drive")
    l.root.mkdir()
    l.ensure_dirs()
    return l


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(library_dir=str(tmp_path / "lib"))
    s.ensure_library()
    return s


def aged(p: Path, data: bytes = b"x" * 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (OLD, OLD))
    return p


def oserr(code: int, path: str = "") -> OSError:
    return OSError(code, os.strerror(code), path) if path else OSError(code, os.strerror(code))


# ------------------------------------------------------------------ 1: temp files are never games
def test_temp_names():
    for n in (".rewii-tmp-game.iso", ".rewii-tmp-RSPE01.wbf1", "data.bin.rewii-part", "cover.png.part",
              "RSPE01.wbf2.partial", "data.bin.rewii-new", "X.REWII-PART"):
        assert FO.is_temp_name(n), n
    for n in ("game.iso", "RSPE01.wbfs", "RSPE01.wbf1", "party.iso", "game.part0.iso", "departed.txt"):
        assert not FO.is_temp_name(n), n


def test_health_never_promotes_an_interrupted_copy_to_a_game(lay):
    """An unplugged drive leaves ``.rewii-tmp-game.iso`` (header first, so it reads as a
    game). The health fix used to rename it to game.iso - a cut-off game listed as fine."""
    folder = lay.gc_games / "Kirby Air Ride [GKYE01]"
    full = make_gc_iso(lay.root / "full.iso", "GKYE01", "Kirby Air Ride")
    part = aged(folder / ".rewii-tmp-game.iso", full.read_bytes()[:0x2000])
    full.unlink()
    wii = aged(lay.wbfs / ".rewii-tmp-RSPE01.wbfs", make_wii_iso(lay.root / "w.iso", "RSPE01").read_bytes()[:0x60000])
    (lay.root / "w.iso").unlink()
    aged(lay.wbfs / "Wii Play [RHAE01]" / "RHAE01.wbf1.partial")
    aged(lay.apps / "usbloader_gx" / "images" / "RHAE01.png.part")
    fs = H.check(lay)
    assert [f.id for f in fs if f.fix_id in ("move_gc_game", "move_wii_game")] == []
    assert not [f for f in fs if f.id == "unreadable_game"]
    left = [f for f in fs if f.id == "temp_leftovers"]
    assert len(left) == 1 and left[0].auto_fixable and "4 unfinished" in left[0].message
    assert G.scan_drive(lay) == []
    log = H.fix(lay, left[0])
    assert len(log) == 4 and not part.exists() and not wii.exists()
    assert not folder.exists()                      # the now-empty game folder goes too
    assert not (lay.gc_games / "Kirby Air Ride [GKYE01]" / "game.iso").exists()
    assert [f.id for f in H.check(lay) if f.id == "temp_leftovers"] == []


def test_temp_cleanup_leaves_running_copies_and_foreign_files_alone(lay):
    fresh = lay.wbfs / ".rewii-tmp-RMCE01.wbfs"             # being written right now
    fresh.write_bytes(b"x")
    old = aged(lay.wbfs / ".rewii-tmp-RSPE01.wbfs")
    mine = aged(lay.root / "Movies" / "holiday.avi.part")     # not ReWii's folder: maybe a download
    old_save = aged(lay.private_saves / "RSBE" / "data.bin.rewii-old")   # the previous save: keep
    f = next(x for x in H.check(lay) if x.id == "temp_leftovers")
    assert "1 of them changed less than an hour ago" in f.message
    assert f.data["paths"] == [str(old)]
    # even a finding that (wrongly) names other files only ever deletes idle ReWii temp files
    f.data["paths"] = [str(fresh), str(mine), str(lay.root / "apps")]
    H.fix(lay, f)
    assert fresh.exists() and mine.exists() and old_save.exists() and not old.exists()
    only_fresh = next(x for x in H.check(lay) if x.id == "temp_leftovers")
    assert not only_fresh.auto_fixable and only_fresh.fix_id is None


def test_backups_skip_temp_files(lay, settings, tmp_path):
    aged(lay.wad / "a.wad.part")
    aged(lay.gc_games / "X [GTST01]" / ".rewii-tmp-game.iso")
    (lay.wad / "real.wad").write_bytes(b"w")
    assert [e.path for e in B.scan_tree(lay.root)] == ["wad/real.wad"]


def test_drive_unplugged_mid_copy_says_so(lay, tmp_path, monkeypatch):
    src = make_gc_iso(tmp_path / "g.iso", "GTST01", "Game")

    def yank(s, dest, **kw):
        import shutil
        shutil.rmtree(lay.root)
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(dest))
    monkeypatch.setattr(G, "copy_file", yank)
    with pytest.raises(FO.DriveNotConnected) as ei:
        G.install_game(lay, src)
    assert "isn't connected" in str(ei.value) and ".rewii-tmp" not in str(ei.value)
    assert not lay.root.exists()


# ------------------------------------------------------------------ 2: never re-create the drive
def test_nothing_recreates_a_vanished_drive(tmp_path, settings, monkeypatch):
    gone = DriveLayout(tmp_path / "gone")
    src = make_gc_iso(tmp_path / "g.iso", "GTST01", "Game")
    wad = tmp_path / "x.wad"
    wad.write_bytes(b"w")
    snap_src = tmp_path / "src"
    (snap_src / "apps").mkdir(parents=True)
    (snap_src / "apps" / "a.txt").write_text("a")
    snap = B.create_snapshot(snap_src, settings, "t")
    from rewii.core import cheats, covers, online, saves, themes, wad as WD
    calls = [
        lambda: gone.ensure_dirs(),
        lambda: gone.write_manifest({"a": 1}),
        lambda: G.install_game(gone, src),
        lambda: covers.fetch_covers(gone, ["RSPE01"], None, settings),
        lambda: cheats.fetch_cheats(gone, "RSPE01"),
        lambda: cheats.build_gct(gone, "RSPE01", []),
        lambda: WD.stage_wads(gone, [wad], settings=settings),
        lambda: osc.install_starter_kit(gone, "wii", apps=[]),
        lambda: R.install_mod(tmp_path / "nomod.zip", gone.root),
        lambda: S.stage_hackmii(gone),
        lambda: online.write_gx_config(gone, {"a": "b"}),
        lambda: B.restore_snapshot(snap, gone.root),
        lambda: B.clone_drive(snap_src, gone.root),
        lambda: themes.stage_theme(tmp_path / "t.csm", gone.root, console_type="wii", region="NTSC-U"),
        lambda: saves.restore_save(None, gone.root, "sgm_gx", settings),     # type: ignore[arg-type]
        lambda: H.fix(gone, H.Finding("mac_junk", H.INFO, "", "delete_mac_junk", gone.root)),
    ]
    monkeypatch.setattr(themes, "identify_theme", lambda p: themes.ThemeInfo(path=p, is_u8=True, is_theme=True))
    monkeypatch.setattr(themes, "check_theme", lambda *a, **k: [])
    for i, call in enumerate(calls):
        try:
            call()
        except FO.DriveNotConnected as e:
            assert "isn't connected" in str(e)
        except (R.ModError, AttributeError):    # refused for another reason before touching the drive
            pass
        assert not gone.root.exists(), f"call {i} re-created the drive"


def test_cli_and_explain_report_a_missing_drive(capsys, tmp_path, monkeypatch):
    s = Settings(library_dir=str(tmp_path / "lib"), last_drive=str(tmp_path / "gone"))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: s))
    for argv in (["health"], ["games", "list"], ["games", "fix-layout"], ["health", "--drive", str(tmp_path / "gone")]):
        rc = entry.main(argv)
        err = capsys.readouterr().err
        assert rc == 2 and "isn't there" in err and not (tmp_path / "gone").exists()


# ------------------------------------------------------------------ 3: channel --out is atomic
def test_channel_out_never_leaves_a_cut_off_wad(capsys, tmp_path, local_patch):
    from rewii.core import channels
    from rewii.core.forwarder import ChannelInfo
    s = Settings(library_dir=str(tmp_path / "lib"))
    local_patch.setattr(Settings, "load", classmethod(lambda cls, path=None: s))
    iso = make_wii_iso(tmp_path / "RAAE01.iso", "RAAE01", "Alpha")
    made = tmp_path / "lib" / "Channels" / "Alpha - UAAE - RAAE01.wad"

    def fake_build(path, settings, **kw):
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"N" * 5000)
        return ChannelInfo("RAAE01", "Alpha", "UAAE", "black", made, 5000)
    local_patch.setattr(channels, "build_channel", fake_build)
    out = tmp_path / "usb" / "wad"
    out.mkdir(parents=True)
    good = out / made.name
    good.write_bytes(b"the good old wad")
    real_open = open

    class Full:
        def __init__(self, f):
            self.f = f

        def write(self, b):
            self.f.write(b[:100])                        # some bytes land, then the drive is full
            raise oserr(errno.ENOSPC)

        def __getattr__(self, n):
            return getattr(self.f, n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

    def fake_open(p, mode="r", *a, **k):
        f = real_open(p, mode, *a, **k)
        return Full(f) if "w" in mode and str(p).startswith(str(out)) else f
    local_patch.setattr("builtins.open", fake_open)
    rc = entry.main(["channel", str(iso), "--style", "black", "--out", str(out)])
    local_patch.undo()
    err = capsys.readouterr().err
    assert rc == 1 and "full" in err and "Errno" not in err and "rewii-part" not in err
    assert good.read_bytes() == b"the good old wad"         # not deleted first, not half-replaced
    assert [p.name for p in out.iterdir()] == [made.name]   # no temp file left either


# ------------------------------------------------------------------ 4/5: renames on FAT32
def test_rename_path_hops_through_a_temp_name_for_case_only_changes(tmp_path, monkeypatch):
    a = tmp_path / "game.ISO"
    a.write_text("x")
    seen: list[tuple[str, str]] = []
    real = os.rename

    def spy(s, d, *args, **kw):
        seen.append((Path(s).name, Path(d).name))
        return real(s, d, *args, **kw)
    monkeypatch.setattr(os, "rename", spy)
    FO.rename_path(a, tmp_path / "game.iso")
    # a direct rename is a silent no-op on Linux vfat: it must go via another name
    assert len(seen) == 2 and seen[0][0] == "game.ISO" and seen[1][1] == "game.iso"
    assert seen[0][1] == seen[1][0] and seen[0][1].lower() != "game.iso"
    assert not FO.is_temp_name(seen[0][1])                  # a crash mid-way must not hide the game
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["game.iso"]
    # folders too
    d = tmp_path / "Wii Sports [RSPE01]"
    d.mkdir()
    FO.rename_path(d, tmp_path / "WII SPORTS [RSPE01]")
    # compare the listing, not .exists(): on a case-insensitive filesystem both spellings "exist"
    assert "WII SPORTS [RSPE01]" in [p.name for p in tmp_path.iterdir()]
    assert "Wii Sports [RSPE01]" not in [p.name for p in tmp_path.iterdir()]


def test_rename_path_never_replaces_another_file_and_rolls_back(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    with pytest.raises(FileExistsError):
        FO.rename_path(a, b)
    assert a.read_text() == "a" and b.read_text() == "b"
    FO.rename_path(a, a)                                    # nothing to do
    (tmp_path / "sub").mkdir()
    FO.rename_path(a, tmp_path / "sub" / "c.txt")
    assert (tmp_path / "sub" / "c.txt").read_text() == "a"
    # second half of a case-only hop fails -> the original name comes back
    x = tmp_path / "x.BIN"
    x.write_text("x")
    real = os.rename
    n = {"i": 0}

    def flaky(s, d, *args, **kw):
        n["i"] += 1
        if n["i"] == 2:
            raise oserr(errno.EIO)
        return real(s, d, *args, **kw)
    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(OSError):
        FO.rename_path(x, tmp_path / "x.bin")
    assert x.exists() and x.read_text() == "x"


def test_case_only_rename_of_a_game_really_renames(lay, settings, tmp_path):
    g = G.install_game(lay, make_wii_iso(tmp_path / "g.iso", "RSPE01", "Sports"), title="Wii Sports")
    r = G.rename_game(lay, g, "WII SPORTS")
    assert [p.name for p in lay.wbfs.iterdir() if not p.name.startswith(".")] == ["WII SPORTS [RSPE01]"]
    assert r.path.exists() and r.path.parent.name == "WII SPORTS [RSPE01]"


def _fat_same_file(monkeypatch):
    """Make "is this the same file?" answer like FAT32: names that differ only in case are."""
    real = FO.same_file

    def ci(a: Path, b: Path) -> bool:
        return real(a, b) or (a.parent == b.parent and a.name.lower() == b.name.lower()
                              and (a.exists() or b.exists()))
    for mod in (FO, G, H):
        if hasattr(mod, "same_file"):
            monkeypatch.setattr(mod, "same_file", ci)
    monkeypatch.setattr(G, "_same_file", ci)


def test_case_only_misnamed_games_can_be_normalised(lay, tmp_path, local_patch):
    wii = G.install_game(lay, make_wii_iso(tmp_path / "w.iso", "RHAE01", "Play"), title="Wii Play")
    gc = G.install_game(lay, make_gc_iso(tmp_path / "g.iso", "GKYE01", "Kirby"), title="Kirby")
    low = wii.path.with_name("rhae01.wbfs")
    wii.path.rename(low)
    up = gc.path.with_name("GAME.ISO")
    gc.path.rename(up)
    # games list and health agree: both are misplaced
    assert {g.game_id: g.problem for g in G.scan_drive(lay)} == {"RHAE01": "misplaced", "GKYE01": "misplaced"}
    ids = sorted(f.id for f in H.check(lay) if f.fix_id in ("move_wii_game", "move_gc_game"))
    assert ids == ["gc_file_name", "wii_file_name"]
    msgs = G.normalize_drive(lay)
    assert len(msgs) == 2 and not any(m.startswith("skipped") for m in msgs)
    assert [p.name for p in wii.path.parent.iterdir()] == ["RHAE01.wbfs"]
    assert [p.name for p in gc.path.parent.iterdir()] == ["game.iso"]
    assert [g.problem for g in G.scan_drive(lay)] == ["", ""]
    # ... and through the health fix, on a filesystem where the right name "already exists"
    wii.path.rename(low)
    gc.path.rename(up)
    _fat_same_file(local_patch)
    real_exists = Path.exists

    def exists(self: Path) -> bool:                     # FAT32: RHAE01.wbfs "exists" as rhae01.wbfs
        return real_exists(self) or any(p.name.lower() == self.name.lower()
                                        for p in (self.parent.iterdir() if real_exists(self.parent) else []))
    local_patch.setattr(Path, "exists", exists)
    for f in [f for f in H.check(lay) if f.fix_id in ("move_wii_game", "move_gc_game")]:
        log = H.fix(lay, f)
        assert log and not any(line.startswith(H.NOT_FIXED) for line in log)
    local_patch.undo()
    assert [p.name for p in wii.path.parent.iterdir()] == ["RHAE01.wbfs"]
    assert [p.name for p in gc.path.parent.iterdir()] == ["game.iso"]


def test_normalize_does_not_skip_a_case_only_rename_on_fat32(lay, tmp_path, local_patch):
    wii = G.install_game(lay, make_wii_iso(tmp_path / "w.iso", "RHAE01", "Play"), title="Wii Play")
    low = wii.path.with_name("rhae01.wbfs")
    wii.path.rename(low)
    _fat_same_file(local_patch)
    real_exists = Path.exists
    local_patch.setattr(Path, "exists", lambda self: real_exists(self) or (
        real_exists(self.parent) and any(p.name.lower() == self.name.lower() for p in self.parent.iterdir())))
    msgs = G.normalize_drive(lay)
    local_patch.undo()
    assert msgs and msgs[0].startswith("moved"), msgs
    assert [p.name for p in wii.path.parent.iterdir()] == ["RHAE01.wbfs"]


def test_fix_layout_uses_the_same_titles_as_games_add(capsys, lay, tmp_path, monkeypatch):
    s = Settings(library_dir=str(tmp_path / "lib"))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: s))
    monkeypatch.setattr(cli, "_titles", lambda settings: {"RHAE01": "Wii Play"}.get)
    make_wii_iso(lay.wbfs / "loose.iso", "RHAE01", "PARTY PACK for REVOLUTION")
    assert entry.main(["games", "fix-layout", "--drive", str(lay.root)]) == 0
    assert (lay.wbfs / "Wii Play [RHAE01]" / "RHAE01.iso").is_file()
    # a move that can't be done makes the command fail
    make_wii_iso(lay.wbfs / "again.iso", "RHAE01", "X")
    assert entry.main(["games", "fix-layout", "--drive", str(lay.root)]) == 1
    assert "skipped again.iso" in capsys.readouterr().out


# ------------------------------------------------------------------ 6: Riivolution, case-insensitive
XML = """<wiidisc version="1" root="/{root}"><id game="RSP"/>
<options><section name="s"><option name="o"><choice name="On"><patch id="p"/></choice></option></section></options>
<patch id="p"><folder external="files" disc="/" recursive="true" create="true"/></patch></wiidisc>"""


def mod_zip(path: Path, xml_name: str, root: str, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"riivolution/{xml_name}", XML.format(root=root))
        for k, v in files.items():
            z.writestr(k, v)
    return path


def test_removing_a_mod_keeps_files_another_mod_recorded_in_other_case(lay, tmp_path):
    a = mod_zip(tmp_path / "ModA.zip", "ModA.xml", "ModShared", {"ModShared/files/a.bin": b"A" * 50})
    R.install_mod(a, lay.root)
    # what installing a second mod with the same file leaves in the record on FAT32
    rec = R._load_record(lay.root)
    rec["ModB"] = {"xmls": ["modb.xml"], "paths": ["modshared/FILES/A.BIN", "riivolution/modb.xml"],
                   "games": [], "source": ""}
    R._save_record(lay.root, rec)
    removed = R.remove_mod(lay.root, "ModA")
    assert (lay.root / "ModShared" / "files" / "a.bin").is_file()
    assert "ModShared/files/a.bin" not in removed and "riivolution/ModA.xml" in removed


def test_a_mod_cannot_take_over_another_mods_file_even_with_overwrite(lay, tmp_path):
    a = mod_zip(tmp_path / "ModA.zip", "ModA.xml", "ModA", {"ModA/files/a.bin": b"A"})
    R.install_mod(a, lay.root)
    # on FAT32 "MODA.XML" *is* ModA.xml; here the same name stands in for that
    e = mod_zip(tmp_path / "ModE.zip", "ModA.xml", "ModE", {"ModE/files/e.bin": b"E"})
    before = (lay.riivolution / "ModA.xml").read_bytes()
    with pytest.raises(R.ModError, match="belongs to the installed mod \"ModA\""):
        R.install_mod(e, lay.root, overwrite=True)
    assert (lay.riivolution / "ModA.xml").read_bytes() == before
    assert not (lay.root / "ModE").exists()
    # the record is matched without regard to case
    rec = R._load_record(lay.root)
    rec["ModA"]["paths"] = [p.upper() if p.startswith("riivolution/") else p for p in rec["ModA"]["paths"]]
    R._save_record(lay.root, rec)
    with pytest.raises(R.ModError, match="belongs to"):
        R.install_mod(e, lay.root, overwrite=True)
    R.install_mod(a, lay.root, overwrite=True)              # updating a mod itself still works


# ------------------------------------------------------------------ 7: plain-language errors
def test_explain_os_error(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    tmpname = str(drive / "private" / "data.bin.rewii-new.rewii-part")
    cases = {
        errno.ENOSPC: "The drive is full",
        errno.EROFS: "write-protected (read-only)",
        errno.EFBIG: "larger than 4 GB, which FAT32 can't hold",
        errno.EIO: "stopped responding",
        errno.EACCES: "Permission denied",
    }
    for code, want in cases.items():
        msg = FO.explain_os_error(oserr(code, tmpname), drive)
        assert want in msg, msg
        assert "Errno" not in msg and "rewii-" not in msg and str(drive) not in msg
    assert "(data.bin)" in FO.explain_os_error(oserr(errno.ENOSPC, tmpname), drive)
    assert FO.explain_os_error(oserr(errno.ENOSPC), None).startswith("The drive is full")
    # a full library is not "the drive"
    assert "This computer's disk is full" in FO.explain_os_error(oserr(errno.ENOSPC, str(tmp_path / "lib" / "x")), drive)
    # the root is gone: whatever the errno, the drive was disconnected
    gone = tmp_path / "gone"
    for code in (errno.ENOENT, errno.EIO, errno.ENODEV):
        assert "was disconnected" in FO.explain_os_error(oserr(code, str(gone / "wbfs" / ".rewii-tmp-x.wbfs")), gone)
    assert FO.explain_os_error(FileNotFoundError(errno.ENOENT, "nope", str(drive / "a.iso")), drive) \
        == "a.iso: no such file or folder"
    assert FO.explain_os_error(ValueError("plain message")) == "plain message"
    assert "isn't connected" in FO.explain_os_error(FO.DriveNotConnected(gone), gone)


def test_health_reports_a_read_only_drive(lay, monkeypatch):
    assert [f for f in H.check(lay) if f.id == "read_only"] == []
    real = os.statvfs

    class RO:
        def __init__(self, st):
            self.st = st

        def __getattr__(self, n):
            return getattr(self.st, n)

        @property
        def f_flag(self):
            return self.st.f_flag | os.ST_RDONLY
    monkeypatch.setattr(os, "statvfs", lambda p: RO(real(p)))
    fs = H.check(lay)
    assert fs[0].id == "read_only" and fs[0].severity == H.ERROR and "write-protected" in fs[0].message
    assert not fs[0].auto_fixable


def test_health_fix_reports_failures_and_exits_non_zero(capsys, lay, tmp_path, monkeypatch):
    s = Settings(library_dir=str(tmp_path / "lib"))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: s))
    monkeypatch.setattr(cli, "_titles", lambda settings: None)
    for name in ("usbloader_gx", "nintendont"):
        (lay.apps / name).mkdir()
        (lay.apps / name / "boot.dol").write_bytes(b"d")
    junk = lay.root / "._junk"
    junk.write_bytes(b"j")
    make_gc_iso(lay.gc_games / "loose.iso", "GKYE01", "Kirby")
    monkeypatch.setattr("rewii.core.drives.drive_for_path", lambda p: None)   # layout checks only
    assert entry.main(["health", "--drive", str(lay.root)]) == 0
    real_unlink, real_move = Path.unlink, FO.shutil.move
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(oserr(errno.EROFS, str(self))))
    monkeypatch.setattr(FO.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(oserr(errno.EROFS, str(a[1]))))
    capsys.readouterr()
    rc = entry.main(["health", "--drive", str(lay.root), "--fix"])
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(FO.shutil, "move", real_move)
    out = capsys.readouterr()
    assert rc == 1 and out.err.count("couldn't fix:") == 2
    assert "write-protected" in out.err and "Errno" not in out.err
    assert "2 fix(es) failed" in out.out and junk.exists()
    assert entry.main(["health", "--drive", str(lay.root), "--fix"]) == 0     # writable again: fixed


def test_config_backups_are_atomic_only_on_change_and_only_the_newest_is_kept(lay, monkeypatch):
    p = lay.apps / "priiloader" / "hacks_hash.ini"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"old\n")

    def baks():
        return sorted(x.name for x in p.parent.iterdir() if ".bak-" in x.name)
    assert S._save_with_backup(p, b"old\n") is None and baks() == []           # unchanged: no backup
    stamps = iter(["20260101-000001", "20260101-000002"])
    monkeypatch.setattr(S.time, "strftime", lambda fmt: next(stamps))
    S._save_with_backup(p, b"new\n")
    assert baks() == ["hacks_hash.ini.bak-20260101-000001"]
    S._save_with_backup(p, b"newer\n")
    assert baks() == ["hacks_hash.ini.bak-20260101-000002"]                      # only the newest
    assert (p.parent / baks()[0]).read_bytes() == b"new\n" and p.read_bytes() == b"newer\n"
    # a full drive: the old file stays, and no empty backup or temp file appears
    monkeypatch.setattr(S.time, "strftime", lambda fmt: "20260101-000003")
    monkeypatch.setattr(S, "copy_atomic", lambda *a, **k: (_ for _ in ()).throw(oserr(errno.ENOSPC)))
    with pytest.raises(OSError):
        S._save_with_backup(p, b"newest\n")
    assert p.read_bytes() == b"newer\n" and baks() == ["hacks_hash.ini.bak-20260101-000002"]
    assert sorted(x.name for x in p.parent.iterdir()) == ["hacks_hash.ini", baks()[0]]


def test_failed_app_install_leaves_no_empty_app_folder(lay, local_patch):
    import hashlib
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("apps/CleanRip/boot.dol", b"d" * 1000)
        z.writestr("apps/CleanRip/meta.xml", b"<app/>")
    data = buf.getvalue()
    app = osc.App.from_api({
        "slug": "CleanRip", "name": "CleanRip", "version": "1", "category": "utilities",
        "assets": {"archive": {"url": "https://hbb1.oscwii.org/api/contents/CleanRip/CleanRip.zip",
                               "hash": hashlib.md5(data).hexdigest(), "size": len(data)}},
        "subdirectories": ["/apps/CleanRip"]})
    net.set_client(httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=data))))
    real_open = open

    def full(p, mode="r", *a, **k):
        if "w" in mode and str(p).startswith(str(lay.apps)):
            raise oserr(errno.ENOSPC, str(p))
        return real_open(p, mode, *a, **k)
    local_patch.setattr("builtins.open", full)
    with pytest.raises(OSError):
        osc.install_app(lay, app)
    local_patch.undo()
    assert not (lay.apps / "CleanRip").exists()
    assert [f for f in H.check(lay) if f.id == "app_no_boot"] == []
    rep = osc.install_starter_kit(lay, "wii", ["CleanRip"], apps=[app], skip_current=False)
    assert rep.installed == ["CleanRip"] and (lay.apps / "CleanRip" / "boot.dol").is_file()


def test_space_check_counts_clusters_and_the_copy_next_to_the_old_file(tmp_path):
    old = tmp_path / "t" / "big.bin"
    old.parent.mkdir()
    old.write_bytes(b"o" * 40_000)
    todo = [(tmp_path / "s1", old, B.FileEntry("big.bin", 40_000, 0)),
            (tmp_path / "s2", tmp_path / "t" / "small.txt", B.FileEntry("small.txt", 10, 0))]
    cl = 32768
    # big.bin is replaced by a same-size file: for a moment both exist (2 clusters more);
    # the 10-byte file still costs a whole cluster
    assert B._space_needed(todo, cl) == 2 * cl
    assert B._space_needed(list(reversed(todo)), cl) == 3 * cl
    assert B._space_needed([], cl) == 0
    assert B._cluster_size(tmp_path / "t" / "not" / "yet") > 0


# ------------------------------------------------------------------ 8: 4 GB files and FAT32
def test_files_too_big_for_fat32_are_refused_before_anything_is_copied(tmp_path, monkeypatch, settings):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for rel, n in (("a.txt", 5), ("media/big1.bin", 3000), ("media/big2.bin", 2500)):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(b"x" * n)
    dst.mkdir()
    monkeypatch.setattr(B, "FAT32_MAX_FILE", 2000)
    monkeypatch.setattr(B, "is_fat32", lambda p: True)
    with pytest.raises(B.FileTooBig) as ei:
        B.clone_drive(src, dst)
    assert ei.value.paths == ["media/big1.bin", "media/big2.bin"]
    assert "4 GB or larger" in str(ei.value) and "media/big2.bin" in str(ei.value)
    assert list(dst.iterdir()) == []                         # not even the small file was copied
    snap = B.create_snapshot(src, settings, "s")
    with pytest.raises(B.FileTooBig):
        B.restore_snapshot(snap, dst)
    monkeypatch.setattr(B, "is_fat32", lambda p: None)       # unknown filesystem: just try
    assert len(B.clone_drive(src, dst).copied) == 3
    assert B.is_fat32(tmp_path) in (False, None)


# ------------------------------------------------------------------ 9: time-zone shifts
def test_whole_hour_time_shifts_are_not_changes():
    def e(mtime, size=10, sha=""):
        return B.FileEntry("f", size, mtime, sha)
    t = 1_700_000_000.0
    assert B._same(e(t), e(t + 1.5))
    for hours in (1, -1, 2, 13, 26):                         # DST, another time zone
        assert B._same(e(t), e(t + hours * 3600)), hours
        assert B._same(e(t), e(t + hours * 3600 + 2)), hours
    assert not B._same(e(t), e(t + 27 * 3600))
    assert not B._same(e(t), e(t + 1800))                    # half an hour is a real change
    assert not B._same(e(t), e(t + 3600 + 5))
    assert not B._same(e(t), e(t + 3600, size=11))
    assert not B._same(e(t, sha="a"), e(t + 3600, sha="b"))  # hashes win when both are known


def test_diff_and_clone_after_a_time_zone_shift(tmp_path, settings):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "apps").mkdir(parents=True)
    (src / "apps" / "boot.dol").write_bytes(b"d" * 100)
    dst.mkdir()
    snap = B.create_snapshot(src, settings, "s")
    B.clone_drive(src, dst)
    st = (src / "apps" / "boot.dol").stat()
    os.utime(src / "apps" / "boot.dol", (st.st_atime, st.st_mtime + 3600))      # the clocks changed
    assert B.diff_snapshot(snap, src).identical
    rep = B.clone_drive(src, dst)
    assert rep.copied == [] and rep.skipped == ["apps/boot.dol"]


# ------------------------------------------------------------------ 10: old WBFS drives
def wbfs_partition(tmp_path: Path, wii: Path) -> Path:
    with D.open_disc(wii) as d:
        wf = W.write_wbfs(d, tmp_path / "one.wbfs")[0]
    data = bytearray(wf.read_bytes())
    data[12] = 1                                             # slot 0 in use
    img = tmp_path / "part.img"
    img.write_bytes(data)
    wf.unlink()
    return img


def test_migrate_reports_cut_off_images_and_fails_the_command(capsys, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    monkeypatch.setattr(cli, "_titles", lambda s: None)
    img = wbfs_partition(tmp_path, make_wii_iso(tmp_path / "game.iso"))
    whole = img.stat().st_size
    with open(img, "r+b") as f:
        f.truncate(whole // 2)                               # the image ends before the game does
    res = G.migrate_wbfs_partition(img, None, settings)
    assert res.skipped == 1 and res.damaged == 0 and res.problems == 1
    assert "cut off" in res[0] and "magic" not in res[0]
    assert not list(settings.wii_games_dir.iterdir())
    assert entry.main(["games", "migrate", str(img)]) == 1
    assert "1 skipped" in capsys.readouterr().err


def test_migrate_verifies_and_flags_damaged_games(capsys, tmp_path, settings, lay, monkeypatch):
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    monkeypatch.setattr(cli, "_titles", lambda s: None)
    img = wbfs_partition(tmp_path, make_wii_iso(tmp_path / "game.iso"))
    ok = G.migrate_wbfs_partition(img, lay, settings)
    assert ok.problems == 0 and len(ok) == 2 and all("DAMAGED" not in m for m in ok)
    for p in list(settings.wii_games_dir.iterdir()):
        p.unlink()
    monkeypatch.setattr(G, "verify_game", lambda *a, **k: G.VerifyResult(False, message="36 damaged clusters"))
    bad = G.migrate_wbfs_partition(img, None, settings)
    assert bad.damaged == 1 and bad.skipped == 0
    assert "DAMAGED" in bad[0] and "36 damaged clusters" in bad[0] and "kept" in bad[0]
    assert len(list(settings.wii_games_dir.iterdir())) == 1          # the copy is kept, but flagged
    for p in list(settings.wii_games_dir.iterdir()):
        p.unlink()
    assert entry.main(["games", "migrate", str(img)]) == 1
    assert "copied but damaged" in capsys.readouterr().err


# ------------------------------------------------------------------ 11: smaller issues
def test_snapshot_diff_ignores_case(tmp_path, settings):
    src = tmp_path / "src"
    (src / "apps" / "priiloader").mkdir(parents=True)
    f = src / "apps" / "priiloader" / "boot.dol"
    f.write_bytes(b"d" * 10)
    snap = B.create_snapshot(src, settings, "s")
    f.rename(f.with_name("BOOT.DOL"))
    d = B.diff_snapshot(snap, src)
    assert d.identical, (d.added, d.removed, d.changed)
    f.with_name("BOOT.DOL").write_bytes(b"e" * 11)
    d = B.diff_snapshot(snap, src)
    assert d.changed == ["apps/priiloader/BOOT.DOL"] and not d.added and not d.removed


def _fat_drive(size: int, cluster: int) -> Drive:
    v = Volume(device="/dev/x1", size=size, filesystem=FAT32, cluster_size=cluster, free=size)
    return Drive(id="x", device="/dev/x", size=size, partition_style="MBR", volumes=[v])


def test_cluster_size_warning_matches_what_the_formatter_would_choose():
    gib = 1024 ** 3
    assert H.expected_cluster(2 * gib) == 16384 and H.expected_cluster(64 * gib) == 32768
    # 2 GB drive formatted by ReWii: 16 KB is the right answer there, no warning
    assert [f for f in H.check_drive(_fat_drive(2 * gib, 16384)) if f.id == "cluster_size"] == []
    assert [f for f in H.check_drive(_fat_drive(64 * gib, 32768)) if f.id == "cluster_size"] == []
    f = [f for f in H.check_drive(_fat_drive(64 * gib, 4096)) if f.id == "cluster_size"]
    assert len(f) == 1 and "32 KB clusters are recommended" in f[0].message
    f = [f for f in H.check_drive(_fat_drive(2 * gib, 4096)) if f.id == "cluster_size"]
    assert len(f) == 1 and "16 KB clusters are recommended" in f[0].message


def test_starter_kit_without_a_console_type_leaves_out_console_specific_apps(lay):
    slugs = [i.slug for i in osc.starter_kit("", "essential")]
    assert "d2x-cios-installer" not in slugs and "d2x-cios-installer-vwii" not in slugs
    assert "usbloader_gx" in slugs
    assert "d2x-cios-installer" in [i.slug for i in osc.starter_kit("wii", "essential")]
    assert "d2x-cios-installer-vwii" in [i.slug for i in osc.starter_kit("vwii", "essential")]
    rep = osc.install_starter_kit(lay, "", "essential", apps=[])
    assert rep.needs_console == ["d2x-cios-installer", "d2x-cios-installer-vwii"]
    assert "d2x-cios-installer" not in rep.not_found
    from rewii.tui.base import kit_lines
    lines, _bad = kit_lines(rep)
    assert any("console type isn't set" in ln and "d2x-cios-installer" in ln for ln in lines)
    assert osc.install_starter_kit(lay, "wii", "essential", apps=[]).needs_console == []


def test_apps_inside_apps_is_reported_once_and_fixed_without_errors(lay):
    inner = lay.apps / "apps" / "WiiXplorer"
    inner.mkdir(parents=True)
    (inner / "boot.dol").write_bytes(b"d")
    fs = [f for f in H.check(lay) if f.path is not None and "apps" in str(f.path)
          and f.id in ("nested_apps", "app_double_folder", "app_no_boot")]
    assert [f.id for f in fs] == ["nested_apps"]
    for f in [f for f in H.check(lay) if f.auto_fixable]:
        assert not any(line.startswith(H.NOT_FIXED) for line in H.fix(lay, f))
    assert (lay.apps / "WiiXplorer" / "boot.dol").is_file() and not (lay.apps / "apps").exists()


def test_scan_flags_a_split_game_with_a_missing_part(lay, tmp_path, monkeypatch):
    monkeypatch.setattr(G, "FAT32_SPLIT", 5 << 20)
    iso = make_wii_iso(tmp_path / "g.iso", "RSPE01", "Sports",
                       files={"opening.bnr": fakedisc.imet_banner(), "data.bin": os.urandom(9 << 20)})
    g = G.install_game(lay, iso)
    assert len(g.files) >= 3 and G.scan_drive(lay)[0].problem == ""
    hold = g.files[1].with_name("hold.bin")
    g.files[1].rename(hold)                                  # .wbf1 gone, .wbf2 still there
    assert G.scan_drive(lay)[0].problem == G.INCOMPLETE_PROBLEM
    hold.rename(g.files[1])
    g.files[-1].unlink()                                     # the last part gone
    assert G.scan_drive(lay)[0].problem == G.INCOMPLETE_PROBLEM
    assert G.normalize_drive(lay) == []                      # never shuffled around as "misplaced"


def test_network_errors_say_check_your_connection(tmp_path):
    def boom(req):
        raise httpx.ConnectError("[Errno 111] Connection refused")
    net.set_client(httpx.Client(transport=httpx.MockTransport(boom)))
    for call in (lambda: net.get_bytes("https://hbb1.oscwii.org/api/v4/contents"),
                 lambda: net.get("https://hbb1.oscwii.org/x"),
                 lambda: net.download("https://hbb1.oscwii.org/x.zip", tmp_path / "x.zip")):
        with pytest.raises(net.NetError) as ei:
            call()
        msg = str(ei.value)
        assert msg.startswith("Couldn't connect — check your internet connection")
        assert "hbb1.oscwii.org" in msg and "Connection refused" in msg and "Errno" not in msg
