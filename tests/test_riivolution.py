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
    res = R.install_mod(src, drive)
    assert (drive / "riivolution" / "simple.xml").is_file()
    assert (drive / "riivolution" / "simple" / "files" / "a.arc").is_file()
    assert not res.warnings
