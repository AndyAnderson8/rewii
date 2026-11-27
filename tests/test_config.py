"""Settings: loading never raises, bad values fall back to defaults."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rewii.core.config import Settings, check_library_path


def _load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "settings.json"
    p.write_text(text, "utf-8")
    return Settings.load(p)


@pytest.mark.parametrize("text", ["", "{not json", "[1, 2, 3]", "null", '"text"', "42", "\x00\x01"])
def test_load_never_raises_on_a_damaged_file(tmp_path, text):
    assert _load(tmp_path, text) == Settings()


def test_load_missing_or_unreadable_file_gives_defaults(tmp_path):
    assert Settings.load(tmp_path / "nope.json") == Settings()
    assert Settings.load(tmp_path) == Settings()          # a folder, not a file


def test_bad_values_fall_back_one_by_one(tmp_path):
    lib = tmp_path / "lib"
    s = _load(tmp_path, json.dumps({
        "library_dir": str(lib), "forwarder_style": "fancy", "cover_lang": 7, "console_type": "wiiu",
        "console_region": "PAL", "online_server": None, "scrub_games": "yes",
        "cover_types": ["cover", 3, None, "disc"], "last_drive": ["x"], "wii_ip": "192.168.1.9",
        "first_run_done": True, "unknown_key": 1}))
    d = Settings()
    assert s.library_dir == str(lib)
    assert s.forwarder_style == d.forwarder_style and s.cover_lang == d.cover_lang
    assert s.console_type == "" and s.console_region == "PAL"
    assert s.online_server == d.online_server and s.scrub_games is d.scrub_games
    assert s.cover_types == ["cover", "disc"]
    assert s.last_drive == "" and s.wii_ip == "192.168.1.9" and s.first_run_done is True


@pytest.mark.parametrize("bad", ["", "   ", ".", "relative/dir", 5, None])
def test_empty_or_relative_library_in_the_file_is_ignored(tmp_path, bad):
    assert _load(tmp_path, json.dumps({"library_dir": bad})).library_dir == Settings().library_dir


def test_save_load_round_trip(tmp_path):
    p = tmp_path / "cfg" / "settings.json"
    s = Settings(library_dir=str(tmp_path / "lib"), console_type="vwii", console_region="NTSC-U",
                 wii_ip="10.0.0.2", forwarder_style="black", cover_types=["cover"])
    s.save(p)
    assert Settings.load(p) == s


def test_check_library_path(tmp_path):
    assert check_library_path(f'  "{tmp_path}"  ') == tmp_path
    assert check_library_path("~/ReWii") == Path.home() / "ReWii"
    for bad in ("", "  ", ".", "games", "../x"):
        with pytest.raises(ValueError):
            check_library_path(bad)
