"""Tests for rewii.core.ids."""
from __future__ import annotations

import sys

import pytest

from rewii.core import ids


def test_valid_game_id():
    for good in ("RMCE01", "rmce01", " GALE01 ", "HADE", "W2GE"):
        assert ids.valid_game_id(good), good
    for bad in ("", "RMC", "RMCE0", "RMCE012", "../../", "RM/E01", "RMCE0?", "??????", "RMCÉ01",
                "RM E01", "A:\\B", None, 123456):
        assert not ids.valid_game_id(bad), bad
    assert ids.valid_id4("RMCE") and not ids.valid_id4("/tmp") and not ids.valid_id4("RMCE01")


def test_filename():
    assert ids.id_for_filename("rmce01") == "RMCE01"
    assert ids.id_for_filename("../../") == "______" and ids.id_for_filename("/a/b") == "_A_B"
    assert ids.id_for_filename("") == "_"


def test_disc_ids_are_defined_once_and_lowercase_is_accepted():
    from rewii.core import ids
    from rewii.core.disc import DiscError, parse_header
    from rewii.core.layout import id_from_folder
    assert ids.is_disc_id("RMCE01") and ids.is_disc_id(b"rmce01") and not ids.is_disc_id("RMCE0") \
        and not ids.is_disc_id("RMC/01") and not ids.is_disc_id(None)
    assert id_from_folder("Mario Kart [RMCE01]") == "RMCE01" == ids.id_in_brackets("x [RMCE01] y")
    assert id_from_folder("homebrew [rmce01]") == "rmce01" and id_from_folder("no id [RMCE]") is None
    hdr = bytearray(0x60)
    hdr[:6] = b"rmce01"
    hdr[0x18:0x1C] = bytes.fromhex("5D1C9EA3")
    assert parse_header(bytes(hdr))[0] == "rmce01"
    hdr[:6] = b"RM/E01"
    with pytest.raises(DiscError):
        parse_header(bytes(hdr))


def test_platform_guess_is_one_rule():
    from rewii.core import ids
    from rewii.core.channels import channel_id_collisions
    assert [ids.platform_guess(x) for x in ("GALE01", "DOLE01", "PZLE01", "RMCE01", "SMNE01", "")] == [
        "gc", "gc", "gc", "wii", "wii", "wii"]
    # a bare ID is guessed the same way everywhere (GameCube 'G' channel vs Wii 'U' channel)
    assert channel_id_collisions(["GALE01", "RALE01"]) == {}


def test_safe_name_never_returns_a_windows_device_name():
    from rewii.core.layout import DriveLayout, game_folder_name, safe_name
    from rewii.core.wad import staged_name
    from pathlib import Path
    assert safe_name("CON") == "_CON" and safe_name("nul.txt") == "_nul.txt" and safe_name("com1") == "_com1"
    assert safe_name("Console") == "Console" and safe_name("  a  b. ") == "a b" and safe_name("???") == "Game"
    assert safe_name("x" * 63 + ". y", maxlen=64) == "x" * 63            # no trailing dot after cutting
    assert game_folder_name("AUX", "RMCE01") == "_AUX [RMCE01]"
    assert staged_name(Path("aux.wad")) == "_aux.wad"
    if sys.platform != "win32":                       # "a:" is a drive letter there, not part of the name
        assert staged_name(Path("a:b?.wad")) == "ab.wad"
    assert staged_name(Path("....wad")) == "title.wad"
    lay = DriveLayout(Path("/x"))
    assert DriveLayout.of(lay) is lay and DriveLayout.of("/x") == lay and DriveLayout.of(Path("/x")).apps == lay.apps
