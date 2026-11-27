"""Tests for rewii.core.riivolution."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from rewii.core import riivolution as R
from rewii.core.config import Settings

XML = """<wiidisc version="1" root="/MyMod">
  <id game="RMC"><region type="E"/><region type="P"/></id>
  <options><section name="My Mod"><option name="Tracks"><choice name="On">
    <patch id="tracks"/></choice></option></section></options>
  <patch id="tracks">
    <folder external="Course" disc="/Race/Course" create="true"/>
    <file external="/MyModExtra/menu.szs" disc="/Scene/UI/Title.szs"/>
    <file external="{$__region}/x.szs" disc="/x.szs"/>
    <savegame external="/riivolution/save/MyMod" clone="false"/>
  </patch>
</wiidisc>"""


def test_combine_rules() -> None:
    assert R.combine("/riivolution", "mymod") == "/riivolution/mymod"
    assert R.combine("/riivolution", "/mymod") == "/mymod"
    assert R.combine("/riivolution", "../mymod/") == "/mymod"
    assert R.combine("/a", "b\\c") == "/a/b/c"


def test_parse_xml() -> None:
    x = R.parse_xml(XML)
    assert x.game_ids == ["RMCE", "RMCP"]
    assert x.patches == {"tracks": "/MyMod"}
    assert x.externals == ["/MyMod/Course", "/MyModExtra/menu.szs"]
    assert x.savegame_paths == ["/riivolution/save/MyMod"]
    assert x.targets_game("RMCE01") and not x.targets_game("RMCJ01")
    assert x.top_dirs() == {"MyMod", "MyModExtra"}
    with pytest.raises(R.ModError):
        R.parse_xml("<notwiidisc/>")
    with pytest.raises(R.ModError):
        R.parse_xml("<wiidisc")


def make_mod_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Release/riivolution/mymod.xml", XML)
        z.writestr("Release/MyMod/Course/beginner_course.szs", b"course")
        z.writestr("Release/MyModExtra/menu.szs", b"menu")
        z.writestr("Release/README.txt", b"readme")
        z.writestr("__MACOSX/Release/._x", b"junk")
    return path


def test_install_list_remove(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    st = Settings(library_dir=str(tmp_path / "lib"))
    res = R.install_mod(make_mod_zip(tmp_path / "MyMod.zip"), drive, st, game_id="RMCJ01")
    assert res.name == "MyMod"
    assert (drive / "riivolution" / "mymod.xml").is_file()
    assert (drive / "MyMod" / "Course" / "beginner_course.szs").read_bytes() == b"course"
    assert (drive / "MyModExtra" / "menu.szs").is_file()
    assert not (drive / "README.txt").exists()
    assert any("not RMCJ01" in w for w in res.warnings)
    assert res.library_copy and (res.library_copy / "MyMod.zip").is_file()

    (drive / "riivolution" / "other.xml").write_text(
        '<wiidisc version="1"><id game="SMN"/></wiidisc>')
    mods = {m.name: m for m in R.list_installed_mods(drive)}
    assert mods["MyMod"].managed and mods["MyMod"].games == ["RMCE", "RMCP"]
    assert not mods["other"].managed and mods["other"].games == ["SMN"]

    removed = R.remove_mod(drive, "MyMod")
    assert "riivolution/mymod.xml" in removed
    assert not (drive / "MyMod").exists() and not (drive / "MyModExtra").exists()
    assert (drive / "riivolution" / "other.xml").exists()
    with pytest.raises(R.ModError):
        R.remove_mod(drive, "other")


def test_install_conflict_and_zip_slip(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    (drive / "MyModExtra").mkdir(parents=True)
    (drive / "MyModExtra" / "menu.szs").write_bytes(b"someone else's")
    with pytest.raises(R.ModError, match="already exists"):
        R.install_mod(make_mod_zip(tmp_path / "m.zip"), drive)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.xml", XML)
    with pytest.raises(R.ModError, match="unsafe"):
        R.install_mod(bad, drive)


def test_install_from_folder_without_wrapper(tmp_path: Path) -> None:
    """XML + data with a relative root next to it (data lands under /riivolution)."""
    src = tmp_path / "Simple"
    (src / "simple" / "files").mkdir(parents=True)
    (src / "simple.xml").write_text(
        '<wiidisc version="1" root="simple"><id game="SMN"/>'
        '<patch id="p"><folder external="files" disc="/"/></patch></wiidisc>')
    (src / "simple" / "files" / "a.arc").write_bytes(b"a")
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    res = R.install_mod(src, drive)
    assert (drive / "riivolution" / "simple.xml").is_file()
    assert (drive / "riivolution" / "simple" / "files" / "a.arc").is_file()
    assert not res.warnings


# --------------------------------------------------------------------------- regressions
def test_remove_never_deletes_outside_the_drive(tmp_path: Path) -> None:
    import json
    victim = tmp_path / "home" / "important.txt"
    victim.parent.mkdir()
    victim.write_text("precious")
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    R.install_mod(make_mod_zip(tmp_path / "MyMod.zip"), drive)
    rec_p = drive / "rewii" / R.RECORD
    rec = json.loads(rec_p.read_text())
    (drive / "keep.txt").write_text("k")
    rec["Cool Mod"] = {"xmls": [], "games": [], "paths": [
        str(victim), "../home/important.txt", "riivolution/../../home/important.txt",
        "C:/Windows/x", "keep.txt/../../home/important.txt", 5, None]}
    rec["Broken"] = "not a dict"
    rec_p.write_text(json.dumps(rec))
    assert R.remove_mod(drive, "Cool Mod") == []
    assert victim.read_text() == "precious" and (drive / "keep.txt").exists()
    assert [m.name for m in R.list_installed_mods(drive)] == ["MyMod"]
    assert R.remove_mod(drive, "MyMod") and not (drive / "MyMod").exists()


def test_mod_cannot_plant_rewii_records_or_touch_protected_folders(tmp_path: Path) -> None:
    for top in ("rewii", "bootmii", "wad", "ReWii"):
        mod = tmp_path / f"evil-{top}"
        (mod / "riivolution").mkdir(parents=True)
        (mod / top).mkdir()
        (mod / "riivolution" / "evil.xml").write_text(
            f'<wiidisc version="1"><id game="RMC"/><patch id="p">'
            f'<file disc="/a" external="/{top}/x.bin"/></patch></wiidisc>')
        (mod / top / "x.bin").write_text("x")
        (mod / top / "riivolution.json").write_text('{"Cool Mod": {"paths": ["/etc/passwd"]}}')
        drive = tmp_path / f"drive-{top}"
        drive.mkdir()
        with pytest.raises(R.ModError, match="doesn't allow"):
            R.install_mod(mod, drive)
        assert not any(drive.iterdir())                 # nothing written at all


def test_zip_with_drive_letter_component_is_refused(tmp_path: Path) -> None:
    (tmp_path / "drive").mkdir()
    for bad_name in ("foo/D:/evil.txt", "a/b/../../../x"):
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("riivolution/m.xml", XML)
            z.writestr(bad_name, b"x")
        with pytest.raises(R.ModError):
            R.install_mod(bad, tmp_path / "drive")
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("riivolution/m.xml", XML)
        z.writestr("MyMod/Course/huge.szs", bytes(300_000))
    old, R.MAX_MOD_BYTES = R.MAX_MOD_BYTES, 100_000
    try:
        with pytest.raises(R.ModError, match="large"):
            R.install_mod(big, tmp_path / "drive")
    finally:
        R.MAX_MOD_BYTES = old
    assert not any((tmp_path / "drive").iterdir())


def test_reinstall_from_library_copy_keeps_it(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    st = Settings(library_dir=str(tmp_path / "lib"))
    lib = R.install_mod(make_mod_zip(tmp_path / "MyMod.zip"), drive, st).library_copy
    assert lib is not None and (lib / "MyMod.zip").is_file()
    # from the zip inside the library copy, and from the library folder itself
    res = R.install_mod(lib / "MyMod.zip", drive, st)
    assert res.library_copy == lib and (lib / "MyMod.zip").is_file()
    folder = tmp_path / "Folder"
    (folder / "riivolution").mkdir(parents=True)
    (folder / "riivolution" / "f.xml").write_text('<wiidisc version="1"><id game="RMC"/></wiidisc>')
    lib2 = R.install_mod(folder, drive, st).library_copy
    res = R.install_mod(lib2, drive, st)
    assert res.library_copy == lib2 and (lib2 / "riivolution" / "f.xml").is_file()
    # a normal re-install replaces the copy, leaving no temporary folders behind
    (folder / "riivolution" / "f.xml").write_text('<wiidisc version="1"><id game="RSB"/></wiidisc>')
    R.install_mod(folder, drive, st, overwrite=True)
    assert "RSB" in (lib2 / "riivolution" / "f.xml").read_text()
    assert sorted(p.name for p in lib2.parent.iterdir()) == ["Folder", "MyMod"]


def test_conflict_is_found_before_anything_is_copied(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    (drive / "MyModExtra").mkdir(parents=True)
    (drive / "MyModExtra" / "menu.szs").write_bytes(b"someone else's")
    with pytest.raises(R.ModError, match="already exists"):
        R.install_mod(make_mod_zip(tmp_path / "m.zip"), drive)
    # the XML and the first folder used to be copied before the conflict was hit
    assert not (drive / "riivolution").exists() and not (drive / "MyMod").exists()
    assert R.list_installed_mods(drive) == []


def test_failed_copy_is_rolled_back(tmp_path: Path, monkeypatch) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    real, n = R._copy_file, []

    def flaky(src, dst):
        n.append(dst)
        if len(n) == 3:
            raise OSError(28, "No space left on device")
        real(src, dst)
    monkeypatch.setattr(R, "_copy_file", flaky)
    with pytest.raises(OSError):
        R.install_mod(make_mod_zip(tmp_path / "m.zip"), drive)
    assert [p for p in drive.rglob("*") if p.is_file()] == []
    assert R.list_installed_mods(drive) == []


def test_case_insensitive_sources_and_odd_xml(tmp_path: Path) -> None:
    src = tmp_path / "CaseMod"
    (src / "Riivolution").mkdir(parents=True)
    (src / "Riivolution" / "c.xml").write_text(XML)
    (src / "mymod" / "course").mkdir(parents=True)          # XML says /MyMod/Course
    (src / "mymod" / "course" / "a.szs").write_bytes(b"a")
    (src / "MYMODEXTRA").mkdir()
    (src / "MYMODEXTRA" / "MENU.SZS").write_bytes(b"m")
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    res = R.install_mod(src, drive)
    assert not any("aren't in the mod" in w for w in res.warnings)
    assert (drive / "MyMod" / "course" / "a.szs").read_bytes() == b"a"
    assert (drive / "MyModExtra" / "MENU.SZS").read_bytes() == b"m"
    # an XML declaring an unknown encoding is a ModError, and doesn't break the list
    bad = b'<?xml version="1.0" encoding="shift_jisx"?><wiidisc version="1"/>'
    with pytest.raises(R.ModError):
        R.parse_xml(bad)
    (drive / "riivolution" / "odd.xml").write_bytes(bad)
    assert "odd" in [m.name for m in R.list_installed_mods(drive)]
