"""Tests for rewii.core.gametdb (network mocked)."""
from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from rewii.core import config, net
from rewii.core.config import Settings
from rewii.core.gametdb import TitleDB, parse_wiitdb

XML = """<?xml version="1.0" encoding="UTF-8"?>
<datafile>
  <WiiTDB version="1" games="3"/>
  <game name="Mario Kart Wii (USA)">
    <id>RMCE01</id><type/><region>NTSC-U</region><languages>EN,FR,ES</languages>
    <locale lang="EN"><title>Mario Kart Wii</title><synopsis>Race   with
      friends.</synopsis></locale>
    <locale lang="DE"><title>Mario Kart Wii DE</title><synopsis/></locale>
    <developer>Nintendo EAD</developer><publisher>Nintendo</publisher>
    <date year="2008" month="4" day="27"/><genre>racing,kart racing</genre>
    <rating type="ESRB" value="E"><descriptor>comic mischief</descriptor></rating>
    <wi-fi players="12"><feature>online</feature></wi-fi>
    <input players="4"><control type="wiimote" required="true"/>
      <control type="nunchuk" required="false"/></input>
  </game>
  <game name="Super Smash Bros. Melee (USA)">
    <id>GALE01</id><type>GameCube</type><region>NTSC-U</region>
    <locale lang="EN"><title>Super Smash Bros. Melée</title><synopsis/></locale>
  </game>
  <game name="Donkey Kong (USA)">
    <id>FAAE</id><type>VC-NES</type><region>NTSC-U</region>
    <locale lang="EN"><title>Donkey Kong</title><synopsis/></locale>
  </game>
</datafile>"""


def make_zip(xml: str = XML) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("wiitdb.xml", xml)
    return b.getvalue()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(config, "cache_dir", lambda: d)
    monkeypatch.setattr(net, "cache_dir", lambda: d)
    yield d
    net.set_client(None)


def mock(handler):
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_fields():
    g = parse_wiitdb(io.BytesIO(XML.encode()), "EN")
    m = g["RMCE01"]
    assert m.title == "Mario Kart Wii"
    assert m.type == "Wii" and m.region == "NTSC-U"
    assert m.languages == ("EN", "FR", "ES")
    assert m.year == 2008 and m.genre == ("racing", "kart racing")
    assert m.players == 4 and m.wifi_players == 12
    assert m.controllers == ("wiimote", "nunchuk") and m.required_controllers == ("wiimote",)
    assert m.rating == "ESRB E"
    assert m.synopsis == "Race with friends."
    assert g["GALE01"].is_gamecube
    assert g["FAAE"].is_virtual_console


def test_localized_title_with_fallback():
    g = parse_wiitdb(io.BytesIO(XML.encode()), "DE")
    assert g["RMCE01"].title == "Mario Kart Wii DE"
    assert g["RMCE01"].synopsis == "Race with friends."  # falls back to EN synopsis
    assert g["GALE01"].title.startswith("Super Smash")


def test_load_downloads_builds_index_and_works_offline(cache):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        assert "LANG=EN" in str(req.url) and "GAMECUBE=TRUE" in str(req.url)
        return httpx.Response(200, content=make_zip())

    mock(handler)
    db = TitleDB.load(Settings(), refresh=False)
    assert db.available and len(db) == 3
    assert db.title("RMCE01") == "Mario Kart Wii"
    assert TitleDB.index_path("EN").exists()
    assert len(calls) == 1

    # offline + forced refresh: stale copies are used
    def offline(req):
        raise httpx.ConnectError("offline")

    mock(offline)
    db2 = TitleDB.load(Settings(), refresh=True)
    assert db2.get("GALE01") is not None

    # zip gone, index remains
    (cache / TitleDB.zip_cache_name("EN")).unlink()
    db3 = TitleDB.load(Settings())
    assert db3.title("FAAE") == "Donkey Kong"


def test_load_without_cache_offline_is_empty(cache):
    def offline(req):
        raise httpx.ConnectError("offline")

    mock(offline)
    db = TitleDB.load(Settings())
    assert not db.available
    assert db.get("RMCE01") is None
    assert db.title("RMCE01") == "RMCE01"
    assert db.title("RMCE01", "My Game") == "My Game"
    assert db.search("mario") == []


def test_bad_zip_degrades(cache):
    mock(lambda req: httpx.Response(200, content=b"not a zip"))
    db = TitleDB.load(Settings())
    assert not db.available


def test_get_id4_fallback_and_search():
    db = TitleDB(parse_wiitdb(io.BytesIO(XML.encode())))
    assert db.get("faae").title == "Donkey Kong"
    assert db.get("FAAE01").title == "Donkey Kong"   # ID6 -> ID4
    assert "RMCE01" in db
    assert [g.id for g in db.search("mario kart")] == ["RMCE01"]
    assert [g.id for g in db.search("melee")] == ["GALE01"]   # accent-insensitive
    assert db.search("GALE01")[0].id == "GALE01"
    assert [g.id for g in db.search("kong", types=["Wii"])] == []
    assert db.search("") == []


def test_index_roundtrip(cache):
    db = TitleDB(parse_wiitdb(io.BytesIO(XML.encode())), "v1", "EN")
    p = TitleDB.index_path("EN")
    db._write_index(p)
    back = TitleDB._read_index(p, "EN")
    assert back is not None and back.games == db.games and back.version == "v1"


# --------------------------------------------------------------------------- regressions
def test_captive_portal_page_is_not_cached(cache):
    calls = []

    def portal(req):
        calls.append(1)
        return httpx.Response(200, content=b"<html>Please log in</html>")
    mock(portal)
    assert not TitleDB.load(Settings()).available
    assert not list((cache / "gametdb").glob("*.zip"))          # nothing kept for a week
    mock(lambda req: httpx.Response(200, content=make_zip()))
    assert TitleDB.load(Settings()).available                   # works as soon as the network does
    # ...and a later bad refresh keeps the good database
    mock(portal)
    db = TitleDB.load(Settings(), refresh=True)
    assert db.available and len(list((cache / "gametdb").glob("*.zip"))) == 1


def test_stale_bad_zip_from_older_versions_is_dropped(cache):
    z = cache / TitleDB.zip_cache_name("EN")
    z.parent.mkdir(parents=True)
    z.write_bytes(b"<html>cached by an older ReWii</html>")
    mock(lambda req: httpx.Response(500))
    assert not TitleDB.load(Settings()).available
    assert not z.exists()


def test_index_that_is_not_an_object_is_ignored(cache):
    idx = TitleDB.index_path("EN")
    idx.parent.mkdir(parents=True)
    idx.write_text("[1, 2, 3]")
    mock(lambda req: httpx.Response(500))
    assert not TitleDB.load(Settings()).available               # "never raises"
